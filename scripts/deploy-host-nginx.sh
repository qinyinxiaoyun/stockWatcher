#!/usr/bin/env bash
# First/manual deployment on a host whose existing Nginx owns ports 80/443.
set -euo pipefail
umask 077

release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
sha=${1:?Usage: bash scripts/deploy-host-nginx.sh CODE_SHA}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid code SHA'; exit 1; }
[[ "$release" == "/srv/stock-watch/releases/$sha" ]] || { echo 'Unexpected release directory'; exit 1; }
[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }

shared=/srv/stock-watch/shared
envfile=$shared/.env
data=$shared/data
site=/etc/nginx/conf.d/stock-watch.conf
cert=/srv/platform/certs/stock.janettepa.uk

test -s "$cert/fullchain.pem"
test -s "$cert/privkey.pem"
test -f "$site"
test ! -L "$site"
nginx -t
docker compose version
[[ $(df -Pk /srv | awk 'NR==2 {print $4}') -ge 2097152 ]] || { echo 'Less than 2 GiB free; inspect disk usage'; exit 1; }

install -d -m 0700 "$shared"
install -d -m 0750 -o 10001 -g 10001 "$data" "$data/backups"
install -d -m 0755 -o 10001 -g 10001 "$data/verification"
exec 9>"$shared/deploy.lock"
flock -n 9 || { echo 'Another stock-watch deployment is running'; exit 1; }

if [[ ! -e "$envfile" ]]; then
    {
        printf '%s\n' 'MONITOR_ENABLED=false' 'SCHEDULE_TIMEZONE=Asia/Shanghai' 'LOG_LEVEL=INFO'
        printf '%s\n' 'APP_DOMAIN=stock.janettepa.uk' 'WORKER_POLL_SECONDS=2'
        printf 'PUSH_TOKEN=%s\n' "$(openssl rand -hex 32)"
        printf '%s\n' 'WECOM_CORP_ID=' 'WECOM_AGENT_ID=' 'WECOM_SECRET=' 'WECOM_TOUSER='
        printf '%s\n' 'WECOM_CALLBACK_TOKEN=' 'WECOM_ENCODING_AES_KEY=' 'WECOM_ADMIN_USERIDS='
        printf '%s\n' 'DEEPSEEK_API_KEY=' 'DEEPSEEK_URL=https://api.deepseek.com/chat/completions' 'DEEPSEEK_MODEL=deepseek-flash'
    } > "$envfile"
    chmod 0600 "$envfile"
    echo 'Created private environment scaffold; external credentials remain blank'
fi
grep -Eq '^MONITOR_ENABLED=false[[:space:]]*$' "$envfile" || { echo 'First/manual deployment requires MONITOR_ENABLED=false'; exit 1; }

export BUILD_SHA="$sha" STOCK_WATCH_ENV_FILE="$envfile" STOCK_WATCH_DATA_DIR="$data"
cd "$release"
compose=(docker compose --env-file "$envfile" -f compose.host-nginx.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" build app

# Back up before the initializer can migrate the database; never print its data.
if [[ -s "$data/stock-watch.db" ]]; then
    "${compose[@]}" stop worker app
    "${compose[@]}" run --rm --no-deps app python -c '
import datetime, pathlib, sqlite3
source = sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True)
path = pathlib.Path("/data/backups") / ("predeploy-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
with sqlite3.connect(path) as target:
    source.backup(target)
source.close()
print("Pre-migration online backup created")
'
fi

"${compose[@]}" run --rm --no-deps app python -m stock_watch init-db
# Do not consume an old delivery queue implicitly when rerunning this entry point.
"${compose[@]}" run --rm --no-deps app python -c '
import sqlite3
with sqlite3.connect("file:/data/stock-watch.db?mode=ro", uri=True) as db:
    deliveries = db.execute("SELECT count(*) FROM outbox WHERE status NOT IN (\"sent\",\"dead\")").fetchone()[0]
    tasks = db.execute("SELECT count(*) FROM async_tasks WHERE status IN (\"pending\",\"running\")").fetchone()[0]
if deliveries or tasks:
    raise SystemExit("Existing queue requires explicit review before worker startup")
print("Queue review passed")
'
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 app worker

install -d -m 0700 /srv/platform/backups
backup=$(mktemp /srv/platform/backups/stock-watch.conf.XXXXXX)
cp -p "$site" "$backup"
install -m 0644 deploy/nginx/stock-watch.conf "$site"
if ! nginx -t; then
    cp -p "$backup" "$site"
    echo 'Nginx validation failed; previous site file restored'
    exit 1
fi
if ! systemctl reload nginx; then
    cp -p "$backup" "$site"
    nginx -t && systemctl reload nginx
    echo 'Nginx reload failed; previous site file restored'
    exit 1
fi

ready=0
for attempt in {1..10}; do
    if curl --noproxy '*' --resolve stock.janettepa.uk:443:127.0.0.1 --connect-timeout 2 --max-time 5 -fsS https://stock.janettepa.uk/ready; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" == 1 ]] || { echo 'HTTPS readiness failed; inspect status before changing release'; exit 1; }
if [[ -e /srv/stock-watch/current && ! -L /srv/stock-watch/current ]]; then
    echo 'current is not a symlink; refusing to replace it'
    exit 1
fi
ln -sfn "$release" /srv/stock-watch/current
"${compose[@]}" ps
"${compose[@]}" exec -T worker python -m stock_watch worker-health
echo 'Deployment ready; automatic monitoring remains disabled'
