from __future__ import annotations

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from croniter import croniter
from dotenv import load_dotenv
from requests import HTTPError, Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ENV_FILE = "/config/.env"
SETTINGS_FILE = "/config/settings.json"
SCHEDULER_STATE_FILE = "/app/runtime/scheduler_state.json"

load_dotenv(dotenv_path=ENV_FILE)


def get_env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def get_env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except ValueError:
        return default


def get_env_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return str(val).strip() if val is not None else default


def _bool_from_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _int_from_value(value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


LOG_FILE = f"/app/runtime/output_{time.strftime('%Y-%m-%d')}.log"
LOG_LEVEL = get_env_str("LOG_LEVEL", "INFO").upper()

logging.Formatter.converter = time.localtime
logging.basicConfig(
    filename=LOG_FILE,
    encoding="utf-8",
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("polishrr")


API_PATH = "/api/v3/"
DEFAULT_CRON_SCHEDULE = get_env_str("CRON_SCHEDULE", "0 * * * *")
DEFAULT_UPGRADE_TAG = get_env_str("UPGRADE_TAG", "upgrade-cf")
RECENT_UPGRADES: Dict[str, List[dict]] = {"radarr": [], "sonarr": []}

DEFAULT_TIMEOUT = float(get_env_str("HTTP_TIMEOUT_SECONDS", "15"))
MAX_RETRIES = get_env_int("HTTP_MAX_RETRIES", 3)
BACKOFF_FACTOR = float(get_env_str("HTTP_BACKOFF_FACTOR", "0.5"))
MAX_WORKERS = max(2, get_env_int("MAX_PARALLEL_REQUESTS", 8))


def _default_settings() -> dict:
    return {
        "cron": DEFAULT_CRON_SCHEDULE,
        "process_radarr": get_env_bool("PROCESS_RADARR", False),
        "process_sonarr": get_env_bool("PROCESS_SONARR", False),
        "num_movies": get_env_int("NUM_MOVIES_TO_UPGRADE", 1),
        "num_episodes": get_env_int("NUM_EPISODES_TO_UPGRADE", 1),
        "force_enabled": get_env_bool("FORCE_ENABLED", False),
    }


def _normalize_settings(raw: Optional[dict], base: Optional[dict] = None) -> dict:
    source = dict(base or _default_settings())
    raw = raw or {}
    source["cron"] = str(raw.get("cron", source["cron"])).strip() or source["cron"]
    if not croniter.is_valid(source["cron"]):
        raise ValueError(f"Invalid cron expression: {source['cron']}")
    source["process_radarr"] = _bool_from_value(raw.get("process_radarr", source["process_radarr"]), source["process_radarr"])
    source["process_sonarr"] = _bool_from_value(raw.get("process_sonarr", source["process_sonarr"]), source["process_sonarr"])
    source["num_movies"] = _int_from_value(raw.get("num_movies", source["num_movies"]), source["num_movies"], minimum=1, maximum=100)
    source["num_episodes"] = _int_from_value(raw.get("num_episodes", source["num_episodes"]), source["num_episodes"], minimum=1, maximum=100)
    source["force_enabled"] = _bool_from_value(raw.get("force_enabled", source["force_enabled"]), source["force_enabled"])
    return source


def _write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_env_overrides(settings: dict) -> None:
    updates = {
        "CRON_SCHEDULE": settings["cron"],
        "PROCESS_RADARR": "true" if settings["process_radarr"] else "false",
        "PROCESS_SONARR": "true" if settings["process_sonarr"] else "false",
        "NUM_MOVIES_TO_UPGRADE": str(settings["num_movies"]),
        "NUM_EPISODES_TO_UPGRADE": str(settings["num_episodes"]),
        "FORCE_ENABLED": "true" if settings["force_enabled"] else "false",
    }

    env_path = Path(ENV_FILE)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    new_lines: List[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in remaining:
            new_lines.append(f"{normalized_key}={remaining.pop(normalized_key)}")
        else:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def load_settings() -> dict:
    defaults = _default_settings()
    path = Path(SETTINGS_FILE)
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("settings file must contain a JSON object")
        return _normalize_settings(payload, defaults)
    except Exception as exc:
        logger.warning("Failed to load settings file %s: %s", SETTINGS_FILE, exc)
        return defaults


def save_settings(cfg: dict) -> dict:
    settings = _normalize_settings(cfg, load_settings())
    _write_json(SETTINGS_FILE, settings)
    _write_env_overrides(settings)
    return settings


@dataclass(frozen=True)
class RadarrConfig:
    enabled: bool
    base_url: str
    api_key: str
    num_to_upgrade: int


@dataclass(frozen=True)
class SonarrConfig:
    enabled: bool
    base_url: str
    api_key: str
    num_to_upgrade: int


@dataclass(frozen=True)
class AppConfig:
    radarr: RadarrConfig
    sonarr: SonarrConfig
    tag_name: str = DEFAULT_UPGRADE_TAG
    api_path: str = API_PATH


def load_app_config() -> AppConfig:
    settings = load_settings()
    return AppConfig(
        radarr=RadarrConfig(
            enabled=bool(settings["process_radarr"]),
            base_url=get_env_str("RADARR_URL"),
            api_key=get_env_str("RADARR_API_KEY"),
            num_to_upgrade=int(settings["num_movies"]),
        ),
        sonarr=SonarrConfig(
            enabled=bool(settings["process_sonarr"]),
            base_url=get_env_str("SONARR_URL"),
            api_key=get_env_str("SONARR_API_KEY"),
            num_to_upgrade=int(settings["num_episodes"]),
        ),
        tag_name=get_env_str("UPGRADE_TAG", DEFAULT_UPGRADE_TAG),
        api_path=get_env_str("ARR_API_PATH", API_PATH),
    )


class HttpClient:
    def __init__(self, headers: Optional[Dict[str, str]] = None) -> None:
        self.session: Session = requests.Session()
        retries = Retry(
            total=MAX_RETRIES,
            connect=MAX_RETRIES,
            read=MAX_RETRIES,
            status=MAX_RETRIES,
            backoff_factor=BACKOFF_FACTOR,
            allowed_methods=frozenset({"GET", "POST", "PUT", "DELETE"}),
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(headers or {})

    def _request(self, method: str, url: str, **kwargs) -> Any:
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
        response: Response = self.session.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        if not response.text:
            return {}
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, url: str, **kwargs) -> Any:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Any:
        return self._request("POST", url, **kwargs)

    def put(self, url: str, **kwargs) -> Any:
        return self._request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs) -> Any:
        return self._request("DELETE", url, **kwargs)


class ArrClient:
    def __init__(self, base_url: str, api_key: str, api_path: str) -> None:
        if not base_url:
            raise ValueError("Base URL must not be empty.")
        if not api_key:
            raise ValueError("API key must not be empty.")
        self.base = base_url.rstrip("/")
        self.api_path = api_path if api_path.startswith("/") else f"/{api_path}"
        self.client = HttpClient(headers={"Authorization": api_key})

    def _url(self, *parts: str) -> str:
        joined = "/".join(part.strip("/") for part in parts if part)
        return f"{self.base}{self.api_path}{joined}"

    def ensure_tag(self, label: str) -> int:
        tags = self.client.get(self._url("tag"))
        if isinstance(tags, dict) and "records" in tags:
            tags = tags["records"]
        match = next((tag["id"] for tag in tags if tag.get("label") == label), None)
        if match is not None:
            return int(match)
        created = self.client.post(self._url("tag"), json={"label": label})
        tag_id = int(created["id"])
        logger.info("Created new tag '%s' with id=%s", label, tag_id)
        return tag_id

    def quality_profiles_cutoff_scores(self) -> Dict[int, int]:
        profiles = self.client.get(self._url("qualityprofile"))
        return {int(profile["id"]): int(profile.get("cutoffFormatScore", 0)) for profile in profiles}

    def queue(self) -> List[dict]:
        payload = self.client.get(self._url("queue"))
        if isinstance(payload, dict) and "records" in payload:
            return list(payload["records"])
        if isinstance(payload, list):
            return payload
        logger.warning("Queue returned unexpected payload type: %s", type(payload))
        return []

    def command(self, name: str, **payload) -> Any:
        return self.client.post(self._url("command"), json={"name": name, **payload})


class Radarr(ArrClient):
    def movies(self) -> List[dict]:
        return self.client.get(self._url("movie"))

    def movie(self, movie_id: int) -> dict:
        return self.client.get(self._url("movie", str(movie_id)))

    def movie_file(self, file_id: int) -> dict:
        return self.client.get(self._url("moviefile", str(file_id)))

    def update_movie(self, movie: dict) -> dict:
        return self.client.put(self._url("movie", str(movie["id"])), json=movie)

    def delete_movie_file(self, file_id: int) -> None:
        self.client.delete(self._url("moviefile", str(file_id)))

    def search_movies(self, movie_ids: Iterable[int]) -> Any:
        return self.command("MoviesSearch", movieIds=list(movie_ids))


class Sonarr(ArrClient):
    def series_list(self) -> List[dict]:
        return self.client.get(self._url("series"))

    def series(self, series_id: int) -> dict:
        return self.client.get(self._url("series", str(series_id)))

    def update_series(self, series: dict) -> dict:
        return self.client.put(self._url("series", str(series["id"])), json=series)

    def episode_file_list(self, series_id: int) -> List[dict]:
        return self.client.get(self._url("episodefile"), params={"seriesId": series_id})

    def episode(self, episode_id: int) -> dict | List[dict]:
        return self.client.get(self._url("episode", str(episode_id)))

    def episodes_for_series(self, series_id: int) -> List[dict]:
        payload = self.client.get(self._url("episode"), params={"seriesId": series_id})
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return [payload]
        return []

    def episodes_by_file(self, episode_file_id: int) -> List[dict]:
        payload = self.client.get(self._url("episode"), params={"episodeFileId": episode_file_id})
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return [payload]
        return []

    def delete_episode_file(self, file_id: int) -> None:
        self.client.delete(self._url("episodefile", str(file_id)))

    def search_episodes(self, episode_ids: Iterable[int]) -> Any:
        return self.command("EpisodeSearch", episodeIds=list(episode_ids))


def parallel_map(func, items: Iterable[Any], max_workers: int = MAX_WORKERS) -> List[Any]:
    results: List[Any] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(func, item): item for item in items}
        for future in as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as exc:
                logger.warning("Parallel task failed for %r: %s", future_map[future], exc)
    return results


def _episode_label(episode: dict) -> str:
    season = episode.get("seasonNumber")
    episode_number = episode.get("episodeNumber")
    if season is None and episode_number is None:
        return str(episode.get("title") or f"Episode {episode.get('id', '-')}")
    if episode_number is None:
        return f"S{int(season):02d}"
    return f"S{int(season):02d}E{int(episode_number):02d}"


def _series_episode_map(episodes: Iterable[dict]) -> Dict[int, List[dict]]:
    mapping: Dict[int, List[dict]] = {}
    for episode in episodes:
        episode_file_id = episode.get("episodeFileId")
        if not episode_file_id:
            continue
        mapping.setdefault(int(episode_file_id), []).append(episode)
    return mapping


def _resolve_sonarr_reference(son: Sonarr, item_id: int) -> Tuple[List[dict], Optional[int]]:
    try:
        payload = son.episode(int(item_id))
        if isinstance(payload, list):
            episodes = payload
        else:
            episodes = [payload]
        episodes = [episode for episode in episodes if isinstance(episode, dict) and episode.get("id")]
        if episodes:
            file_id = episodes[0].get("episodeFileId")
            return episodes, int(file_id) if file_id else None
    except HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise

    episodes = son.episodes_by_file(int(item_id))
    episodes = [episode for episode in episodes if isinstance(episode, dict) and episode.get("id")]
    if episodes:
        return episodes, int(item_id)

    raise ValueError(f"Could not resolve Sonarr item {item_id}")


def _force_enabled() -> bool:
    return bool(load_settings().get("force_enabled", False))


def collect_radarr_upgrade_candidates(rad: Radarr, tag_id: int) -> Dict[int, Dict[str, Any]]:
    quality_scores = rad.quality_profiles_cutoff_scores()
    movies = [movie for movie in rad.movies() if movie.get("monitored") and movie.get("movieFileId")]

    def fetch_score(movie: dict) -> Tuple[int, int, str]:
        file_data = rad.movie_file(int(movie["movieFileId"]))
        return int(movie["id"]), int(file_data.get("customFormatScore", 0)), str(movie.get("title", ""))

    score_lookup = {
        movie_id: (score, title)
        for movie_id, score, title in parallel_map(fetch_score, movies)
    }

    candidates: Dict[int, Dict[str, Any]] = {}
    tagged = 0
    for movie in movies:
        movie_id = int(movie["id"])
        profile_id = int(movie.get("qualityProfileId"))
        cutoff = int(quality_scores.get(profile_id, 0))
        is_tagged = tag_id in (movie.get("tags", []) or [])
        if is_tagged:
            tagged += 1
        score_title = score_lookup.get(movie_id)
        if not score_title:
            continue
        current_score, title = score_title
        if current_score < cutoff and not is_tagged:
            candidates[movie_id] = {
                "title": title,
                "currentScore": current_score,
                "requiredScore": cutoff,
            }

    logger.info(
        "Radarr: movies=%s below_cutoff_unTagged=%s already_tagged=%s",
        len(movies), len(candidates), tagged,
    )
    return candidates


def run_radarr_upgrade(cfg: AppConfig) -> None:
    if not cfg.radarr.enabled:
        logger.info("Radarr disabled (PROCESS_RADARR=False)")
        return

    logger.info("Starting Radarr upgrade cycle...")
    rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
    tag_id = rad.ensure_tag(cfg.tag_name)
    movies = rad.movies()
    if not movies:
        logger.info("Radarr returned 0 movies.")
        return

    if all(tag_id in (movie.get("tags", []) or []) for movie in movies):
        logger.info("All movies have the upgrade tag. Removing to restart cycle...")
        for movie in movies:
            movie["tags"] = [tag for tag in movie.get("tags", []) if tag != tag_id]
            rad.update_movie(movie)
        logger.info("Upgrade tag removed from all movies.")
        return

    candidates = collect_radarr_upgrade_candidates(rad, tag_id)
    if not candidates:
        logger.info("No Radarr movies found for upgrade.")
        return

    selected_ids = random.sample(list(candidates.keys()), k=min(cfg.radarr.num_to_upgrade, len(candidates)))
    logger.info("Radarr selected movie IDs for upgrade: %s", selected_ids)

    RECENT_UPGRADES["radarr"].clear()
    for movie_id in selected_ids:
        movie = rad.movie(movie_id)
        movie["tags"] = sorted(set(movie.get("tags", [])) | {tag_id})
        rad.update_movie(movie)
        RECENT_UPGRADES["radarr"].append({"id": movie_id, "title": candidates[movie_id]["title"]})
        logger.info("Tagged movie '%s' with '%s'", candidates[movie_id]["title"], cfg.tag_name)

    rad.search_movies(selected_ids)
    logger.info("Triggered Radarr MoviesSearch.")


def collect_sonarr_upgrade_candidates(son: Sonarr, tag_id: int) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, dict]]:
    quality_scores = son.quality_profiles_cutoff_scores()
    series_list = son.series_list()
    candidates: Dict[int, Dict[str, Any]] = {}
    tagged_candidate_series: Dict[int, dict] = {}

    for series in series_list:
        series_id = int(series["id"])
        if int(series.get("statistics", {}).get("episodeFileCount", 0)) == 0:
            continue

        profile_id = int(series.get("qualityProfileId"))
        cutoff = int(quality_scores.get(profile_id, 0))
        is_series_tagged = tag_id in (series.get("tags", []) or [])

        try:
            episode_files = son.episode_file_list(series_id)
            series_episodes = son.episodes_for_series(series_id)
        except Exception as exc:
            logger.warning("Failed to fetch Sonarr data for series %s: %s", series_id, exc)
            continue

        episodes_by_file = _series_episode_map(series_episodes)
        series_has_below_cutoff = False

        for episode_file in episode_files:
            current_score = int(episode_file.get("customFormatScore", 0))
            if current_score >= cutoff:
                continue

            mapped_episodes = [
                episode
                for episode in episodes_by_file.get(int(episode_file["id"]), [])
                if episode.get("monitored")
            ]
            if not mapped_episodes:
                continue

            series_has_below_cutoff = True
            if is_series_tagged:
                continue

            for episode in mapped_episodes:
                episode_id = int(episode["id"])
                candidates[episode_id] = {
                    "id": episode_id,
                    "seriesId": series_id,
                    "series": str(series.get("title", "Series")),
                    "title": str(episode.get("title", "")),
                    "episode": _episode_label(episode),
                    "episodeFileId": int(episode_file["id"]),
                    "currentScore": current_score,
                    "requiredScore": cutoff,
                }

        if series_has_below_cutoff and is_series_tagged:
            tagged_candidate_series[series_id] = series

    logger.info(
        "Sonarr: series=%s below_cutoff_unTagged_episodes=%s tagged_candidate_series=%s",
        len(series_list), len(candidates), len(tagged_candidate_series),
    )
    return candidates, tagged_candidate_series


