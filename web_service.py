from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import ipaddress
import os
import secrets
from pathlib import Path
from typing import AsyncGenerator, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (
    force_upgrade_single_item,
    get_download_queue,
    get_eligible_items,
    get_recent_upgrades,
    get_upgrade_status,
    load_app_config,
    load_settings,
    logger,
    run_radarr_upgrade,
    run_sonarr_upgrade,
    save_settings,
    upgrade_single_item,
)

app = FastAPI(title="Polishrr Web Service", version="3.0")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(os.environ.get("POLISHRR_STATIC_DIR", str(BASE_DIR / "static")))
ASSETS_DIR = Path(os.environ.get("POLISHRR_ASSETS_DIR", str(BASE_DIR / "assets")))

POLISHRR_TOKEN = os.environ.get("POLISHRR_TOKEN", "")
ALLOWED_IPS = [ip.strip() for ip in os.environ.get("ALLOWED_IPS", "").split(",") if ip.strip()]
SESSION_COOKIE = "polishrr_session"
SESSION_TTL_SECONDS = max(300, int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
LOGIN_WINDOW_SECONDS = max(60, int(os.environ.get("LOGIN_WINDOW_SECONDS", "300")))
MAX_FAILED_LOGINS = max(3, int(os.environ.get("MAX_FAILED_LOGINS", "10")))

RUN_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
LAST_STATUS = {"started": None, "finished": None, "running": False, "last_result": None}
SESSION_STORE: dict[str, dt.datetime] = {}
FAILED_LOGINS: dict[str, list[dt.datetime]] = {}


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, message: str) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping SSE message for a slow subscriber.")


EVENT_BROKER = EventBroker()


class SessionBody(BaseModel):
    token: str = Field(min_length=1)


class TriggerBody(BaseModel):
    target: Literal["radarr", "sonarr", "both"] = "both"


class ItemActionBody(BaseModel):
    target: Literal["radarr", "sonarr"]
    id: int


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ct_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _request_ip(request: Request) -> str:
    if request.client is None or not request.client.host:
        return ""
    return request.client.host


def _client_allowed(ip: str) -> bool:
    if not ALLOWED_IPS:
        return True
    try:
        candidate = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in ALLOWED_IPS:
        try:
            if candidate in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            if ip == net:
                return True
    return False


def _require_allowed_client(request: Request) -> None:
    if not _client_allowed(_request_ip(request)):
        raise HTTPException(status_code=403, detail="Forbidden")


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin.rstrip("/") == str(request.base_url).rstrip("/")


def _prune_sessions() -> None:
    now = _utcnow()
    expired = [session_id for session_id, expires_at in SESSION_STORE.items() if expires_at <= now]
    for session_id in expired:
        SESSION_STORE.pop(session_id, None)


def _prune_failed_logins() -> None:
    cutoff = _utcnow() - dt.timedelta(seconds=LOGIN_WINDOW_SECONDS)
    expired_ips = []
    for ip, attempts in FAILED_LOGINS.items():
        kept = [attempt for attempt in attempts if attempt > cutoff]
        if kept:
            FAILED_LOGINS[ip] = kept
        else:
            expired_ips.append(ip)
    for ip in expired_ips:
        FAILED_LOGINS.pop(ip, None)


def _login_limited(ip: str) -> bool:
    _prune_failed_logins()
    return len(FAILED_LOGINS.get(ip, [])) >= MAX_FAILED_LOGINS


def _record_failed_login(ip: str) -> None:
    _prune_failed_logins()
    FAILED_LOGINS.setdefault(ip, []).append(_utcnow())


def _session_valid(session_id: str | None) -> bool:
    if not session_id:
        return False
    _prune_sessions()
    expires_at = SESSION_STORE.get(session_id)
    return bool(expires_at and expires_at > _utcnow())


