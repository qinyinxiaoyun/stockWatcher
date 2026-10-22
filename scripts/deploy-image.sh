#!/usr/bin/env bash
# Run an already published, immutable image; never build on the production host.
set -euo pipefail
umask 077

die() { printf '%s\n' "$1" >&2; exit 1; }
valid_image() { [[ "$1" =~ ^ghcr\.io/[a-z0-9]+([._-][a-z0-9]+)*/[a-z0-9]+([._-][a-z0-9]+)*@sha256:[0-9a-f]{64}$ ]]; }
valid_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }
[[ $# == 2 ]] || die 'Usage: bash scripts/deploy-image.sh GHCR_IMAGE_DIGEST CODE_SHA'
image=$1
sha=$2
valid_image "$image" || die 'Invalid immutable GHCR image reference'
valid_sha "$sha" || die 'Invalid code SHA'
[[ ${WORKER_QUEUE_APPROVED:-false} == false || ${WORKER_QUEUE_APPROVED:-false} == true ]] || die 'WORKER_QUEUE_APPROVED must be true or false'

base=/srv/stock-watch
shared=$base/shared
envfile=$shared/.env
data=$shared/data
state=$shared/image-release.env
marker=$base/current-image
release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ "$release" == "$base/image-releases/$sha" ]] || die 'Unexpected image release directory'
[[ $(id -u) == 0 ]] || die 'Run as root'
[[ -d "$base" && ! -L "$base" && -d "$shared" && ! -L "$shared" ]] || die 'Missing or unsafe shared directory'
[[ -f "$envfile" && ! -L "$envfile" ]] || die 'Provision the private shared environment file first'
[[ ! -L "$data" && ! -L "$data/backups" ]] || die 'Persistent data directories must not be symlinks'
[[ ! -e "$marker" || -L "$marker" ]] || die 'current-image must be a symlink'
[[ -f "$release/compose.registry.yaml" && ! -L "$release/compose.registry.yaml" ]] || die 'Missing image Compose file'

exec 9>"$shared/deploy.lock"
flock -n 9 || die 'Another stock-watch deployment is running'
install -d -m 0750 -o 10001 -g 10001 "$data" "$data/backups"

# Treat release metadata as data, not executable shell input.
previous_image=
previous_sha=
previous_marker=
if [[ -e "$state" || -L "$state" ]]; then
    [[ -f "$state" && ! -L "$state" ]] || die 'Unsafe image release metadata'
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            STOCK_WATCH_IMAGE=*) [[ -z "$previous_image" ]] || die 'Duplicate image metadata'; previous_image=${line#*=} ;;
            BUILD_SHA=*) [[ -z "$previous_sha" ]] || die 'Duplicate SHA metadata'; previous_sha=${line#*=} ;;
            *) die 'Invalid image release metadata' ;;
        esac
    done < "$state"
    valid_image "$previous_image" && valid_sha "$previous_sha" || die 'Invalid previous image metadata'
fi
if [[ -L "$marker" ]]; then
    previous_marker=$(readlink -- "$marker")
    [[ -n "$previous_sha" && "$previous_marker" == "$base/image-releases/$previous_sha" ]] || die 'Inconsistent previous release marker'
fi

export STOCK_WATCH_IMAGE="$image" BUILD_SHA="$sha"
export STOCK_WATCH_ENV_FILE="$envfile" STOCK_WATCH_DATA_DIR="$data"
compose=(docker compose -p stock-watch --env-file "$envfile" -f "$release/compose.registry.yaml")
stage=preflight
stopped=false
complete=false
pre_schema=
state_tmp=
link_tmp=
records_started=false
previous_worker_running=false

queue_check() {
    # MONITOR_ENABLED=false only stops scheduling; existing tasks still call real APIs.
    "${compose[@]}" run --rm --no-deps -T app python - "${WORKER_QUEUE_APPROVED:-false}" <<'PY'
import sqlite3, sys
with sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True) as db:
    deliveries = db.execute("SELECT count(*) FROM outbox WHERE status NOT IN ('sent','dead')").fetchone()[0]
    tasks = db.execute("SELECT count(*) FROM async_tasks WHERE status IN ('pending','running')").fetchone()[0]
if (deliveries or tasks) and sys.argv[1] != "true":
    raise SystemExit(1)
PY
}

ready_check() {
    "${compose[@]}" exec -T app python - "$1" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=5) as response:
    payload = json.load(response)
if payload.get("code") != 0 or payload.get("status") != "ready" or payload.get("build_sha") != sys.argv[1]:
    raise SystemExit(1)
PY
}

schema_check() {
    "${compose[@]}" run --rm --no-deps -T app python - <<'PY'
import hashlib, json, sqlite3
with sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True) as db:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name").fetchall()
print(str(version) + ":" + hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest())
PY
}