def run_sonarr_upgrade(cfg: AppConfig) -> None:
    if not cfg.sonarr.enabled:
        logger.info("Sonarr disabled (PROCESS_SONARR=False)")
        return

    logger.info("Starting Sonarr upgrade cycle...")
    son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
    tag_id = son.ensure_tag(cfg.tag_name)
    candidates, tagged_candidate_series = collect_sonarr_upgrade_candidates(son, tag_id)

    if not candidates:
        if tagged_candidate_series:
            logger.info("All Sonarr candidate series are currently tagged. Removing tags to restart cycle...")
            for series in tagged_candidate_series.values():
                series["tags"] = [tag for tag in series.get("tags", []) if tag != tag_id]
                son.update_series(series)
            logger.info("Removed Sonarr upgrade tag from %s series.", len(tagged_candidate_series))
        else:
            logger.info("No Sonarr episodes found for upgrade.")
        return

    selected_ids = random.sample(list(candidates.keys()), k=min(cfg.sonarr.num_to_upgrade, len(candidates)))
    logger.info("Sonarr selected episode IDs for upgrade: %s", selected_ids)

    RECENT_UPGRADES["sonarr"].clear()
    updated_series: set[int] = set()
    for episode_id in selected_ids:
        candidate = candidates[episode_id]
        series_id = int(candidate["seriesId"])
        if series_id not in updated_series:
            series = son.series(series_id)
            series["tags"] = sorted(set(series.get("tags", [])) | {tag_id})
            son.update_series(series)
            updated_series.add(series_id)

        RECENT_UPGRADES["sonarr"].append({
            "id": episode_id,
            "seriesId": series_id,
            "series": candidate["series"],
            "episode": candidate["episode"],
            "title": candidate["title"],
        })

    son.search_episodes(selected_ids)
    logger.info("Triggered Sonarr EpisodeSearch for %s episodes.", len(selected_ids))


