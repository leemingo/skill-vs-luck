import os
import time
import json
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple, Optional


# -----------------------------------------------------------------------------
# How to set your API key in terminal before running this script:
#
#   export SPORTRADAR_API_KEY="your_real_api_key"
#   python get_sportsradar_data.py
# -----------------------------------------------------------------------------

API_KEY = os.getenv("SPORTRADAR_API_KEY")
if not API_KEY:
    raise ValueError("SPORTRADAR_API_KEY is not set")

ACCESS_LEVEL = "trial"
LANG = "en"
SPORTS = ["tabletennis", "badminton"]

MAX_RETRIES = 5
REQUEST_INTERVAL_SEC = 1.3
BACKOFF_BASE_SEC = 2.0
TIMEOUT_SEC = 30
DEFAULT_OUTPUT_ROOT = Path("/data2/MHL/skill-vs-luck/")

class SportradarClient:
    def __init__(self, api_key: str, sport: str):
        self.api_key = api_key
        self.sport = sport
        self.base_url = f"https://api.sportradar.com/{sport}/{ACCESS_LEVEL}/v2/{LANG}"

        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "x-api-key": api_key,
        })

    def get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            time.sleep(REQUEST_INTERVAL_SEC)

            try:
                resp = self.session.get(url, timeout=TIMEOUT_SEC)
            except requests.RequestException as e:
                last_error = e
                sleep_sec = BACKOFF_BASE_SEC * (2 ** attempt)
                print(
                    f"[NETWORK RETRY] sport={self.sport} "
                    f"attempt={attempt + 1}/{MAX_RETRIES + 1} "
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
                    f"[RATE LIMIT] sport={self.sport} "
                    f"attempt={attempt + 1}/{MAX_RETRIES + 1} "
                    f"url={url} sleep={sleep_sec:.1f}s"
                )
                time.sleep(sleep_sec)
                continue

            raise requests.HTTPError(
                f"GET {url} failed: {resp.status_code}\n{resp.text}"
            )

        if last_error is not None:
            raise requests.HTTPError(
                f"GET {url} failed after retries due to network error: {last_error}"
            )

        raise requests.HTTPError(
            f"GET {url} failed after retries due to 429 Too Many Requests"
        )

    def get_competitions(self) -> Dict[str, Any]:
        return self.get("/competitions.json")

    def get_competition_seasons(self, competition_id: str) -> Dict[str, Any]:
        return self.get(f"/competitions/{competition_id}/seasons.json")

    def get_season_summaries(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/summaries.json")


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_directory(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def safe_resource_name(resource_id: str) -> str:
    return resource_id.replace(":", "_").replace("/", "_")


def build_storage_paths(output_root: Path, sport: str) -> Dict[str, Path]:
    root = output_root / sport
    raw_dir = root / "raw"
    metadata_dir = root / "metadata"
    return {
        "root": root,
        "raw": raw_dir,
        "metadata": metadata_dir,
        "competitions": raw_dir / "competitions.json",
        "summary": metadata_dir / f"{sport}_access_summary.json",
    }


def extract_competitions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    comps = payload.get("competitions", [])
    if isinstance(comps, list):
        return [c for c in comps if isinstance(c, dict)]
    return []


def extract_seasons(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = []

    if isinstance(payload.get("seasons"), list):
        candidates.extend(payload["seasons"])

    if isinstance(payload.get("season"), list):
        candidates.extend(payload["season"])

    for value in payload.values():
        if isinstance(value, list):
            for item in value:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item["id"].startswith("sr:season:")
                ):
                    candidates.append(item)

    seen = set()
    result = []
    for item in candidates:
        sid = item.get("id")
        if sid and sid not in seen:
            seen.add(sid)
            result.append(item)
    return result


def walk_collect_sport_event_ids(obj: Any, out: Set[str]) -> None:
    if isinstance(obj, dict):
        sport_event = obj.get("sport_event")
        if isinstance(sport_event, dict):
            sid = sport_event.get("id")
            if isinstance(sid, str) and sid.startswith("sr:sport_event:"):
                out.add(sid)

        sid = obj.get("id")
        if isinstance(sid, str) and sid.startswith("sr:sport_event:"):
            out.add(sid)

        for v in obj.values():
            walk_collect_sport_event_ids(v, out)

    elif isinstance(obj, list):
        for item in obj:
            walk_collect_sport_event_ids(item, out)


def save_sport_snapshot(
    api_key: str,
    sport: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> Tuple[int, int, int, List[Dict[str, Any]]]:
    client = SportradarClient(api_key=api_key, sport=sport)
    storage_paths = build_storage_paths(output_root, sport)

    competitions_payload = client.get_competitions()
    write_json(storage_paths["competitions"], competitions_payload)
    competitions = extract_competitions(competitions_payload)

    unique_competition_ids: Set[str] = set()
    unique_season_ids: Set[str] = set()
    unique_sport_event_ids: Set[str] = set()

    details: List[Dict[str, Any]] = []
    failed_seasons: List[Dict[str, Any]] = []

    for comp in competitions:
        competition_id = comp.get("id")
        competition_name = comp.get("name")

        if not competition_id:
            continue

        unique_competition_ids.add(competition_id)

        competition_result = {
            "sport": sport,
            "competition_id": competition_id,
            "competition_name": competition_name,
            "season_count": 0,
            "match_count": 0,
            "season_ids": [],
            "error": None,
        }

        try:
            seasons_payload = client.get_competition_seasons(competition_id)
            competition_file = (
                storage_paths["raw"]
                / "competitions"
                / safe_resource_name(competition_id)
                / "seasons.json"
            )
            write_json(competition_file, seasons_payload)
            seasons = extract_seasons(seasons_payload)
        except Exception as e:
            competition_result["error"] = f"season fetch failed: {e}"
            details.append(competition_result)
            continue

        local_match_ids: Set[str] = set()

        for season in seasons:
            season_id = season.get("id")
            if not season_id:
                continue

            unique_season_ids.add(season_id)
            competition_result["season_ids"].append(season_id)

            try:
                summaries_payload = client.get_season_summaries(season_id)
                season_file = (
                    storage_paths["raw"]
                    / "seasons"
                    / safe_resource_name(season_id)
                    / "summaries.json"
                )
                write_json(season_file, summaries_payload)

                before_global = len(unique_sport_event_ids)
                before_local = len(local_match_ids)

                walk_collect_sport_event_ids(summaries_payload, unique_sport_event_ids)
                walk_collect_sport_event_ids(summaries_payload, local_match_ids)

                after_global = len(unique_sport_event_ids)
                after_local = len(local_match_ids)

                print(
                    f"[OK] sport={sport} competition={competition_id} "
                    f"season={season_id} "
                    f"added_matches_global={after_global - before_global} "
                    f"added_matches_local={after_local - before_local}"
                )
            except Exception as e:
                failed_seasons.append({
                    "sport": sport,
                    "competition_id": competition_id,
                    "competition_name": competition_name,
                    "season_id": season_id,
                    "error": str(e),
                })
                print(
                    f"[WARN] sport={sport} competition={competition_id} "
                    f"season summaries failed: {season_id} -> {e}"
                )

        competition_result["season_count"] = len(competition_result["season_ids"])
        competition_result["match_count"] = len(local_match_ids)
        details.append(competition_result)

    write_json(
        storage_paths["summary"],
        {
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "sport": sport,
            "base_url": client.base_url,
            "competition_count": len(unique_competition_ids),
            "season_count": len(unique_season_ids),
            "match_count": len(unique_sport_event_ids),
            "failed_seasons": failed_seasons,
            "details": details,
        },
    )

    return (
        len(unique_competition_ids),
        len(unique_season_ids),
        len(unique_sport_event_ids),
        details,
    )


def save_all_sports_snapshot(
    api_key: str,
    sports: List[str] = SPORTS,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> Dict[str, Any]:
    all_results: Dict[str, Any] = {
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "sports": {},
    }

    total_competitions = 0
    total_seasons = 0
    total_matches = 0

    for sport in sports:
        print("\n" + "=" * 80)
        print(f"Collecting: {sport}")
        print("=" * 80)

        try:
            competition_count, season_count, match_count, details = save_sport_snapshot(
                api_key=api_key,
                sport=sport,
                output_root=output_root,
            )

            all_results["sports"][sport] = {
                "status": "success",
                "competition_count": competition_count,
                "season_count": season_count,
                "match_count": match_count,
                "details": details,
            }

            total_competitions += competition_count
            total_seasons += season_count
            total_matches += match_count

        except Exception as e:
            all_results["sports"][sport] = {
                "status": "failed",
                "error": str(e),
                "competition_count": 0,
                "season_count": 0,
                "match_count": 0,
                "details": [],
            }
            print(f"[SPORT FAILED] sport={sport} error={e}")

    all_results["total_competition_count"] = total_competitions
    all_results["total_season_count"] = total_seasons
    all_results["total_match_count"] = total_matches

    write_json(output_root / "combined_summary.json", all_results)
    return all_results


if __name__ == "__main__":
    result = save_all_sports_snapshot(API_KEY)

    print("\n" + "=" * 80)
    print("Combined Access Scope Summary")
    print("=" * 80)

    for sport, sport_result in result["sports"].items():
        if sport_result["status"] == "success":
            print(
                f"{sport}: competitions={sport_result['competition_count']}, "
                f"seasons={sport_result['season_count']}, "
                f"matches={sport_result['match_count']}"
            )
        else:
            print(f"{sport}: failed - {sport_result['error']}")

    print("-" * 80)
    print(f"Total competitions: {result['total_competition_count']}")
    print(f"Total seasons: {result['total_season_count']}")
    print(f"Total matches: {result['total_match_count']}")
    print(f"Saved raw data under: {DEFAULT_OUTPUT_ROOT}")
    print(f"Combined summary: {DEFAULT_OUTPUT_ROOT / 'combined_summary.json'}")