worker_check() {
    "${compose[@]}" exec -T worker python - "$1" "$2" <<'PY'
import datetime, sqlite3, sys
with sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True) as db:
    row = db.execute("SELECT value,updated_at FROM monitor_state WHERE key='worker:heartbeat'").fetchone()
if not row or row[0] != sys.argv[1]:
    raise SystemExit(1)
observed = datetime.datetime.fromisoformat(row[1].replace("Z", "+00:00"))
started = datetime.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
if observed < started:
    raise SystemExit(1)
PY
}

capture_previous_runtime() {
    local cid facts service running health observed_image since
    if [[ -z "$previous_sha" ]]; then
        cid=$("${compose[@]}" ps --all --quiet app worker) || die 'Could not inspect existing services'
        [[ -z "$cid" ]] || die 'Existing services have no trusted previous release metadata'
        return
    fi
    local previous_release=$base/image-releases/$previous_sha
    [[ -d "$previous_release" && ! -L "$previous_release" && -f "$previous_release/compose.registry.yaml" && ! -L "$previous_release/compose.registry.yaml" ]] || die 'Missing or unsafe previous Compose release'
    [[ -s "$data/stock-watch.db" ]] || die 'Existing release database is missing or empty'
    for service in app worker; do
        cid=$("${compose[@]}" ps --all --quiet "$service") || die 'Could not inspect previous service'
        [[ "$cid" =~ ^[0-9a-f]{12,64}$ ]] || die 'Previous service is absent or ambiguous; no service was stopped'
        facts=$(docker inspect --format '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.Config.Image}}' "$cid") || die 'Could not inspect previous container identity'
        IFS='|' read -r running health observed_image <<< "$facts"
        [[ "$running" == true && "$health" == healthy && "$observed_image" == "$previous_image" ]] || die 'Previous service is stopped, unhealthy, or has an unexpected image; no service was stopped'
    done
    ready_check "$previous_sha" >/dev/null 2>&1 || die 'Previous app version is not ready; no service was stopped'
    since=$(date -u -d '3 minutes ago' +%Y-%m-%dT%H:%M:%S%z)
    worker_check "$previous_sha" "$since" >/dev/null 2>&1 || die 'Previous worker version or heartbeat is invalid; no service was stopped'
    previous_worker_running=true
}

restore_release_records() {
    [[ "$records_started" == true ]] || return 0
    if [[ -n "$previous_sha" ]]; then
        state_tmp=$(mktemp "$shared/.image-restore.XXXXXX") || return 1
        printf 'STOCK_WATCH_IMAGE=%s\nBUILD_SHA=%s\n' "$previous_image" "$previous_sha" > "$state_tmp" || return 1
        mv -f -- "$state_tmp" "$state" || return 1
        state_tmp=
    else
        rm -f -- "$state" || return 1
    fi
    if [[ -n "$previous_marker" ]]; then
        link_tmp=$base/.restore-image-$$
        ln -s -- "$previous_marker" "$link_tmp" || return 1
        mv -Tf -- "$link_tmp" "$marker" || return 1
        link_tmp=
    else
        rm -f -- "$marker" || return 1
    fi
    records_started=false
}

recover_previous() {
    [[ "$previous_worker_running" == true && -n "$previous_image" && "$pre_schema" =~ ^[0-9]+:[0-9a-f]{64}$ ]] || return 1
    local current_schema previous_release=$base/image-releases/$previous_sha
    [[ -f "$previous_release/compose.registry.yaml" && ! -L "$previous_release/compose.registry.yaml" ]] || return 1
    current_schema=$(schema_check 2>/dev/null) || return 1
    [[ "$current_schema" == "$pre_schema" ]] || return 1
    export STOCK_WATCH_IMAGE="$previous_image" BUILD_SHA="$previous_sha"
    compose=(docker compose -p stock-watch --env-file "$envfile" -f "$previous_release/compose.registry.yaml")
    "${compose[@]}" up -d --no-build --wait --wait-timeout 120 app >/dev/null 2>&1 || return 1
    ready_check "$previous_sha" >/dev/null 2>&1 || return 1
    # Resume the exact worker proven active before this attempt. A queue that
    # arrived during shutdown belongs to the previous service's normal work;
    # this does not approve its consumption by the candidate release.
    local started
    started=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ) || return 1
    if ! "${compose[@]}" up -d --no-build --wait --wait-timeout 120 worker >/dev/null 2>&1; then
        "${compose[@]}" stop worker >/dev/null 2>&1 || true
        return 1
    fi
    for attempt in {1..30}; do
        if worker_check "$previous_sha" "$started" >/dev/null 2>&1; then
            printf '%s\n' 'Previous containers restored; database was not restored' >&2
            return 0
        fi
        sleep 2
    done
    "${compose[@]}" stop worker >/dev/null 2>&1 || true
    return 1
}