def get_upgrade_status(detailed: bool = False) -> dict:
    cfg = load_app_config()
    status = {
        "radarr": {"total_below_cutoff": 0, "eligible_for_upgrade": 0, "items": []},
        "sonarr": {"total_below_cutoff": 0, "eligible_for_upgrade": 0, "items": []},
    }
    logger.info("Collecting upgrade statistics... detailed=%s", detailed)

    try:
        if cfg.radarr.enabled:
            rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
            tag_id = rad.ensure_tag(cfg.tag_name)
            quality_scores = rad.quality_profiles_cutoff_scores()
            movies = [movie for movie in rad.movies() if movie.get("monitored") and movie.get("movieFileId")]

            def fetch_tuple(movie: dict) -> Tuple[int, str, int, int, bool]:
                file_data = rad.movie_file(int(movie["movieFileId"]))
                score = int(file_data.get("customFormatScore", 0))
                cutoff = int(quality_scores.get(int(movie["qualityProfileId"]), 0))
                tagged = tag_id in (movie.get("tags", []) or [])
                return int(movie["id"]), str(movie.get("title", "")), score, cutoff, tagged

            for movie_id, title, score, cutoff, tagged in parallel_map(fetch_tuple, movies):
                if score < cutoff:
                    status["radarr"]["total_below_cutoff"] += 1
                    if not tagged:
                        status["radarr"]["eligible_for_upgrade"] += 1
                if detailed:
                    status["radarr"]["items"].append({
                        "id": movie_id,
                        "title": title,
                        "score": score,
                        "cutoff": cutoff,
                        "tagged": tagged,
                    })
    except Exception as exc:
        logger.exception("Error fetching Radarr stats:")
        status["radarr_error"] = str(exc)

    try:
        if cfg.sonarr.enabled:
            son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
            tag_id = son.ensure_tag(cfg.tag_name)
            quality_scores = son.quality_profiles_cutoff_scores()
            for series in son.series_list():
                if int(series.get("statistics", {}).get("episodeFileCount", 0)) == 0:
                    continue
                series_id = int(series["id"])
                cutoff = int(quality_scores.get(int(series.get("qualityProfileId")), 0))
                series_tagged = tag_id in (series.get("tags", []) or [])
                episode_files = son.episode_file_list(series_id)
                episodes_by_file = _series_episode_map(son.episodes_for_series(series_id))
                for episode_file in episode_files:
                    score = int(episode_file.get("customFormatScore", 0))
                    if score >= cutoff:
                        continue
                    for episode in episodes_by_file.get(int(episode_file["id"]), []):
                        if not episode.get("monitored"):
                            continue
                        status["sonarr"]["total_below_cutoff"] += 1
                        if not series_tagged:
                            status["sonarr"]["eligible_for_upgrade"] += 1
                        if detailed:
                            status["sonarr"]["items"].append({
                                "id": int(episode["id"]),
                                "series": series.get("title"),
                                "episode": _episode_label(episode),
                                "title": episode.get("title"),
                                "episodeFileId": int(episode_file["id"]),
                                "score": score,
                                "cutoff": cutoff,
                                "tagged": series_tagged,
                            })
    except Exception as exc:
        logger.exception("Error fetching Sonarr stats:")
        status["sonarr_error"] = str(exc)

    return status