async def _auth(request: Request) -> dict:
    _require_allowed_client(request)

    session_id = request.cookies.get(SESSION_COOKIE)
    if _session_valid(session_id):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _origin_allowed(request):
            raise HTTPException(status_code=403, detail="Invalid origin")
        return {"auth": "session"}

    if not POLISHRR_TOKEN:
        raise HTTPException(status_code=503, detail="Service token not configured")

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")

    token = auth_header.split(" ", 1)[1].strip()
    if not _ct_equals(token, POLISHRR_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")

    return {"auth": "bearer"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _publish(event_name: str, data: str) -> None:
    await EVENT_BROKER.publish(f"event: {event_name}\ndata: {data}\n\n")


async def _run_and_stream(target: str) -> dict:
    await _publish("info", f"run_start {target} {_utcnow().isoformat()}")
    cfg = load_app_config()

    try:
        if target in ("radarr", "both"):
            await _publish("info", "starting radarr")
            await asyncio.to_thread(run_radarr_upgrade, cfg)
            await _publish("info", "finished radarr")

        if target in ("sonarr", "both"):
            await _publish("info", "starting sonarr")
            await asyncio.to_thread(run_sonarr_upgrade, cfg)
            await _publish("info", "finished sonarr")

        await _publish("done", "ok")
        return {"ok": True}
    except Exception as exc:
        logger.exception("Upgrade run failed:")
        await _publish("error", f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.post("/api/session")
async def create_session(body: SessionBody, request: Request) -> JSONResponse:
    _require_allowed_client(request)
    request_ip = _request_ip(request)
    if _login_limited(request_ip):
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")
    if not POLISHRR_TOKEN:
        raise HTTPException(status_code=503, detail="Service token not configured")
    if not _ct_equals(body.token, POLISHRR_TOKEN):
        _record_failed_login(request_ip)
        raise HTTPException(status_code=401, detail="Invalid token")

    session_id = secrets.token_urlsafe(32)
    SESSION_STORE[session_id] = _utcnow() + dt.timedelta(seconds=SESSION_TTL_SECONDS)
    FAILED_LOGINS.pop(request_ip, None)

    response = JSONResponse({"ok": True, "expires_in": SESSION_TTL_SECONDS})
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    _require_allowed_client(request)
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        SESSION_STORE.pop(session_id, None)

    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/status")
async def status(_: dict = Depends(_auth)) -> dict:
    return LAST_STATUS


@app.get("/api/upgrade-summary")
async def upgrade_summary(_: dict = Depends(_auth)) -> dict:
    return get_upgrade_status()


@app.post("/api/trigger")
async def trigger(body: TriggerBody, background: BackgroundTasks, _: dict = Depends(_auth)) -> dict:
    async with STATE_LOCK:
        if LAST_STATUS["running"]:
            raise HTTPException(status_code=409, detail="Run already in progress")
        LAST_STATUS.update({
            "started": _utcnow().isoformat(),
            "finished": None,
            "running": True,
            "last_result": None,
        })

    async def _job(target: str) -> None:
        async with RUN_LOCK:
            result = await _run_and_stream(target)
        async with STATE_LOCK:
            LAST_STATUS.update({
                "finished": _utcnow().isoformat(),
                "running": False,
                "last_result": result,
            })

    background.add_task(_job, body.target)
    return {"accepted": True}


@app.get("/api/events")
async def events(_: dict = Depends(_auth)) -> StreamingResponse:
    async def gen() -> AsyncGenerator[bytes, None]:
        queue = await EVENT_BROKER.subscribe()
        try:
            yield b": stream start\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                yield message.encode("utf-8")
        finally:
            await EVENT_BROKER.unsubscribe(queue)

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.get("/api/eligible")
async def eligible(_: dict = Depends(_auth)) -> dict:
    return get_eligible_items()


@app.get("/api/recent-upgrades")
async def recent_upgrades(_: dict = Depends(_auth)) -> dict:
    return get_recent_upgrades()


@app.get("/api/download-queue")
async def download_queue(tagged: bool = False, _: dict = Depends(_auth)) -> dict:
    return get_download_queue(tagged_only=tagged)


@app.post("/api/upgrade-item")
async def upgrade_item(body: ItemActionBody, _: dict = Depends(_auth)) -> dict:
    try:
        return upgrade_single_item(body.target, body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("upgrade_item failed for %s id=%s", body.target, body.id)
        raise HTTPException(status_code=500, detail="Upgrade action failed.") from exc


@app.post("/api/force-upgrade-item")
async def force_upgrade_item(body: ItemActionBody, _: dict = Depends(_auth)) -> dict:
    try:
        return force_upgrade_single_item(body.target, body.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("force_upgrade_item failed for %s id=%s", body.target, body.id)
        raise HTTPException(status_code=500, detail="Force upgrade action failed.") from exc


@app.get("/api/settings")
async def get_settings(_: dict = Depends(_auth)) -> dict:
    return load_settings()


@app.post("/api/settings")
async def update_settings(request: Request, _: dict = Depends(_auth)) -> dict:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Settings payload must be a JSON object.")
        settings = save_settings(payload)
        return {"ok": True, "settings": settings}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to update settings")
        raise HTTPException(status_code=500, detail="Settings update failed.") from exc


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    try:
        with open(STATIC_DIR / "status.html", "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return "<h1>Polishrr</h1><p>No static page found.</p>"