finish() {
    local status=$?
    trap - EXIT
    [[ -z "$state_tmp" ]] || rm -f -- "$state_tmp" || true
    [[ -z "$link_tmp" ]] || rm -f -- "$link_tmp" || true
    if [[ "$complete" != true ]]; then
        printf 'Image deployment failed during %s; no success was recorded.\n' "$stage" >&2
        if ! restore_release_records; then
            printf '%s\n' 'Could not restore release metadata; inspect image-release.env and current-image' >&2
        fi
        if [[ "$stopped" == true ]]; then
            "${compose[@]}" stop worker app >/dev/null 2>&1 || true
            if ! recover_previous; then
                printf '%s\n' 'Automatic container recovery unavailable or unsuccessful; inspect service state before retrying' >&2
            fi
            printf '%s\n' 'Database was not restored; any pre-migration backup remains in shared/data/backups' >&2
        fi
        [[ "$status" != 0 ]] || status=1
    fi
    [[ -z "$state_tmp" ]] || rm -f -- "$state_tmp" || true
    [[ -z "$link_tmp" ]] || rm -f -- "$link_tmp" || true
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose version >/dev/null 2>&1
"${compose[@]}" config --quiet >/dev/null 2>&1
stage=image-pull
docker pull "$image" >/dev/null 2>&1
revision=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image" 2>/dev/null)
[[ "$revision" == "$sha" ]] || die 'Image revision does not match requested code SHA'

stage=monitor-policy
# Resolve Compose env_file exactly as the service does, without sourcing or printing it.
"${compose[@]}" run --rm --no-deps -T app python - "$([[ -n "$previous_sha" ]] && printf existing || printf first)" <<'PY' >/dev/null 2>&1
import os, sys
value = os.environ.get("MONITOR_ENABLED", "").strip().lower()
if value not in {"true", "false"} or (sys.argv[1] == "first" and value != "false"):
    raise SystemExit(1)
PY

stage=previous-runtime
capture_previous_runtime
if [[ -s "$data/stock-watch.db" ]]; then
    pre_schema=$(schema_check) || die 'Could not read the existing database structure before shutdown'
    [[ "$pre_schema" =~ ^[0-9]+:[0-9a-f]{64}$ ]] || die 'Invalid existing database structure signature'
    stage=queue-preflight
    queue_check >/dev/null 2>&1 || die 'Pending real work requires explicit WORKER_QUEUE_APPROVED=true; existing services were left running'
fi

stage=stop-services
stopped=true
"${compose[@]}" stop worker app >/dev/null 2>&1
stage=backup
if [[ -s "$data/stock-watch.db" ]]; then
    backup_schema=$("${compose[@]}" run --rm --no-deps -T app python - <<'PY'
import datetime, hashlib, json, pathlib, sqlite3
with sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True) as source:
    schema = source.execute("PRAGMA user_version").fetchone()[0]
    structure = source.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name").fetchall()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = pathlib.Path("/data/backups") / ("pre-image-" + stamp + ".db")
    with path.open("xb"):
        pass
    with sqlite3.connect(path) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
print(str(schema) + ":" + hashlib.sha256(json.dumps(structure, separators=(",", ":")).encode()).hexdigest())
PY
    )
    [[ "$backup_schema" == "$pre_schema" ]] || die 'Database structure changed before migration; inspect before retrying'
fi
stage=database-initialization
"${compose[@]}" run --rm --no-deps -T app python -m stock_watch init-db >/dev/null 2>&1
stage=queue-review
queue_check >/dev/null 2>&1 || die 'Pending real work requires explicit WORKER_QUEUE_APPROVED=true before worker startup'
stage=app-readiness
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 app >/dev/null 2>&1
ready_check "$sha" >/dev/null 2>&1
stage=worker-start
# Check again immediately before startup: HTTP requests may have queued work meanwhile.
queue_check >/dev/null 2>&1 || die 'Pending real work requires explicit WORKER_QUEUE_APPROVED=true before worker startup'
worker_started=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 worker >/dev/null 2>&1
stage=worker-heartbeat
worker_ready=false
for attempt in {1..30}; do
    if worker_check "$sha" "$worker_started" >/dev/null 2>&1; then
        worker_ready=true
        break
    fi
    sleep 2
done
[[ "$worker_ready" == true ]] || die 'Worker did not publish a fresh heartbeat for the requested SHA'

stage=record-release
state_tmp=$(mktemp "$shared/.image-release.XXXXXX")
printf 'STOCK_WATCH_IMAGE=%s\nBUILD_SHA=%s\n' "$image" "$sha" > "$state_tmp"
link_tmp=$base/.current-image-$$
ln -s -- "$release" "$link_tmp"
records_started=true
mv -f -- "$state_tmp" "$state"
state_tmp=
mv -Tf -- "$link_tmp" "$marker"
link_tmp=
complete=true
printf '%s\n' 'Image deployment ready; environment and existing Nginx configuration were preserved'
