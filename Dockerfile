# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      cron \
      curl \
      dumb-init \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG APP_USER=polishrr
RUN useradd -m -u 1000 -s /usr/sbin/nologin ${APP_USER}

WORKDIR /app

COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app.py polishrr_core.py scheduler_tick.py web_service.py entrypoint.sh cronjob.template .example_env /app/
COPY static/ /app/static/
COPY assets/ /app/assets/

RUN mkdir -p /config /app/runtime /var/run /run && \
    chown -R 1000:1000 /config /app/runtime /app/static /app/assets && \
    chmod 0770 /config /app/runtime && \
    sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh && \
    cp /app/cronjob.template /etc/cron.d/my-cron-job && \
    chmod 0644 /etc/cron.d/my-cron-job

EXPOSE 8998

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8998/healthz || exit 1

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/app/entrypoint.sh"]
