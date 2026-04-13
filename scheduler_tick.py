from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from croniter import croniter

from app import SCHEDULER_STATE_FILE, load_settings, logger, main

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local tooling
    fcntl = None

LOCK_FILE = Path("/app/runtime/scheduler.lock")


def _load_state() -> dict:
    path = Path(SCHEDULER_STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    path = Path(SCHEDULER_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _due_now(schedule: str, now: dt.datetime) -> bool:
    previous_minute = now - dt.timedelta(minutes=1)
    expected = croniter(schedule, previous_minute).get_next(dt.datetime)
    return expected == now


def run() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.info("Scheduler tick skipped because another run is active.")
                return

        settings = load_settings()
        schedule = str(settings.get("cron") or "").strip()
        if not schedule or not croniter.is_valid(schedule):
            logger.error("Invalid cron schedule in settings: %r", schedule)
            return

        now = dt.datetime.now().replace(second=0, microsecond=0)
        current_slot = now.isoformat()
        state = _load_state()
        if state.get("last_run_slot") == current_slot:
            return
        if not _due_now(schedule, now):
            return

        state["last_run_slot"] = current_slot
        _save_state(state)
        main()


if __name__ == "__main__":
    run()
