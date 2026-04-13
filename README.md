# Polishrr

Polishrr upgrades Radarr movies and Sonarr episodes that are still below the configured Custom Format cutoff.
It can run on a schedule, exposes a small web dashboard, and keeps manual upgrade actions available for review and intervention.

## What It Does

### Radarr
- Loads monitored movies with an existing file.
- Compares the current `customFormatScore` with the movie profile `cutoffFormatScore`.
- Tags randomly selected candidates and triggers `MoviesSearch`.
- Resets the upgrade tag cycle once all relevant movies are already tagged.

### Sonarr
- Resolves monitored episodes through their owning series and episode files.
- Compares the current file score with the series profile `cutoffFormatScore`.
- Tags the series, triggers `EpisodeSearch` for the selected episode IDs, and resets the cycle once all current candidate series are already tagged.

## Runtime Model

- `/config/.env` stores the ARR connection settings and default runtime values.
- `/config/settings.json` stores dashboard-managed settings and overrides the scheduling and per-run limits.
- Cron runs `scheduler_tick.py` once per minute.
- `scheduler_tick.py` evaluates the configured cron expression and only starts a run when the current minute matches.
- The web dashboard runs through FastAPI and Uvicorn.

This means dashboard changes are not cosmetic anymore. Saved settings affect the next scheduled run.

## Security Model

- Dashboard login uses the `POLISHRR_TOKEN` once and then switches to an HttpOnly session cookie.
- The token is no longer stored in browser storage.
- Server-Sent Events are authenticated as well.
- Optional IP filtering is supported via `ALLOWED_IPS`.
- Forwarded proxy headers are only trusted for the IPs listed in `FORWARDED_ALLOW_IPS`.
- Session login attempts are rate-limited in memory.
- The frontend renders API data through DOM APIs instead of injecting raw HTML.

## Configuration

Main config file: `/config/.env`

Example:

```env
# General
LOG_LEVEL=INFO
UPGRADE_TAG=upgrade-cf
CRON_SCHEDULE=0 * * * *
FORCE_ENABLED=false

# Radarr
PROCESS_RADARR=true
RADARR_URL=http://localhost:7878
RADARR_API_KEY=your_radarr_api_key
NUM_MOVIES_TO_UPGRADE=2

# Sonarr
PROCESS_SONARR=true
SONARR_URL=http://localhost:8989
SONARR_API_KEY=your_sonarr_api_key
NUM_EPISODES_TO_UPGRADE=3

# Web service
POLISHRR_TOKEN=replace_me_with_a_long_random_secret
ALLOWED_IPS=192.168.1.0/24,10.0.0.0/8
WEB_SERVICE_PORT=8998
WEB_SERVICE_BIND=0.0.0.0
FORWARDED_ALLOW_IPS=127.0.0.1
SESSION_TTL_HOURS=12
COOKIE_SECURE=false
LOGIN_WINDOW_SECONDS=300
MAX_FAILED_LOGINS=10
```

Notes:
- Use `COOKIE_SECURE=true` when the dashboard is served through HTTPS.
- `FORCE_ENABLED=true` is required before force-upgrade actions are accepted.
- Dashboard settings are persisted in `/config/settings.json`.

## Docker

Example `docker-compose.yml`:

```yaml
services:
  polishrr:
    image: ghcr.io/legromorph/polishrr:with-web
    container_name: polishrr
    environment:
      - CRON_SCHEDULE=0 * * * *
      - TZ=Europe/Berlin
      - POLISHRR_TOKEN=<LONG_SECRET_TOKEN>
      - FORWARDED_ALLOW_IPS=127.0.0.1
      - ALLOWED_IPS=192.168.1.0/24
    volumes:
      - /path/to/config:/config
    ports:
      - "8998:8998"
    restart: unless-stopped
```

Build locally:

```bash
docker build -t polishrr:local .
```

Run locally:

```bash
docker run --rm -p 8998:8998 -v /path/to/config:/config \
  -e POLISHRR_TOKEN=replace_me \
  polishrr:local
```

## Manual Usage

Run one cycle locally:

```bash
python app.py
```

Run inside the container:

```bash
docker exec -it polishrr python /app/app.py
```

Open the dashboard:

```text
http://localhost:8998/
```

At first login, enter the configured `POLISHRR_TOKEN`.

## Logs

- Application log: `/app/runtime/output_YYYY-MM-DD.log`
- Scheduler log: `/app/runtime/cron.log`

## GHCR Publishing

Tag and build:

```bash
docker build -t ghcr.io/<owner>/polishrr:with-web .
```

Login and push:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
docker push ghcr.io/<owner>/polishrr:with-web
```

The Docker image is Linux-compatible because it is built from `python:3.12-slim` and uses Linux-native cron/Uvicorn inside the container.

## License

MIT
