#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE=hanseo-crawler-test
APP=/opt/hanseo-mate/crawler-test/source
ARTIFACTS=/var/lib/hanseo-crawler-test-deploy
ENV_FILE=/etc/hanseo-mate/crawler-test.env
SHA="${1:-}"
FILES=(crawler api_main.py main.py requirements.txt)

if [[ "$EUID" -ne 0 || ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid user or commit SHA" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]] ||
   ! grep -qx 'CRAWLER_TEST_DB_NAME=hanseo_mate_test' "$ENV_FILE"; then
  echo "Invalid test database configuration" >&2
  exit 1
fi

if [[ "$(systemctl show "$SERVICE" -p WorkingDirectory --value)" != "$APP" ]] ||
   ! systemctl cat "$SERVICE" | grep -Fq "EnvironmentFile=$ENV_FILE"; then
  echo "Invalid test crawler service configuration" >&2
  exit 1
fi

if [[ ! -x "$APP/.venv/bin/python" ||
      ! -f "$ARTIFACTS/$SHA.tar.gz" ]]; then
  echo "Missing Python environment or deployment package" >&2
  exit 1
fi

exec 9>/run/lock/hanseo-crawler-test-deploy.lock
flock -n 9 || {
  echo "Another deployment is running" >&2
  exit 1
}

WORK="$(mktemp -d /opt/hanseo-mate/crawler-test/.deploy-XXXXXXXX)"
mkdir -p "$WORK/stage" "$WORK/backup"
ROLLBACK=0

finish() {
  result=$?
  trap - EXIT
  set +e

  if (( result != 0 && ROLLBACK == 1 )); then
    echo "Deployment failed. Restoring previous test crawler."

    systemctl stop "$SERVICE"

    for file in "${FILES[@]}"; do
      if [[ -e "$WORK/backup/$file" ]]; then
        rm -rf -- "$APP/$file"
        mv -- "$WORK/backup/$file" "$APP/$file"
      fi
    done

    systemctl start "$SERVICE"
  fi

  rm -rf -- "$WORK"
  exit "$result"
}

trap finish EXIT

tar -xzf "$ARTIFACTS/$SHA.tar.gz" \
  -C "$WORK/stage" --no-same-owner

for file in "${FILES[@]}"; do
  [[ -e "$APP/$file" && -e "$WORK/stage/$file" ]] || {
    echo "Missing file: $file" >&2
    exit 1
  }
done

chmod 755 "$WORK" "$WORK/stage"

runuser -u hsmate -- \
  "$APP/.venv/bin/python" -m pip install \
  -r "$WORK/stage/requirements.txt"

ROLLBACK=1
systemctl stop "$SERVICE"

for file in "${FILES[@]}"; do
  mv -- "$APP/$file" "$WORK/backup/$file"
  mv -- "$WORK/stage/$file" "$APP/$file"
done

chown -R hsmate:hsmate \
  "$APP/crawler" "$APP/api_main.py" \
  "$APP/main.py" "$APP/requirements.txt"

systemctl start "$SERVICE"

for attempt in {1..20}; do
  if curl -fsS --max-time 3 \
       http://127.0.0.1:18000/health >/dev/null \
     && curl -fsS --max-time 3 \
       http://127.0.0.1:18000/cafeteria-crawl/daily-status >/dev/null; then
    ROLLBACK=0
    echo "Test crawler deployment succeeded: $SHA"
    exit 0
  fi
  sleep 1
done

echo "Test crawler health or database check failed" >&2
exit 1
