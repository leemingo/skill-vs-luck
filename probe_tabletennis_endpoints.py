import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError as UrllibHTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import requests
except ModuleNotFoundError:
    requests = None


ACCESS_LEVEL = "trial"
LANG = "en"
SPORT = "tabletennis"

MAX_RETRIES = 5
REQUEST_INTERVAL_SEC = 1.3
BACKOFF_BASE_SEC = 2.0
TIMEOUT_SEC = 30

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / SPORT
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "endpoint_probes" / SPORT


class SportradarHTTPError(Exception):
    pass


def load_default_api_key() -> Optional[str]:
    api_key = os.getenv("SPORTRADAR_API_KEY")
    if api_key:
        return api_key

    source_path = PROJECT_ROOT / "get_sportsradar_data.py"
    if not source_path.exists():
        return None

    source = source_path.read_text(encoding="utf-8")
    match = re.search(r'API_KEY\s*=\s*"([^"]+)"', source)
    if match:
        return match.group(1)

    return None


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def safe_resource_name(resource_id: str) -> str:
    return resource_id.replace(":", "_").replace("/", "_")


def decode_saved_resource_name(name: str) -> str:
    parts = name.split("_", 2)
    if len(parts) == 3:
        return f"{parts[0]}:{parts[1]}:{parts[2]}"
    return name


class SportradarTableTennisClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = (
            f"https://api.sportradar.com/{SPORT}/{ACCESS_LEVEL}/v2/{LANG}"
        )
        self.headers = {
            "accept": "application/json",
            "x-api-key": api_key,
        }
        self.session = None
        if requests is not None:
            self.session = requests.Session()
            self.session.headers.update(self.headers)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            time.sleep(REQUEST_INTERVAL_SEC)

            if self.session is not None:
                try:
                    resp = self.session.get(url, timeout=TIMEOUT_SEC)
                except Exception as e:
                    last_error = e
                    sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)
                    print(
                        f"[NETWORK RETRY] attempt={attempt + 1}/{MAX_RETRIES + 1} "
                        f"url={url} sleep={sleep_sec:.1f}s error={e}"
                    )
                    time.sleep(sleep_sec)
                    continue

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            sleep_sec = float(retry_after)
                        except ValueError:
                            sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)
                    else:
                        sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)

                    print(
                        f"[RATE LIMIT] attempt={attempt + 1}/{MAX_RETRIES + 1} "
                        f"url={url} sleep={sleep_sec:.1f}s"
                    )
                    time.sleep(sleep_sec)
                    continue

                raise SportradarHTTPError(
                    f"GET {url} failed: {resp.status_code}\n{resp.text}"
                )

            request = Request(url, headers=self.headers)
            try:
                with urlopen(request, timeout=TIMEOUT_SEC) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except UrllibHTTPError as e:
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            sleep_sec = float(retry_after)
                        except ValueError:
                            sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)
                    else:
                        sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)

                    print(
                        f"[RATE LIMIT] attempt={attempt + 1}/{MAX_RETRIES + 1} "
                        f"url={url} sleep={sleep_sec:.1f}s"
                    )
                    time.sleep(sleep_sec)
                    continue

                body = e.read().decode("utf-8", errors="replace")
                raise SportradarHTTPError(f"GET {url} failed: {e.code}\n{body}")
            except URLError as e:
                last_error = e
                sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)
                print(
                    f"[NETWORK RETRY] attempt={attempt + 1}/{MAX_RETRIES + 1} "
                    f"url={url} sleep={sleep_sec:.1f}s error={e}"
                )
                time.sleep(sleep_sec)
                continue

        if last_error is not None:
            raise SportradarHTTPError(
                f"GET {url} failed after retries due to network error: {last_error}"
            )

        raise SportradarHTTPError(
            f"GET {url} failed after retries due to 429 Too Many Requests"
        )

    def get_competitions(self) -> Dict[str, Any]:
        return self.get("/competitions.json")

    def get_competition_info(self, competition_id: str) -> Dict[str, Any]:
        return self.get(f"/competitions/{competition_id}/info.json")

    def get_competition_seasons(self, competition_id: str) -> Dict[str, Any]:
        return self.get(f"/competitions/{competition_id}/seasons.json")

    def get_seasons(self) -> Dict[str, Any]:
        return self.get("/seasons.json")

    def get_season_info(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/info.json")

    def get_season_competitors(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/competitors.json")

    def get_season_links(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/stages_groups_cup_rounds.json")

    def get_season_probabilities(
        self,
        season_id: str,
        start: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = {"start": start} if start is not None else None
        return self.get(f"/seasons/{season_id}/probabilities.json", params=params)

    def get_season_standings(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/standings.json")

    def get_season_summaries(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/summaries.json")

    def get_daily_summaries(
        self,
        date_str: str,
        start: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = {"start": start} if start is not None else None
        return self.get(f"/schedules/{date_str}/summaries.json", params=params)

    def get_live_summaries(self) -> Dict[str, Any]:
        return self.get("/schedules/live/summaries.json")

    def get_live_timelines(self) -> Dict[str, Any]:
        return self.get("/schedules/live/timelines.json")

    def get_live_timelines_delta(self) -> Dict[str, Any]:
        return self.get("/schedules/live/timelines_delta.json")

    def get_sport_event_summary(self, sport_event_id: str) -> Dict[str, Any]:
        return self.get(f"/sport_events/{sport_event_id}/summary.json")

    def get_sport_event_timeline(self, sport_event_id: str) -> Dict[str, Any]:
        return self.get(f"/sport_events/{sport_event_id}/timeline.json")

    def get_sport_events_created(
        self,
        start: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = {"start": start} if start is not None else None
        return self.get("/sport_events/created.json", params=params)

    def get_sport_events_updated(
        self,
        start: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = {"start": start} if start is not None else None
        return self.get("/sport_events/updated.json", params=params)

    def get_sport_events_removed(self) -> Dict[str, Any]:
        return self.get("/sport_events/removed.json")

    def get_competitor_profile(self, competitor_id: str) -> Dict[str, Any]:
        return self.get(f"/competitors/{competitor_id}/profile.json")

    def get_competitor_summaries(self, competitor_id: str) -> Dict[str, Any]:
        return self.get(f"/competitors/{competitor_id}/summaries.json")

    def get_competitor_versus(
        self,
        competitor_id: str,
        competitor2_id: str,
    ) -> Dict[str, Any]:
        return self.get(
            f"/competitors/{competitor_id}/versus/{competitor2_id}/summaries.json"
        )

    def get_competitor_merge_mappings(self) -> Dict[str, Any]:
        return self.get("/competitors/merge_mappings.json")

    def get_rankings(self) -> Dict[str, Any]:
        return self.get("/rankings.json")


@dataclass
class MatchSample:
    sport_event_id: str
    season_id: str
    competition_id: str
    date_str: str
    competitor_ids: List[str]


@dataclass
class ProbeContext:
    competition_ids: List[str]
    season_ids: List[str]
    match_samples: List[MatchSample]
    competitor_ids: List[str]
    versus_pair: Optional[Tuple[str, str]]
    date_strs: List[str]


def load_sample_context(
    data_root: Path,
    sample_competitions: int = 2,
    sample_seasons: int = 2,
    sample_matches: int = 3,
) -> ProbeContext:
    metadata_path = data_root / "metadata" / "tabletennis_access_summary.json"
    summary = json.loads(metadata_path.read_text(encoding="utf-8"))

    competition_ids = [
        row["competition_id"]
        for row in summary.get("details", [])
        if row.get("competition_id")
    ][:sample_competitions]

    season_ids: List[str] = []
    for row in summary.get("details", []):
        for season_id in row.get("season_ids", []):
            if season_id not in season_ids:
                season_ids.append(season_id)
            if len(season_ids) >= sample_seasons:
                break
        if len(season_ids) >= sample_seasons:
            break

    match_samples: List[MatchSample] = []
    competitor_ids: List[str] = []
    versus_pair: Optional[Tuple[str, str]] = None
    date_strs: List[str] = []

    for summary_file in sorted((data_root / "raw" / "seasons").glob("*/summaries.json")):
        payload = json.loads(summary_file.read_text(encoding="utf-8"))
        for item in payload.get("summaries", []):
            sport_event = item.get("sport_event", {})
            context = sport_event.get("sport_event_context", {})
            competitors = sport_event.get("competitors", [])

            sport_event_id = sport_event.get("id")
            season_id = context.get("season", {}).get("id")
            competition_id = context.get("competition", {}).get("id")
            start_time = sport_event.get("start_time")

            ids_in_match = [
                competitor.get("id")
                for competitor in competitors
                if competitor.get("id")
            ]
            if not sport_event_id or not season_id or not competition_id or not start_time:
                continue

            date_str = start_time[:10]
            match_samples.append(
                MatchSample(
                    sport_event_id=sport_event_id,
                    season_id=season_id,
                    competition_id=competition_id,
                    date_str=date_str,
                    competitor_ids=ids_in_match,
                )
            )

            if date_str not in date_strs:
                date_strs.append(date_str)

            for competitor_id in ids_in_match:
                if competitor_id not in competitor_ids:
                    competitor_ids.append(competitor_id)

            if versus_pair is None and len(ids_in_match) >= 2:
                versus_pair = (ids_in_match[0], ids_in_match[1])

            if len(match_samples) >= sample_matches:
                break
        if len(match_samples) >= sample_matches:
            break

    if not season_ids:
        season_ids = [
            decode_saved_resource_name(path.parent.name)
            for path in sorted((data_root / "raw" / "seasons").glob("*/summaries.json"))[:sample_seasons]
        ]

    return ProbeContext(
        competition_ids=competition_ids,
        season_ids=season_ids,
        match_samples=match_samples[:sample_matches],
        competitor_ids=competitor_ids[: max(2, sample_matches)],
        versus_pair=versus_pair,
        date_strs=date_strs[:sample_matches],
    )


def summarize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "top_level_keys": list(payload.keys()),
    }
    for key, value in payload.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_keys"] = list(value.keys())[:20]
    return summary


def run_probe_call(
    output_dir: Path,
    endpoint_name: str,
    resource_label: str,
    fetcher: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    path = (
        output_dir
        / endpoint_name
        / f"{safe_resource_name(resource_label)}.json"
    )

    try:
        payload = fetcher()
        write_json(path, payload)
        result = {
            "endpoint": endpoint_name,
            "resource": resource_label,
            "status": "success",
            "saved_path": str(path),
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "payload_summary": summarize_payload(payload),
        }
        print(f"[OK] endpoint={endpoint_name} resource={resource_label}")
        return result
    except Exception as e:
        result = {
            "endpoint": endpoint_name,
            "resource": resource_label,
            "status": "failed",
            "saved_path": None,
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
        }
        print(f"[WARN] endpoint={endpoint_name} resource={resource_label} error={e}")
        return result


def build_probe_jobs(
    client: SportradarTableTennisClient,
    context: ProbeContext,
) -> List[Tuple[str, str, Callable[[], Dict[str, Any]]]]:
    jobs: List[Tuple[str, str, Callable[[], Dict[str, Any]]]] = [
        ("competitions", "all", client.get_competitions),
        ("seasons", "all", client.get_seasons),
        ("rankings", "all", client.get_rankings),
        ("competitor_merge_mappings", "all", client.get_competitor_merge_mappings),
        ("live_summaries", "all", client.get_live_summaries),
        ("live_timelines", "all", client.get_live_timelines),
        ("live_timelines_delta", "all", client.get_live_timelines_delta),
        ("sport_events_created", "all", client.get_sport_events_created),
        ("sport_events_updated", "all", client.get_sport_events_updated),
        ("sport_events_removed", "all", client.get_sport_events_removed),
    ]

    for competition_id in context.competition_ids:
        jobs.append((
            "competition_info",
            competition_id,
            lambda competition_id=competition_id: client.get_competition_info(competition_id),
        ))
        jobs.append((
            "competition_seasons",
            competition_id,
            lambda competition_id=competition_id: client.get_competition_seasons(competition_id),
        ))

    for season_id in context.season_ids:
        jobs.append((
            "season_info",
            season_id,
            lambda season_id=season_id: client.get_season_info(season_id),
        ))
        jobs.append((
            "season_competitors",
            season_id,
            lambda season_id=season_id: client.get_season_competitors(season_id),
        ))
        jobs.append((
            "season_links",
            season_id,
            lambda season_id=season_id: client.get_season_links(season_id),
        ))
        jobs.append((
            "season_probabilities",
            season_id,
            lambda season_id=season_id: client.get_season_probabilities(season_id),
        ))
        jobs.append((
            "season_standings",
            season_id,
            lambda season_id=season_id: client.get_season_standings(season_id),
        ))
        jobs.append((
            "season_summaries",
            season_id,
            lambda season_id=season_id: client.get_season_summaries(season_id),
        ))

    for match in context.match_samples:
        jobs.append((
            "sport_event_summary",
            match.sport_event_id,
            lambda sport_event_id=match.sport_event_id: client.get_sport_event_summary(sport_event_id),
        ))
        jobs.append((
            "sport_event_timeline",
            match.sport_event_id,
            lambda sport_event_id=match.sport_event_id: client.get_sport_event_timeline(sport_event_id),
        ))

    for date_str in context.date_strs:
        jobs.append((
            "daily_summaries",
            date_str,
            lambda date_str=date_str: client.get_daily_summaries(date_str),
        ))

    for competitor_id in context.competitor_ids:
        jobs.append((
            "competitor_profile",
            competitor_id,
            lambda competitor_id=competitor_id: client.get_competitor_profile(competitor_id),
        ))
        jobs.append((
            "competitor_summaries",
            competitor_id,
            lambda competitor_id=competitor_id: client.get_competitor_summaries(competitor_id),
        ))

    if context.versus_pair is not None:
        competitor_id, competitor2_id = context.versus_pair
        jobs.append((
            "competitor_versus",
            f"{competitor_id}__{competitor2_id}",
            lambda competitor_id=competitor_id, competitor2_id=competitor2_id: (
                client.get_competitor_versus(competitor_id, competitor2_id)
            ),
        ))

    return jobs


def run_tabletennis_endpoint_probe(
    api_key: str,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    sample_competitions: int = 2,
    sample_seasons: int = 2,
    sample_matches: int = 3,
) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / timestamp
    ensure_directory(run_dir)

    context = load_sample_context(
        data_root=data_root,
        sample_competitions=sample_competitions,
        sample_seasons=sample_seasons,
        sample_matches=sample_matches,
    )
    write_json(run_dir / "probe_context.json", asdict(context))

    client = SportradarTableTennisClient(api_key=api_key)
    jobs = build_probe_jobs(client, context)

    results = [
        run_probe_call(run_dir, endpoint_name, resource_label, fetcher)
        for endpoint_name, resource_label, fetcher in jobs
    ]

    success_count = sum(1 for result in results if result["status"] == "success")
    failed_results = [result for result in results if result["status"] == "failed"]

    summary = {
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "sample_context": asdict(context),
        "job_count": len(results),
        "success_count": success_count,
        "failure_count": len(failed_results),
        "failed_jobs": failed_results,
        "results": results,
    }
    write_json(run_dir / "probe_summary.json", summary)
    return summary


if __name__ == "__main__":
    api_key = load_default_api_key()
    api_key = "TJdbSPgkFnJ91mtqdFs6lbnGT1W6lx2ww0zxM6zo"
    if not api_key or api_key == "YOUR_API_KEY":
        raise ValueError("Set SPORTRADAR_API_KEY or provide a valid API key in get_sportsradar_data.py")

    summary = run_tabletennis_endpoint_probe(api_key=api_key)

    print("\n" + "=" * 80)
    print("Table Tennis Endpoint Probe Summary")
    print("=" * 80)
    print(f"Jobs: {summary['job_count']}")
    print(f"Success: {summary['success_count']}")
    print(f"Failed: {summary['failure_count']}")
    print(f"Output: {summary['run_dir']}")