def get_download_queue(tagged_only: bool = False) -> dict:
    if tagged_only:
        return {
            "radarr": RECENT_UPGRADES.get("radarr", []),
            "sonarr": RECENT_UPGRADES.get("sonarr", []),
        }

    cfg = load_app_config()
    data = {"radarr": [], "sonarr": []}

    try:
        if cfg.radarr.enabled:
            rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
            for item in rad.queue():
                if not isinstance(item, dict):
                    continue
                data["radarr"].append({
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "protocol": item.get("protocol"),
                    "size": round(float(item.get("size", 0)) / (1024 ** 3), 2),
                    "sizeleft": round(float(item.get("sizeleft", 0)) / (1024 ** 3), 2),
                    "timeleft": item.get("timeleft"),
                    "errorMessage": item.get("errorMessage"),
                    "indexer": item.get("indexer"),
                    "downloadId": item.get("downloadId"),
                })
    except Exception as exc:
        logger.exception("Radarr queue fetch failed:")
        data["radarr_error"] = str(exc)

    try:
        if cfg.sonarr.enabled:
            son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
            tag_id = son.ensure_tag(cfg.tag_name)
            series_cache: Dict[int, dict] = {}
            for item in son.queue():
                if not isinstance(item, dict):
                    continue
                series_id = item.get("seriesId")
                if not series_id:
                    continue
                series_id = int(series_id)
                if series_id not in series_cache:
                    try:
                        series_cache[series_id] = son.series(series_id)
                    except Exception as exc:
                        logger.warning("Failed to fetch Sonarr series %s: %s", series_id, exc)
                        series_cache[series_id] = {"title": f"Series {series_id}", "tags": []}

                series = series_cache[series_id]
                if tagged_only and tag_id not in (series.get("tags", []) or []):
                    continue

                episode_info = item.get("episode") if isinstance(item.get("episode"), dict) else {}
                data["sonarr"].append({
                    "series": series.get("title", "-"),
                    "episode": _episode_label({
                        "seasonNumber": item.get("seasonNumber"),
                        "episodeNumber": episode_info.get("episodeNumber"),
                        "title": episode_info.get("title"),
                    }),
                    "status": item.get("status", "-"),
                    "protocol": item.get("protocol", "-"),
                    "size": round(float(item.get("size", 0)) / (1024 ** 3), 2),
                    "sizeleft": round(float(item.get("sizeleft", 0)) / (1024 ** 3), 2),
                    "timeleft": item.get("timeleft", "-"),
                    "indexer": item.get("indexer", "-"),
                    "downloadId": item.get("downloadId"),
                })
    except Exception as exc:
        logger.exception("Sonarr queue fetch failed:")
        data["sonarr_error"] = str(exc)

    return data


