#!/usr/bin/env python3
"""Exercise deployment recovery with real, pre-existing images in an isolated project.

Run as root on Linux with Python 3.8+ and Docker Compose v2. This does not build
images, load the production environment, publish ports, or run application workers.
The app and worker commands are replaced by offline readiness/heartbeat fixtures.
Only this process's randomly named Compose project and directory are cleaned up.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


IMAGE_RE = re.compile(r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
CHECKS_ROOT = Path("/srv/stock-watch/recovery-checks")

APP_FIXTURE = r'''import http.server, json, os, pathlib
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        sha = os.environ["BUILD_SHA"]
        if self.path == "/ready" and pathlib.Path("/data/fail-candidate-ready").exists() and sha == os.environ["PROBE_NEW_SHA"]:
            sha = "0" * 40
        data = json.dumps({"code": 0, "status": "ready", "build_sha": sha}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
'''

WORKER_FIXTURE = r'''import datetime, os, pathlib, signal, sqlite3, time
dbpath = "/data/stock-watch.db"
running = True
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
def stop(signum, frame):
    global running
    marker = pathlib.Path("/data/inject-stop-pending")
    if marker.exists():
        marker.unlink()
        stamp = now()
        with sqlite3.connect(dbpath, timeout=5) as db:
            db.execute("INSERT INTO async_tasks(task_key,task_type,payload_json,status,available_at,created_at,updated_at) VALUES('probe-stop-race','probe','{}','pending',?,?,?)", (stamp, stamp, stamp))
    running = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while running:
    with sqlite3.connect(dbpath, timeout=5) as db:
        db.execute("INSERT INTO monitor_state(key,value,updated_at) VALUES('worker:heartbeat',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (os.environ["BUILD_SHA"], now()))
    time.sleep(0.15)
'''

COMPOSE_FIXTURE = '''name: {project}
x-runtime: &runtime
  image: ${{STOCK_WATCH_IMAGE:?}}
  env_file:
    - ${{STOCK_WATCH_ENV_FILE:?}}
  environment:
    DATA_DIR: /data
    DATABASE_PATH: /data/stock-watch.db
    BUILD_SHA: ${{BUILD_SHA:?}}
    PROBE_NEW_SHA: {new_sha}
  user: "10001:10001"
  network_mode: none
  volumes:
    - ${{STOCK_WATCH_DATA_DIR:?}}:/data
    - {fixtures}:/probe:ro
  restart: "no"
  read_only: true
  tmpfs:
    - /tmp:size=16m,mode=1777
  security_opt:
    - no-new-privileges:true
  cap_drop: [ALL]
  mem_limit: 192m
  pids_limit: 64
services:
  app:
    <<: *runtime
    command: [python, /probe/app.py]
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]
      interval: 1s
      timeout: 3s
      retries: 10
      start_period: 1s
  worker:
    <<: *runtime
    command: [python, /probe/worker.py]
    stop_grace_period: 10s
    healthcheck:
      test: [CMD, python, -c, "import os,sqlite3; d=sqlite3.connect('file:/data/stock-watch.db?mode=ro',uri=True); r=d.execute(\\\"SELECT value FROM monitor_state WHERE key='worker:heartbeat'\\\").fetchone(); assert r and r[0]==os.environ['BUILD_SHA']"]
      interval: 1s
      timeout: 3s
      retries: 10
      start_period: 1s
'''


class CheckFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def checked_run(command, env, input_text=None, timeout=120, allow_failure=False):
    # Never invoke a shell or print subprocess output/environment. Fixture output
    # is intentionally captured; summaries contain counts, revisions, and stages.
    result = subprocess.run(command, input=input_text, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=timeout)
    if result.returncode and not allow_failure:
        raise CheckFailure("command failed: {} (exit {})".format(command[0], result.returncode))
    return result


def sanitized_environment():
    # Compose must not inherit interpolation values or approval from the caller.
    env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
           "HOME": "/root", "LANG": "C.UTF-8", "MONITOR_ENABLED": "false",
           "WORKER_QUEUE_APPROVED": "false"}
    return env


class RecoveryProbe:
    def __init__(self, args, source, source_digest):
        self.args = args
        self.source_digest = source_digest
        self.env = sanitized_environment()
        self.base = None
        self.project = None
        self.results = []
        self.summary_path = None
        self.source = source

    def create(self):
        require(os.geteuid() == 0, "run this isolated check as root")
        require(sys.platform.startswith("linux"), "Linux host required")
        for path in (Path("/srv"), Path("/srv/stock-watch")):
            require(path.is_dir() and not path.is_symlink(), "unsafe recovery parent")
        if CHECKS_ROOT.exists():
            require(CHECKS_ROOT.is_dir() and not CHECKS_ROOT.is_symlink(), "unsafe checks root")
        else:
            CHECKS_ROOT.mkdir(mode=0o700)
        self.base = Path(tempfile.mkdtemp(prefix="run-", dir=str(CHECKS_ROOT)))
        self.base.chmod(0o755)
        suffix = self.base.name.removeprefix("run-") if sys.version_info >= (3, 9) else self.base.name[4:]
        self.project = "stock-watch-recovery-" + suffix
        require(re.fullmatch(r"stock-watch-recovery-[a-z0-9_]+", self.project), "unsafe project name")
        self.shared = self.base / "shared"
        self.data = self.shared / "data"
        self.fixtures = self.base / "fixtures"
        self.envfile = self.shared / ".env"
        self.state = self.shared / "image-release.env"
        self.marker = self.base / "current-image"
        for directory in (self.shared, self.data, self.data / "backups", self.fixtures):
            directory.mkdir(mode=0o755)
        self.fixtures.chmod(0o755)
        os.chown(self.data, 10001, 10001)
        os.chown(self.data / "backups", 10001, 10001)
        self.envfile.write_text("MONITOR_ENABLED=false\n", encoding="utf-8")
        self.envfile.chmod(0o600)
        (self.fixtures / "app.py").write_text(APP_FIXTURE, encoding="utf-8")
        (self.fixtures / "worker.py").write_text(WORKER_FIXTURE, encoding="utf-8")
        for fixture in (self.fixtures / "app.py", self.fixtures / "worker.py"):
            fixture.chmod(0o644)
        compose_text = COMPOSE_FIXTURE.format(project=self.project, new_sha=self.args.new_sha,
                                               fixtures=self.fixtures.as_posix())
        require(self.source.count("/srv/stock-watch") == 1, "unexpected deployment base replacement count")
        require(self.source.count("-p stock-watch ") == 2, "unexpected Compose project replacement count")
        derived = self.source.replace("/srv/stock-watch", self.base.as_posix()).replace(
            "-p stock-watch ", "-p " + self.project + " ")
        require("/srv/stock-watch" not in derived.replace(self.base.as_posix(), ""), "unreplaced production base")
        require("-p stock-watch " not in derived, "unreplaced production project")
        for sha in (self.args.old_sha, self.args.new_sha):
            release = self.base / "image-releases" / sha
            (release / "scripts").mkdir(parents=True)
            (release / "compose.registry.yaml").write_text(compose_text, encoding="utf-8")
            (release / "scripts" / "deploy-image.sh").write_text(derived, encoding="utf-8")
        self.select_old()
        config = self.compose("config", "--format", "json").stdout
        config_data = json.loads(config)
        for service in config_data["services"].values():
            require(service["network_mode"] == "none", "fixture has a network")
            require(not service.get("ports"), "fixture publishes ports")
            require(service["command"][0:1] == ["python"], "unexpected fixture entry command")
            require(service["command"][1] in ("/probe/app.py", "/probe/worker.py"), "unexpected fixture command")
            for volume in service.get("volumes", []):
                require(Path(volume["source"]).resolve().is_relative_to(self.base.resolve())
                        if sys.version_info >= (3, 9)
                        else str(Path(volume["source"]).resolve()).startswith(str(self.base.resolve()) + "/"),
                        "fixture volume escaped temporary base")
        self.compose("run", "--rm", "--no-deps", "-T", "app", "python", "-m", "stock_watch", "init-db")
        self.seed()
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            with sqlite3.connect(str(self.base / "baseline.db")) as baseline:
                db.backup(baseline)
        self.expected = self.fingerprint()
        self.summary_path = self.base / "summary.json"

    def select_old(self):
        self.env.update(STOCK_WATCH_IMAGE=self.args.old_image, BUILD_SHA=self.args.old_sha,
                        STOCK_WATCH_ENV_FILE=str(self.envfile), STOCK_WATCH_DATA_DIR=str(self.data))
        self.old_compose = self.base / "image-releases" / self.args.old_sha / "compose.registry.yaml"

    def compose(self, *arguments, **kwargs):
        return checked_run(["docker", "compose", "-p", self.project, "--env-file", str(self.envfile),
                            "-f", str(self.old_compose)] + list(arguments), self.env, **kwargs)

    def seed(self):
        stamp = utcnow()
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            db.execute("UPDATE config_state SET config_version=config_version+1,updated_at=? WHERE singleton=1", (stamp,))
            db.execute("INSERT INTO seen_articles(article_key,seen_at) VALUES('recovery-probe-seen',?)", (stamp,))
            db.execute("INSERT INTO monitor_state(key,value,updated_at) VALUES('baseline:recovery-probe','ready',?)", (stamp,))
            db.execute("INSERT INTO outbox(event_key,payload_json,status,available_at,created_at,sent_at) VALUES('recovery-probe-sent','{}','sent',?,?,?)", (stamp, stamp, stamp))

    def fingerprint(self):
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            selected = {}
            for table in ("config_state", "stocks", "stock_aliases", "stock_topics", "themes",
                          "theme_keywords", "config_audit", "seen_articles", "outbox"):
                selected[table] = db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            selected["baseline"] = db.execute("SELECT * FROM monitor_state WHERE key='baseline:recovery-probe'").fetchall()
            selected["schema"] = db.execute("PRAGMA user_version").fetchone()[0]
        return hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    def add_pending(self):
        stamp = utcnow()
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            db.execute("INSERT INTO async_tasks(task_key,task_type,payload_json,status,available_at,created_at,updated_at) VALUES('probe-preflight','probe','{}','pending',?,?,?)", (stamp, stamp, stamp))

    def pending_count(self):
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            return db.execute("SELECT count(*) FROM async_tasks WHERE status IN ('pending','running')").fetchone()[0]

    def heartbeat(self):
        with sqlite3.connect(str(self.data / "stock-watch.db")) as db:
            return db.execute("SELECT value,updated_at FROM monitor_state WHERE key='worker:heartbeat'").fetchone()

    def ids(self):
        result = {}
        for service in ("app", "worker"):
            output = self.compose("ps", "-a", "-q", service).stdout.strip()
            require(output and "\n" not in output, "expected exactly one fixture container")
            inspected = checked_run(["docker", "inspect", "--format", "{{json .}}", output], self.env)
            obj = json.loads(inspected.stdout)
            require(obj["Config"]["Labels"].get("com.docker.compose.project") == self.project,
                    "container belongs to another project")
            require(obj["HostConfig"]["NetworkMode"] == "none", "container network escaped fixture")
            require(not obj["HostConfig"].get("PortBindings"), "container publishes ports")
            result[service] = {"id": obj["Id"], "running": obj["State"]["Running"],
                               "image": obj["Config"]["Image"]}
        return result

    def reset(self):
        self.select_old()
        self.compose("down", "--remove-orphans", "--timeout", "10")
        for path in (self.data / "inject-stop-pending", self.data / "fail-candidate-ready"):
            if path.exists():
                path.unlink()
        with sqlite3.connect(str(self.base / "baseline.db")) as source:
            with sqlite3.connect(str(self.data / "stock-watch.db")) as target:
                source.backup(target)
        self.state.write_text("STOCK_WATCH_IMAGE={}\nBUILD_SHA={}\n".format(self.args.old_image, self.args.old_sha), encoding="utf-8")
        if self.marker.is_symlink():
            self.marker.unlink()
        self.marker.symlink_to(self.base / "image-releases" / self.args.old_sha)
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "20", "app", "worker")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            beat = self.heartbeat()
            if beat and beat[0] == self.args.old_sha:
                return
            time.sleep(0.1)
        raise CheckFailure("old fixture heartbeat unavailable")

    def deploy(self):
        started = utcnow()
        deployment = self.base / "image-releases" / self.args.new_sha / "scripts" / "deploy-image.sh"
        result = checked_run(["bash", str(deployment), self.args.new_image, self.args.new_sha],
                             self.env, timeout=200, allow_failure=True)
        stages = re.findall(r"failed during ([a-z-]+)", result.stderr)
        return {"returncode": result.returncode, "failure_stage": stages[-1] if stages else None,
                "started_at": started}

    def assert_old_restored(self, started, worker_running=True):
        containers = self.ids()
        require(containers["app"]["running"], "old app not running after rejection/recovery")
        require(containers["app"]["image"] == self.args.old_image, "old app image not restored")
        require(containers["worker"]["running"] == worker_running, "worker running state not preserved")
        require(containers["worker"]["image"] == self.args.old_image, "old worker image not restored")
        if worker_running:
            beat = self.heartbeat()
            require(beat and beat[0] == self.args.old_sha, "old worker heartbeat revision missing")
            require(beat[1] >= started, "old worker heartbeat is stale")
        require(self.marker.resolve().name == self.args.old_sha, "failed deployment changed release marker")
        require(self.state.read_text(encoding="utf-8").splitlines() == [
            "STOCK_WATCH_IMAGE=" + self.args.old_image, "BUILD_SHA=" + self.args.old_sha],
            "failed deployment changed release metadata")
        require(self.fingerprint() == self.expected, "persistent business fixture rows changed")
        return containers

    def run_case(self, name, operation):
        self.reset()
        started = time.monotonic()
        details = operation()
        record = {"case": name, "passed": True, "elapsed_seconds": round(time.monotonic() - started, 2)}
        record.update(details)
        self.results.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)

    def pending_rejection(self):
        self.add_pending()
        before = self.ids()
        result = self.deploy()
        require(result["returncode"] != 0, "pending queue deployment unexpectedly succeeded")
        require(result["failure_stage"] == "queue-preflight", "pending queue was not rejected at preflight")
        after = self.assert_old_restored(result["started_at"])
        require(all(before[s]["id"] == after[s]["id"] for s in before), "preflight rejection replaced old container")
        require(self.pending_count() == 1, "preflight pending task lost or consumed")
        return dict(result, container_ids_unchanged=True, pending_tasks=1)

    def failed_candidate(self):
        (self.data / "fail-candidate-ready").touch()
        result = self.deploy()
        require(result["returncode"] != 0, "wrong readiness revision unexpectedly succeeded")
        require(result["failure_stage"] == "app-readiness", "candidate did not reach readiness failure")
        self.assert_old_restored(result["started_at"])
        return dict(result, old_app_restored=True, old_worker_fresh=True)

    def stop_race(self):
        (self.data / "inject-stop-pending").touch()
        result = self.deploy()
        require(result["returncode"] != 0, "stop-time pending race unexpectedly succeeded")
        require(result["failure_stage"] == "queue-review", "stop-time queue was not rejected after migration")
        require(not (self.data / "inject-stop-pending").exists(), "worker stop injection was not exercised")
        require(self.pending_count() == 1, "stop-time pending task lost or consumed")
        self.assert_old_restored(result["started_at"])
        return dict(result, old_worker_fresh=True, pending_tasks=1)

    def stopped_worker(self):
        self.compose("stop", "worker")
        self.add_pending()
        before = self.ids()
        result = self.deploy()
        require(result["returncode"] != 0, "stopped worker with pending task unexpectedly deployed")
        require(result["failure_stage"] == "previous-runtime", "stopped worker was not rejected before stop")
        after = self.assert_old_restored(result["started_at"], worker_running=False)
        require(all(before[s]["id"] == after[s]["id"] for s in before), "rejection changed stopped-worker containers")
        require(self.pending_count() == 1, "stopped-worker pending task changed")
        return dict(result, worker_remained_stopped=True, container_ids_unchanged=True)

    def success(self):
        result = self.deploy()
        require(result["returncode"] == 0, "empty-queue deployment failed")
        containers = self.ids()
        require(all(item["running"] and item["image"] == self.args.new_image for item in containers.values()),
                "new app/worker not running")
        beat = self.heartbeat()
        require(beat and beat[0] == self.args.new_sha and beat[1] >= result["started_at"], "new worker heartbeat not fresh")
        require(self.marker.resolve().name == self.args.new_sha, "success marker not committed")
        require(self.state.read_text(encoding="utf-8").splitlines() == [
            "STOCK_WATCH_IMAGE=" + self.args.new_image, "BUILD_SHA=" + self.args.new_sha],
            "success metadata not committed")
        require(self.fingerprint() == self.expected, "success changed persistent business fixture rows")
        require(self.pending_count() == 0, "success created pending task")
        return dict(result, persistent_rows_preserved=True, new_worker_fresh=True)

    def cleanup(self):
        if self.base is None:
            return True
        require(self.base.parent == CHECKS_ROOT and not self.base.is_symlink() and
                self.base.name.startswith("run-") and self.base.resolve().parent == CHECKS_ROOT.resolve(),
                "refusing unsafe cleanup target")
        cleaned = True
        if self.project and hasattr(self, "old_compose"):
            result = self.compose("down", "--remove-orphans", "--timeout", "10", allow_failure=True)
            cleaned = result.returncode == 0
            output = checked_run(["docker", "ps", "-aq", "--filter",
                                  "label=com.docker.compose.project=" + self.project], self.env).stdout.strip()
            cleaned = cleaned and not output
        summary = {"project": self.project, "deployment_script_sha256": self.source_digest,
                   "old_sha": self.args.old_sha, "new_sha": self.args.new_sha,
                   "cases": self.results, "passed": len(self.results) == 5 and cleaned,
                   "containers_removed": cleaned, "production_project_touched": False,
                   "finished_at": utcnow()}
        # Keep only this non-sensitive summary. If Docker cleanup failed, leave
        # all fixtures for a deliberate follow-up instead of deleting live mounts.
        if cleaned:
            for child in self.base.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(str(child))
        self.summary_path = self.base / "summary.json"
        self.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.summary_path.chmod(0o600)
        print(json.dumps({"summary": str(self.summary_path), "passed": summary["passed"],
                          "containers_removed": cleaned}, sort_keys=True), flush=True)
        return cleaned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--old-sha", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument("--new-sha", required=True)
    parser.add_argument("--deploy-script", default=str(Path(__file__).with_name("deploy-image.sh")))
    args = parser.parse_args()
    for value in (args.old_image, args.new_image):
        require(IMAGE_RE.fullmatch(value), "immutable GHCR image digest required")
    for value in (args.old_sha, args.new_sha):
        require(SHA_RE.fullmatch(value), "40-character lowercase code SHA required")
    require(args.old_sha != args.new_sha and args.old_image != args.new_image, "two distinct images/revisions required")
    source_path = Path(args.deploy_script)
    require(source_path.is_file() and not source_path.is_symlink(), "deployment source must be a regular file")
    source = source_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    env = sanitized_environment()
    checked_run(["docker", "compose", "version"], env)
    for image, sha in ((args.old_image, args.old_sha), (args.new_image, args.new_sha)):
        result = checked_run(["docker", "image", "inspect", "--format",
                              '{{ index .Config.Labels "org.opencontainers.image.revision" }}', image], env)
        require(result.stdout.strip() == sha, "pre-existing image revision differs from supplied SHA")
    probe = RecoveryProbe(args, source, hashlib.sha256(source.encode()).hexdigest())
    failed = False
    try:
        probe.create()
        probe.run_case("pending_rejected_before_stop", probe.pending_rejection)
        probe.run_case("candidate_readiness_rollback", probe.failed_candidate)
        probe.run_case("stop_time_queue_race_rollback", probe.stop_race)
        probe.run_case("stopped_worker_not_started", probe.stopped_worker)
        probe.run_case("successful_release_preserves_rows", probe.success)
    except (CheckFailure, subprocess.TimeoutExpired, OSError, sqlite3.Error, ValueError) as error:
        failed = True
        print(json.dumps({"passed": False, "error_type": type(error).__name__,
                          "error": str(error) if isinstance(error, CheckFailure) else "isolated check failed"}), flush=True)
    finally:
        try:
            if not probe.cleanup():
                failed = True
        except Exception as error:
            failed = True
            print(json.dumps({"passed": False, "cleanup_error_type": type(error).__name__,
                              "directory": str(probe.base)}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CheckFailure, subprocess.TimeoutExpired, OSError, ValueError) as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__,
                          "error": str(error) if isinstance(error, CheckFailure) else "preflight failed"}), flush=True)
        sys.exit(1)
