"""Exercise the production shell entry point with local Docker/filesystem doubles only."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
OLD_SHA = "b" * 40
IMAGE = "ghcr.io/example/stock-watch@sha256:" + "c" * 64
OLD_IMAGE = "ghcr.io/example/stock-watch@sha256:" + "d" * 64


def bash_executable():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    candidate = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not candidate:
        pytest.skip("Bash is required for offline deployment-script tests")
    return candidate


def bash_path(path):
    value = Path(path).as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


DOCKER_DOUBLE = r'''
import datetime, io, json, os, pathlib, sqlite3, sys, urllib.request
args = sys.argv[1:]
root = pathlib.Path(os.environ["DOUBLE_ROOT"])
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"args": args, "sha": os.environ.get("BUILD_SHA")}) + "\n")
sha = os.environ.get("BUILD_SHA", "")
target_sha = "a" * 40
state_path = root / "containers.json"
containers = json.loads(state_path.read_text()) if state_path.exists() else {}
def save_containers():
    state_path.write_text(json.dumps(containers), encoding="utf-8")
if args[:2] == ["image", "inspect"]:
    print("e" * 40 if os.environ.get("DOUBLE_BAD_IMAGE") else sha)
    sys.exit(0)
if args[0] == "pull":
    sys.exit(0)
if args[0] == "inspect":
    container = containers[{"1" * 64: "app", "2" * 64: "worker"}[args[-1]]]
    print("|".join((str(container["running"]).lower(), container["health"], container["image"])))
    sys.exit(0)
if args[0] != "compose":
    raise SystemExit(99)
if args[1:] == ["version"]:
    sys.exit(0)
verbs = {"config", "run", "up", "stop", "exec", "ps"}
index = next(i for i, value in enumerate(args) if value in verbs)
verb, tail = args[index], args[index + 1:]
dbpath = root / "shared" / "data" / "stock-watch.db"
if verb == "config":
    sys.exit(0)
if verb == "ps":
    for service in (value for value in tail if value in {"app", "worker"}):
        if service in containers:
            print(("1" if service == "app" else "2") * 64)
    sys.exit(0)
if verb == "stop":
    for service in tail:
        if service in containers:
            containers[service]["running"] = False
    save_containers()
    if os.environ.get("DOUBLE_QUEUE_AT_STOP") and dbpath.exists():
        with sqlite3.connect(dbpath) as db:
            db.execute("INSERT INTO outbox VALUES ('pending')")
    sys.exit(0)
if verb == "up":
    for service in ("app", "worker"):
        if service in tail:
            containers[service] = {"running": True, "health": "healthy", "image": os.environ["STOCK_WATCH_IMAGE"], "sha": sha}
    save_containers()
    if "app" in tail and sha == target_sha and os.environ.get("DOUBLE_QUEUE_AT_APP"):
        with sqlite3.connect(dbpath) as db:
            db.execute("INSERT INTO async_tasks VALUES ('pending')")
    if "worker" in tail:
        with sqlite3.connect(dbpath) as db:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            heartbeat_sha = "stale" if os.environ.get("DOUBLE_STALE_WORKER") and sha == target_sha else sha
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES ('worker:heartbeat',?,?)", (heartbeat_sha, stamp))
    sys.exit(0)
position = tail.index("python")
python_args = tail[position + 1:]
if python_args == ["-m", "stock_watch", "init-db"]:
    with sqlite3.connect(dbpath) as db:
        if os.environ.get("DOUBLE_MIGRATE"):
            db.execute("PRAGMA user_version=3")
        if os.environ.get("DOUBLE_DDL_FAIL"):
            db.execute("CREATE TABLE partially_migrated(value TEXT)")
            db.commit()
            raise SystemExit(6)
        if os.environ.get("DOUBLE_INIT_FAIL"):
            raise SystemExit(3)
        db.executescript("CREATE TABLE IF NOT EXISTS outbox(status TEXT); CREATE TABLE IF NOT EXISTS async_tasks(status TEXT); CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);")
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            db.execute("PRAGMA user_version=2")
    sys.exit(0)
if python_args[0] == "-c":
    code = python_args[1]
    sys.argv = ["-c", *python_args[2:]]
else:
    code = sys.stdin.read()
    sys.argv = ["-", *python_args[1:]]
code = code.replace("/data", (root / "shared" / "data").as_posix())
os.environ["MONITOR_ENABLED"] = os.environ.get("DOUBLE_MONITOR", "false")
if "source.backup" in code:
    if os.environ.get("DOUBLE_BACKUP_FAIL"):
        raise SystemExit(4)
    if os.environ.get("DOUBLE_BACKUP_SCHEMA_DRIFT"):
        with sqlite3.connect(dbpath) as db:
            db.execute("PRAGMA user_version=3")
if verb == "exec" and "app" in tail:
    app = containers.get("app")
    if not app or not app["running"]:
        raise SystemExit(5)
    active_sha = app["sha"]
    payload = {"code": 0, "status": "ready", "build_sha": "wrong" if os.environ.get("DOUBLE_BAD_READY") and active_sha == target_sha else active_sha}
    urllib.request.urlopen = lambda *a, **kw: io.BytesIO(json.dumps(payload).encode())
exec(compile(code, "offline-container-command", "exec"), {"__name__": "__main__"})
'''


@pytest.fixture
def deployment(tmp_path):
    base = tmp_path / "stock-watch"
    release = base / "image-releases" / SHA
    (release / "scripts").mkdir(parents=True)
    (base / "shared" / "data" / "backups").mkdir(parents=True)
    (base / "shared" / ".env").write_text("MONITOR_ENABLED=false\nPRIVATE_FIXTURE=never-print-this\n", encoding="utf-8")
    (release / "compose.registry.yaml").write_text("# handled by offline Docker double\n", encoding="utf-8")
    script = release / "scripts" / "deploy-image.sh"
    # Preserve every production branch; relocate its fixed server root into pytest's sandbox.
    script.write_text((ROOT / "scripts" / "deploy-image.sh").read_text(encoding="utf-8").replace("/srv/stock-watch", bash_path(base)), encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    double = tmp_path / "docker_double.py"
    double.write_text(DOCKER_DOUBLE, encoding="utf-8")
    stubs = {
        "docker": '#!/usr/bin/env bash\nexec "$DOUBLE_PYTHON" "$DOUBLE_SCRIPT" "$@"\n',
        "id": "#!/usr/bin/env bash\nprintf '0\\n'\n",
        "flock": "#!/usr/bin/env bash\nexit 0\n",
        "install": '#!/usr/bin/env bash\n# Directory already exists; never chown local test data.\nexit 0\n',
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "mv": '#!/usr/bin/env bash\nif [[ ${DOUBLE_METADATA_RENAME_FAIL:-false} == true && "$1" == -Tf && "$3" == *"/.current-image-"* && ! -e "$DOUBLE_ROOT/rename-failed" ]]; then touch "$DOUBLE_ROOT/rename-failed"; exit 1; fi\nexec /usr/bin/mv "$@"\n',
        "rm": '#!/usr/bin/env bash\nif [[ ${DOUBLE_CLEANUP_FAIL:-false} == true && "${@: -1}" == *"/.current-image-"* ]]; then exit 1; fi\nexec /usr/bin/rm "$@"\n',
    }
    # Windows requires developer mode for symlinks. Model only the final marker operations.
    if os.name == "nt":
        stubs["ln"] = '#!/usr/bin/env bash\nprintf "%s\\n" "$3" > "$4"\n'
    for name, content in stubs.items():
        path = bin_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    env = dict(os.environ, DOUBLE_ROOT=base.as_posix(), DOUBLE_PYTHON=bash_path(sys.executable), DOUBLE_SCRIPT=double.as_posix(), MSYS_NO_PATHCONV="1")
    # Bash normalizes Windows PATH itself, so prepend the double directory inside Bash.
    env["DOUBLE_BIN"] = bash_path(bin_dir)

    def run(*arguments, **options):
        values = env | options
        result = subprocess.run(
            [bash_executable(), "--noprofile", "--norc", "-c", 'export PATH="$DOUBLE_BIN:$PATH"; exec bash "$1" "${@:2}"', "offline-deploy", bash_path(script), *(arguments or (IMAGE, SHA))],
            env=values, cwd=release, capture_output=True, text=True, timeout=30,
        )
        assert "never-print-this" not in result.stdout + result.stderr
        calls_file = base / "calls.jsonl"
        calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
        return result, calls

    return base, run


def seed_database(base, *, status=None, task=None):
    db = sqlite3.connect(base / "shared" / "data" / "stock-watch.db")
    db.executescript("CREATE TABLE outbox(status TEXT); CREATE TABLE async_tasks(status TEXT); CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT); PRAGMA user_version=2;")
    if status:
        db.execute("INSERT INTO outbox VALUES (?)", (status,))
    if task:
        db.execute("INSERT INTO async_tasks VALUES (?)", (task,))
    db.commit()
    db.close()


def previous_release(base):
    release = base / "image-releases" / OLD_SHA
    release.mkdir()
    (release / "compose.registry.yaml").write_text("# previous offline release\n", encoding="utf-8")
    (base / "shared" / "image-release.env").write_text(f"STOCK_WATCH_IMAGE={OLD_IMAGE}\nBUILD_SHA={OLD_SHA}\n", encoding="utf-8", newline="\n")
    containers = {service: {"running": True, "health": "healthy", "image": OLD_IMAGE, "sha": OLD_SHA} for service in ("app", "worker")}
    (base / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.execute("INSERT OR REPLACE INTO monitor_state VALUES ('worker:heartbeat',?,?)", (OLD_SHA, stamp))
    db.close()


def worker_starts(calls):
    return [call for call in calls if "up" in call["args"] and "worker" in call["args"]]


def service_mutations(calls):
    return [call for call in calls if "compose" in call["args"] and any(verb in call["args"] for verb in ("stop", "up"))]


def container_state(base):
    path = base / "containers.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("image,sha", [("ghcr.io/example/stock-watch:latest", SHA), (IMAGE + ";echo injected", SHA), (IMAGE, "../../outside"), (IMAGE, SHA.upper())])
def test_invalid_inputs_fail_before_docker(deployment, image, sha):
    base, run = deployment
    result, calls = run(image, sha)
    assert result.returncode != 0
    assert calls == []
    assert not (base / "shared" / "image-release.env").exists()


@pytest.mark.parametrize("status,task", [("pending", None), ("sending", None), (None, "pending"), (None, "running")])
def test_existing_real_queue_is_rejected_before_stopping_services_or_backup(deployment, status, task):
    base, run = deployment
    seed_database(base, status=status, task=task)
    previous_release(base)
    original = container_state(base)
    result, calls = run()
    assert result.returncode != 0
    assert "WORKER_QUEUE_APPROVED=true" in result.stderr
    assert service_mutations(calls) == []
    assert not any("init-db" in call["args"] for call in calls)
    assert container_state(base) == original
    assert f"BUILD_SHA={OLD_SHA}" in (base / "shared" / "image-release.env").read_text(encoding="utf-8")
    backups = list((base / "shared" / "data" / "backups").glob("pre-image-*.db"))
    assert backups == []


@pytest.mark.parametrize("approve", [False, True])
def test_success_and_idempotent_redeployment(deployment, approve):
    base, run = deployment
    seed_database(base, status="pending" if approve else "sent", task=None if approve else "done")
    options = {"WORKER_QUEUE_APPROVED": "true"} if approve else {}
    for _ in range(2):
        result, calls = run(**options)
        assert result.returncode == 0, result.stderr
        assert "Image deployment ready" in result.stdout
        if os.name == "nt":
            # Remove the marker double; Windows cannot model an actual Linux symlink.
            (base / "current-image").unlink()
    assert worker_starts(calls)
    assert (base / "shared" / "image-release.env").read_text(encoding="utf-8") == f"STOCK_WATCH_IMAGE={IMAGE}\nBUILD_SHA={SHA}\n"
    assert len(list((base / "shared" / "data" / "backups").glob("pre-image-*.db"))) == 2


@pytest.mark.parametrize("scenario,stage", [("DOUBLE_BAD_IMAGE", "image-pull"), ("DOUBLE_BAD_READY", "app-readiness"), ("DOUBLE_STALE_WORKER", "worker-heartbeat"), ("DOUBLE_MONITOR", "monitor-policy")])
def test_wrong_revision_readiness_stale_worker_and_initial_monitor_fail(deployment, scenario, stage):
    base, run = deployment
    seed_database(base)
    result, _ = run(**{scenario: "true"})
    assert result.returncode != 0
    assert not (base / "shared" / "image-release.env").exists()
    assert "Image deployment ready" not in result.stdout
    assert f"failed during {stage}" in result.stderr


def test_first_deployment_initializes_an_empty_persistent_directory(deployment):
    base, run = deployment
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert worker_starts(calls)
    assert (base / "shared" / "data" / "stock-watch.db").is_file()
    assert not list((base / "shared" / "data" / "backups").glob("*.db"))


@pytest.mark.parametrize("schema_changes", [False, True])
def test_failed_migration_only_recovers_previous_containers_when_schema_unchanged(deployment, schema_changes):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    options = {"DOUBLE_INIT_FAIL": "true"}
    if schema_changes:
        options["DOUBLE_MIGRATE"] = "true"
    result, calls = run(**options)
    assert result.returncode != 0
    old_app_starts = [call for call in calls if call["sha"] == OLD_SHA and "up" in call["args"] and "app" in call["args"]]
    assert bool(old_app_starts) is not schema_changes
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (3 if schema_changes else 2)
    assert f"BUILD_SHA={OLD_SHA}" in (base / "shared" / "image-release.env").read_text(encoding="utf-8")
    assert list((base / "shared" / "data" / "backups").glob("pre-image-*.db"))


def test_queue_arriving_after_preflight_restores_original_running_worker(deployment):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, calls = run(DOUBLE_QUEUE_AT_STOP="true")
    assert result.returncode != 0
    assert worker_starts(calls)
    assert all(call["sha"] == OLD_SHA for call in worker_starts(calls))
    assert "Previous containers restored" in result.stderr
    assert all(service["running"] and service["sha"] == OLD_SHA for service in container_state(base).values())
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0] >= 1


def test_queue_arriving_after_app_start_never_starts_candidate_worker(deployment):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, calls = run(DOUBLE_QUEUE_AT_APP="true")
    assert result.returncode != 0
    assert "failed during worker-start" in result.stderr
    assert "Previous containers restored" in result.stderr
    assert worker_starts(calls)
    assert all(call["sha"] == OLD_SHA for call in worker_starts(calls))
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        assert db.execute("SELECT count(*) FROM async_tasks WHERE status='pending'").fetchone()[0] == 1


@pytest.mark.parametrize("identity", ["app_sha", "worker_sha", "worker_stale"])
def test_old_readiness_or_heartbeat_mismatch_rejects_before_stop(deployment, identity):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    if identity == "app_sha":
        containers = container_state(base)
        containers["app"]["sha"] = SHA
        (base / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
    else:
        with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
            if identity == "worker_sha":
                db.execute("UPDATE monitor_state SET value=? WHERE key='worker:heartbeat'", (SHA,))
            else:
                db.execute("UPDATE monitor_state SET updated_at='2000-01-01T00:00:00+00:00' WHERE key='worker:heartbeat'")
    original = container_state(base)
    result, calls = run()
    assert result.returncode != 0
    assert "failed during previous-runtime" in result.stderr
    assert service_mutations(calls) == []
    assert container_state(base) == original


@pytest.mark.parametrize("service,change", [("worker", {"running": False}), ("worker", {"health": "unhealthy"}), ("worker", {"image": IMAGE}), ("app", {"running": False}), ("app", {"health": "unhealthy"}), ("app", {"image": IMAGE})])
def test_unknown_or_unhealthy_original_service_is_rejected_untouched(deployment, service, change):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    containers = container_state(base)
    containers[service].update(change)
    (base / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
    result, calls = run()
    assert result.returncode != 0
    assert service_mutations(calls) == []
    assert container_state(base) == containers
    assert not list((base / "shared" / "data" / "backups").glob("*.db"))


@pytest.mark.parametrize("service", ["app", "worker"])
def test_service_without_previous_release_metadata_is_rejected_untouched(deployment, service):
    base, run = deployment
    seed_database(base)
    containers = {service: {"running": False, "health": "healthy", "image": OLD_IMAGE, "sha": OLD_SHA}}
    (base / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
    result, calls = run()
    assert result.returncode != 0
    assert service_mutations(calls) == []
    assert container_state(base) == containers


@pytest.mark.parametrize("empty_file", [False, True])
def test_previous_release_with_missing_or_empty_database_is_rejected_before_stop(deployment, empty_file):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    dbpath = base / "shared" / "data" / "stock-watch.db"
    dbpath.unlink()
    if empty_file:
        dbpath.touch()
    result, calls = run()
    assert result.returncode != 0
    assert service_mutations(calls) == []


@pytest.mark.parametrize("scenario", ["DOUBLE_BACKUP_FAIL", "DOUBLE_BAD_READY", "DOUBLE_STALE_WORKER"])
def test_failure_with_unchanged_schema_restores_original_app_and_worker(deployment, scenario):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, calls = run(**{scenario: "true"})
    assert result.returncode != 0
    assert "Previous containers restored" in result.stderr
    assert any(call["sha"] == OLD_SHA for call in worker_starts(calls))
    assert all(service["running"] and service["sha"] == OLD_SHA for service in container_state(base).values())
    assert f"BUILD_SHA={OLD_SHA}" in (base / "shared" / "image-release.env").read_text(encoding="utf-8")


def test_schema_drift_at_backup_prevents_migration_and_unsafe_container_restore(deployment):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, calls = run(DOUBLE_BACKUP_SCHEMA_DRIFT="true")
    assert result.returncode != 0
    assert not any("init-db" in call["args"] for call in calls)
    assert worker_starts(calls) == []
    assert "Previous containers restored" not in result.stderr
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_committed_partial_ddl_without_version_change_prevents_old_container_recovery(deployment):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, calls = run(DOUBLE_DDL_FAIL="true")
    assert result.returncode != 0
    assert worker_starts(calls) == []
    assert "Previous containers restored" not in result.stderr
    with sqlite3.connect(base / "shared" / "data" / "stock-watch.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='partially_migrated'").fetchone()
    assert all(not service["running"] for service in container_state(base).values())


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_release_metadata_rename_failure_keeps_previous_release_consistent(deployment, cleanup_failure):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    result, _ = run(DOUBLE_METADATA_RENAME_FAIL="true", DOUBLE_CLEANUP_FAIL="true" if cleanup_failure else "false")
    assert result.returncode != 0
    assert "Previous containers restored" in result.stderr
    assert f"BUILD_SHA={OLD_SHA}" in (base / "shared" / "image-release.env").read_text(encoding="utf-8")
    assert all(service["running"] and service["sha"] == OLD_SHA for service in container_state(base).values())
    marker = base / "current-image"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Real Linux symlink behavior is exercised by Linux CI and isolated Docker probe")
def test_release_metadata_failure_restores_existing_marker(deployment):
    base, run = deployment
    seed_database(base)
    previous_release(base)
    marker = base / "current-image"
    marker.symlink_to(base / "image-releases" / OLD_SHA)
    result, _ = run(DOUBLE_METADATA_RENAME_FAIL="true")
    assert result.returncode != 0
    assert "Previous containers restored" in result.stderr
    assert marker.is_symlink() and marker.resolve().name == OLD_SHA
    assert f"BUILD_SHA={OLD_SHA}" in (base / "shared" / "image-release.env").read_text(encoding="utf-8")


def test_metadata_is_never_sourced_and_original_deployment_is_untouched(deployment):
    base, run = deployment
    (base / "shared" / "image-release.env").write_text("BUILD_SHA=$(touch forbidden)\n", encoding="utf-8")
    result, calls = run()
    assert result.returncode != 0
    assert not (base / "forbidden").exists()
    assert calls == []
    script = (ROOT / "scripts" / "deploy-image.sh").read_text(encoding="utf-8")
    assert "nginx -t" not in script
    assert "systemctl" not in script
    assert '"$base/current"' not in script
    assert "docker build" not in script