def get_eligible_items() -> dict:
    cfg = load_app_config()
    output = {"radarr": [], "sonarr": []}

    try:
        if cfg.radarr.enabled:
            rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
            tag_id = rad.ensure_tag(cfg.tag_name)
            quality_scores = rad.quality_profiles_cutoff_scores()
            movies = [movie for movie in rad.movies() if movie.get("monitored") and movie.get("movieFileId")]

            def fetch_tuple(movie: dict) -> Tuple[int, str, int, int, bool]:
                file_data = rad.movie_file(int(movie["movieFileId"]))
                score = int(file_data.get("customFormatScore", 0))
                cutoff = int(quality_scores.get(int(movie["qualityProfileId"]), 0))
                tagged = tag_id in (movie.get("tags", []) or [])
                return int(movie["id"]), str(movie.get("title", "")), score, cutoff, tagged

            for movie_id, title, score, cutoff, tagged in parallel_map(fetch_tuple, movies):
                if score < cutoff and not tagged:
                    output["radarr"].append({
                        "id": movie_id,
                        "title": title,
                        "status": f"Score {score} / {cutoff}",
                        "score": score,
                        "cutoff": cutoff,
                    })
    except Exception as exc:
        logger.exception("Eligible Radarr fetch failed:")
        output["radarr_error"] = str(exc)

    try:
        if cfg.sonarr.enabled:
            son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
            tag_id = son.ensure_tag(cfg.tag_name)
            quality_scores = son.quality_profiles_cutoff_scores()
            for series in son.series_list():
                if int(series.get("statistics", {}).get("episodeFileCount", 0)) == 0:
                    continue
                if tag_id in (series.get("tags", []) or []):
                    continue
                series_id = int(series["id"])
                cutoff = int(quality_scores.get(int(series.get("qualityProfileId")), 0))
                episode_files = son.episode_file_list(series_id)
                episodes_by_file = _series_episode_map(son.episodes_for_series(series_id))
                for episode_file in episode_files:
                    score = int(episode_file.get("customFormatScore", 0))
                    if score >= cutoff:
                        continue
                    for episode in episodes_by_file.get(int(episode_file["id"]), []):
                        if not episode.get("monitored"):
                            continue
                        output["sonarr"].append({
                            "id": int(episode["id"]),
                            "series": series.get("title"),
                            "episode": _episode_label(episode),
                            "title": episode.get("title"),
                            "status": f"Score {score} / {cutoff}",
                            "score": score,
                            "cutoff": cutoff,
                            "episodeFileId": int(episode_file["id"]),
                        })
    except Exception as exc:
        logger.exception("Eligible Sonarr fetch failed:")
        output["sonarr_error"] = str(exc)

    return output


