"""Exercise the production shell entry point with local Docker/filesystem doubles only."""
from __future__ import annotations

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
if args[:2] == ["image", "inspect"]:
    print("e" * 40 if os.environ.get("DOUBLE_BAD_IMAGE") else sha)
    sys.exit(0)
if args[0] == "pull":
    sys.exit(0)
if args[0] != "compose":
    raise SystemExit(99)
if args[1:] == ["version"]:
    sys.exit(0)
verbs = {"config", "run", "up", "stop", "exec"}
index = next(i for i, value in enumerate(args) if value in verbs)
verb, tail = args[index], args[index + 1:]
dbpath = root / "shared" / "data" / "stock-watch.db"
if verb in {"config", "stop"}:
    sys.exit(0)
if verb == "up":
    if "worker" in tail:
        with sqlite3.connect(dbpath) as db:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            heartbeat_sha = "stale" if os.environ.get("DOUBLE_STALE_WORKER") else sha
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES ('worker:heartbeat',?,?)", (heartbeat_sha, stamp))
    sys.exit(0)
position = tail.index("python")
python_args = tail[position + 1:]
if python_args == ["-m", "stock_watch", "init-db"]:
    with sqlite3.connect(dbpath) as db:
        if os.environ.get("DOUBLE_MIGRATE"):
            db.execute("PRAGMA user_version=3")
        if os.environ.get("DOUBLE_INIT_FAIL"):
            raise SystemExit(3)
        db.executescript("CREATE TABLE IF NOT EXISTS outbox(status TEXT); CREATE TABLE IF NOT EXISTS async_tasks(status TEXT); CREATE TABLE IF NOT EXISTS monitor_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT); PRAGMA user_version=2;")
    sys.exit(0)
if python_args[0] == "-c":
    code = python_args[1]
    sys.argv = ["-c", *python_args[2:]]
else:
    code = sys.stdin.read()
    sys.argv = ["-", *python_args[1:]]
code = code.replace("/data", (root / "shared" / "data").as_posix())
os.environ["MONITOR_ENABLED"] = os.environ.get("DOUBLE_MONITOR", "false")
if verb == "exec" and "app" in tail:
    payload = {"code": 0, "status": "ready", "build_sha": "wrong" if os.environ.get("DOUBLE_BAD_READY") else sha}
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


def worker_starts(calls):
    return [call for call in calls if "up" in call["args"] and "worker" in call["args"]]


@pytest.mark.parametrize("image,sha", [("ghcr.io/example/stock-watch:latest", SHA), (IMAGE + ";echo injected", SHA), (IMAGE, "../../outside"), (IMAGE, SHA.upper())])
def test_invalid_inputs_fail_before_docker(deployment, image, sha):
    base, run = deployment
    result, calls = run(image, sha)
    assert result.returncode != 0
    assert calls == []
    assert not (base / "shared" / "image-release.env").exists()


@pytest.mark.parametrize("status,task", [("pending", None), ("sending", None), (None, "pending"), (None, "running")])
def test_existing_real_queue_blocks_worker_and_retains_backup(deployment, status, task):
    base, run = deployment
    seed_database(base, status=status, task=task)
    result, calls = run()
    assert result.returncode != 0
    assert "WORKER_QUEUE_APPROVED=true" in result.stderr
    assert worker_starts(calls) == []
    assert not (base / "shared" / "image-release.env").exists()
    backups = list((base / "shared" / "data" / "backups").glob("pre-image-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2


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


def test_recovery_keeps_worker_stopped_if_queue_needs_approval(deployment):
    base, run = deployment
    seed_database(base, status="pending")
    previous_release(base)
    result, calls = run()
    assert result.returncode != 0
    assert worker_starts(calls) == []
    assert "Previous app restored; worker remains stopped" in result.stderr


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
