#!/usr/bin/env bash
set -Eeuo pipefail

: "${TZ:=Etc/UTC}"
: "${WEB_SERVICE_BIND:=0.0.0.0}"
: "${WEB_SERVICE_PORT:=8998}"
: "${FORWARDED_ALLOW_IPS:=127.0.0.1}"
: "${POLISHRR_TOKEN:?POLISHRR_TOKEN env var required}"

echo "Starting Polishrr on ${WEB_SERVICE_BIND}:${WEB_SERVICE_PORT}"

mkdir -p /config /app/runtime
chown -R polishrr:polishrr /config /app/runtime 2>/dev/null || true
chmod -R u+rwX,g+rwX,o-rwx /config /app/runtime 2>/dev/null || true
touch /app/runtime/cron.log
chown polishrr:polishrr /app/runtime/cron.log 2>/dev/null || true

if [ ! -f /etc/cron.d/my-cron-job ]; then
  echo "Error: /etc/cron.d/my-cron-job template missing."
  exit 1
fi

cp /etc/cron.d/my-cron-job /app/runtime/my-cron-job.actual
chmod 0644 /app/runtime/my-cron-job.actual
crontab /app/runtime/my-cron-job.actual
cron
echo "Cron started."

echo "Launching web service as 'polishrr'..."
exec su -s /bin/bash polishrr -c "uvicorn web_service:app \
  --host ${WEB_SERVICE_BIND} \
  --port ${WEB_SERVICE_PORT} \
  --proxy-headers \
  --forwarded-allow-ips='${FORWARDED_ALLOW_IPS}' \
  --log-level info"