def get_recent_upgrades() -> dict:
    return RECENT_UPGRADES


def upgrade_single_item(target: str, item_id: int) -> dict:
    cfg = load_app_config()
    if target == "radarr":
        rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
        tag_id = rad.ensure_tag(cfg.tag_name)
        movie = rad.movie(int(item_id))
        movie["tags"] = sorted(set(movie.get("tags", [])) | {tag_id})
        rad.update_movie(movie)
        rad.search_movies([int(item_id)])
        logger.info("Triggered upgrade for Radarr movie '%s' (id=%s)", movie.get("title"), item_id)
        return {"ok": True}

    if target == "sonarr":
        son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
        tag_id = son.ensure_tag(cfg.tag_name)
        episodes, _episode_file_id = _resolve_sonarr_reference(son, int(item_id))
        monitored_episodes = [episode for episode in episodes if episode.get("monitored")]
        if not monitored_episodes:
            raise ValueError(f"No monitored Sonarr episode found for {item_id}")
        episode_ids = sorted({int(episode["id"]) for episode in monitored_episodes})
        series_id = int(monitored_episodes[0].get("seriesId") or 0)
        if not series_id:
            raise ValueError(f"No seriesId found for episode {item_id}")
        series = son.series(series_id)
        series["tags"] = sorted(set(series.get("tags", [])) | {tag_id})
        son.update_series(series)
        son.search_episodes(episode_ids)
        logger.info("Triggered upgrade for Sonarr episodes %s (series '%s')", episode_ids, series.get("title"))
        return {"ok": True}

    raise ValueError("Invalid target (expected 'radarr' or 'sonarr').")


