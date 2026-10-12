"""MLS daily recap preparation using Football Soccer API's free results endpoint.

This stage creates script drafts only. It does not generate audio/video or upload.
The free tier supplies recent results; goal-event details and standings may require
a higher plan, so the script never invents scorers or current table positions.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_BASE = "https://api.footballsoccerapi.com/v1"
TIMEZONE = os.getenv("REPORT_TIMEZONE", "America/Chicago")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
COUNTRY = os.getenv("MLS_COUNTRY", "USA")
LEAGUE_NAME = os.getenv("MLS_LEAGUE_NAME", "MLS").casefold()


class ProviderError(RuntimeError):
    pass


def api_get(path: str, params: dict[str, str] | None = None) -> dict:
    key = os.getenv("FOOTBALL_SOCCER_API_KEY", "").strip()
    if not key:
        raise ProviderError(
            "FOOTBALL_SOCCER_API_KEY is not configured in Railway Variables. "
            "No network requests were made."
        )
    response = requests.get(
        f"{API_BASE}/{path.lstrip('/')}",
        headers={"X-API-Key": key},
        params=params or {},
        timeout=25,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise ProviderError(f"Football Soccer API returned HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("Football Soccer API returned invalid JSON.") from exc
    if not isinstance(payload, dict) or "data" not in payload:
        raise ProviderError("Football Soccer API response did not contain a data field.")
    return payload


def fetch_yesterdays_results() -> list[dict]:
    local_now = datetime.now(ZoneInfo(TIMEZONE))
    report_date = os.getenv("REPORT_DATE", (local_now.date() - timedelta(days=1)).isoformat())
    payload = api_get(
        "matches",
        {
            "country": COUNTRY,
            "date_from": report_date,
            "date_to": report_date,
            "status": "finished",
            "limit": "1000",
        },
    )
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ProviderError("Unexpected results format from Football Soccer API.")
    return [
        match for match in data
        if (
            "mls" in str(match.get("league_name", "")).casefold()
            or "major league soccer" in str(match.get("league_name", "")).casefold()
        )
    ]


def build_match_summary(match: dict) -> dict:
    return {
        "match_id": match.get("match_id"),
        "date": match.get("kickoff_date"),
        "status": match.get("match_status"),
        "finished": match.get("match_status") in {"finished", "finished_after_extra_time"},
        "league": match.get("league_name"),
        "home_team": match.get("home_team_name", "Home team"),
        "away_team": match.get("away_team_name", "Away team"),
        "home_score": match.get("home_goals"),
        "away_score": match.get("away_goals"),
        "scorers": [],
        "scorer_note": "Goal-event details are not included in the free results response.",
    }


def create_script(matches: list[dict], report_date: str) -> dict:
    completed = [m for m in matches if m["finished"]]
    if not completed:
        return {
            "date": report_date,
            "status": "no_completed_matches",
            "short_script": "",
            "long_script": "",
            "note": (
                "No completed MLS matches were returned for yesterday. This can be a "
                "valid no-game day, or league coverage may need verification."
            ),
            "matches": [],
        }

    lines = []
    for m in completed:
        lines.append(
            f'{m["home_team"]} {m["home_score"]}, {m["away_team"]} {m["away_score"]}.'
        )
    match_text = " ".join(lines)
    short_script = (
        f"Here is your MLS score recap for {report_date}. {match_text} "
        "These are the final scores available from our data provider. Follow for more MLS updates."
    )
    long_script = (
        f"Welcome to your MLS recap for {report_date}. "
        f"{match_text} We are reporting verified final scores only in this version. "
        "Goal scorers and standings will be added when we have confirmed access to those details. "
        "Thanks for watching."
    )
    return {
        "date": report_date,
        "status": "draft_ready",
        "completed_match_count": len(completed),
        "short_script": short_script,
        "long_script": long_script,
        "matches": completed,
        "note": "Score-only template draft; review before narration or publication.",
    }


def main() -> int:
    local_now = datetime.now(ZoneInfo(TIMEZONE))
    report_date = os.getenv(
        "REPORT_DATE", (local_now.date() - timedelta(days=1)).isoformat()
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"MLS recap preparation | report date: {report_date} | source: Football Soccer API")

    try:
        results = fetch_yesterdays_results()
    except (ProviderError, requests.RequestException, ValueError) as exc:
        print(f"SAFE STOP: {exc}", file=sys.stderr)
        return 2

    summaries = [build_match_summary(match) for match in results]
    draft = create_script(summaries, report_date)
    draft["source"] = "Football Soccer API (free results endpoint)"
    draft["source_report_date"] = report_date
    draft["limitations"] = [
        "Free endpoint results are for the previous UTC day; report date is America/Chicago.",
        "Scorer event details and full standings are not claimed unless verified separately.",
        "No audio/video was rendered and no YouTube upload was attempted.",
    ]
    output_path = OUTPUT_DIR / f"mls_recap_{report_date}.json"
    output_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"MLS results returned: {len(summaries)}")
    print(f"Completed matches: {sum(1 for m in summaries if m['finished'])}")
    print(f"Draft output: {output_path}")
    print("No audio/video was rendered and no YouTube upload was attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