def force_upgrade_single_item(target: str, item_id: int) -> dict:
    if not _force_enabled():
        raise PermissionError("Force mode is disabled in the settings.")

    cfg = load_app_config()
    if target == "radarr":
        rad = Radarr(cfg.radarr.base_url, cfg.radarr.api_key, cfg.api_path)
        movie = rad.movie(int(item_id))
        file_id = movie.get("movieFileId")
        if file_id:
            rad.delete_movie_file(int(file_id))
            logger.info("Deleted movie file for Radarr movie id=%s", item_id)
        rad.search_movies([int(item_id)])
        return {"ok": True}

    if target == "sonarr":
        son = Sonarr(cfg.sonarr.base_url, cfg.sonarr.api_key, cfg.api_path)
        episodes, episode_file_id = _resolve_sonarr_reference(son, int(item_id))
        monitored_episodes = [episode for episode in episodes if episode.get("monitored")]
        if not monitored_episodes:
            raise ValueError(f"No monitored Sonarr episode found for {item_id}")
        if episode_file_id:
            son.delete_episode_file(int(episode_file_id))
            logger.info("Deleted episode file %s for Sonarr item %s", episode_file_id, item_id)
        episode_ids = sorted({int(episode["id"]) for episode in monitored_episodes})
        son.search_episodes(episode_ids)
        return {"ok": True}

    raise ValueError("Invalid target (expected 'radarr' or 'sonarr').")


def main() -> None:
    cfg = load_app_config()
    try:
        run_radarr_upgrade(cfg)
    except Exception:
        logger.exception("Radarr upgrade cycle failed:")
    try:
        run_sonarr_upgrade(cfg)
    except Exception:
        logger.exception("Sonarr upgrade cycle failed:")
