"""MLS daily recap preparation.

This stage fetches licensed match data and creates script drafts only.
It never generates audio/video or uploads to YouTube.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_BASE = "https://v3.football.api-sports.io"
MLS_LEAGUE_ID = os.getenv("MLS_LEAGUE_ID", "253")
TIMEZONE = os.getenv("REPORT_TIMEZONE", "America/Chicago")
SEASON = os.getenv("MLS_SEASON", str(datetime.now(ZoneInfo(TIMEZONE)).year))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))


class ProviderError(RuntimeError):
    pass


def api_get(path: str, params: dict[str, str]) -> dict:
    key = os.getenv("MLS_DATA_API_KEY", "").strip()
    if not key:
        raise ProviderError(
            "MLS_DATA_API_KEY is not configured. Add an API-Football key in Railway "
            "Variables; no network requests were made."
        )
    response = requests.get(
        f"{API_BASE}/{path.lstrip('/')}",
        headers={"x-apisports-key": key},
        params=params,
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise ProviderError(f"Data provider returned an error: {payload['errors']}")
    return payload


def fetch_yesterdays_fixtures(report_date: str) -> list[dict]:
    payload = api_get(
        "fixtures",
        {
            "date": report_date,
            "league": MLS_LEAGUE_ID,
            "season": SEASON,
            "timezone": TIMEZONE,
        },
    )
    return payload.get("response", [])


def fetch_events(fixture_id: int) -> list[dict]:
    payload = api_get("fixtures/events", {"fixture": str(fixture_id)})
    return payload.get("response", [])


def fetch_standings() -> list:
    payload = api_get(
        "standings",
        {"league": MLS_LEAGUE_ID, "season": SEASON},
    )
    response = payload.get("response", [])
    if not response:
        return []
    leagues = response[0].get("league", {}).get("standings", [])
    return leagues[0] if leagues else []


def goal_scorers(events: list[dict]) -> list[str]:
    scorers = []
    for event in events:
        if event.get("type") != "Goal":
            continue
        detail = (event.get("detail") or "").lower()
        if "missed penalty" in detail or "penalty shootout" in detail:
            continue
        player = (event.get("player") or {}).get("name")
        team = (event.get("team") or {}).get("name")
        minute = (event.get("time") or {}).get("elapsed")
        extra = (event.get("time") or {}).get("extra")
        if player and team:
            clock = f"{minute}'" if minute is not None else "time unlisted"
            if extra:
                clock = f"{minute}+{extra}'"
            scorers.append(f"{player} ({team}, {clock})")
    return scorers


def build_match_summary(fixture: dict, events: list[dict]) -> dict:
    f = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})
    home = (teams.get("home") or {}).get("name", "Home team")
    away = (teams.get("away") or {}).get("name", "Away team")
    home_score = goals.get("home")
    away_score = goals.get("away")
    status = (f.get("status") or {}).get("short", "")
    finished = status in {"FT", "AET", "PEN"}
    return {
        "fixture_id": f.get("id"),
        "date": f.get("date"),
        "status": status,
        "finished": finished,
        "home_team": home,
        "away_team": away,
        "home_score": home_score,
        "away_score": away_score,
        "scorers": goal_scorers(events),
    }


def create_script(matches: list[dict], report_date: str) -> dict:
    completed = [m for m in matches if m["finished"]]
    if not completed:
        return {
            "date": report_date,
            "status": "no_completed_matches",
            "short_script": "",
            "long_script": "",
            "note": "No completed MLS matches were found for this report date.",
        }

    lines = []
    for m in completed:
        lines.append(
            f'{m["home_team"]} {m["home_score"]}, {m["away_team"]} {m["away_score"]}.'
        )
        if m["scorers"]:
            lines.append("Goal scorers: " + "; ".join(m["scorers"]) + ".")
        elif m["home_score"] or m["away_score"]:
            lines.append("Scorer details were not returned by the provider.")
        else:
            lines.append("This match finished scoreless.")

    match_text = " ".join(lines)
    short_script = (
        f"Here is your MLS recap for {report_date}. {match_text} "
        "Follow for more MLS results and match updates."
    )
    long_script = (
        f"Welcome to your MLS daily recap for {report_date}. "
        f"{match_text} These are the verified final scores and scorer details available "
        "from our data provider. Standings context and deeper tactical analysis will be "
        "added only when supported by verified statistics. Thanks for watching."
    )
    return {
        "date": report_date,
        "status": "draft_ready",
        "completed_match_count": len(completed),
        "short_script": short_script,
        "long_script": long_script,
        "matches": completed,
        "note": "Template-based draft only; review before narration or publication.",
    }


def main() -> int:
    local_now = datetime.now(ZoneInfo(TIMEZONE))
    report_date = os.getenv(
        "REPORT_DATE", (local_now.date() - timedelta(days=1)).isoformat()
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"MLS recap preparation | report date: {report_date} | season: {SEASON}")

    try:
        fixtures = fetch_yesterdays_fixtures(report_date)
        summaries = []
        for fixture in fixtures:
            fixture_id = (fixture.get("fixture") or {}).get("id")
            if not fixture_id:
                continue
            events = fetch_events(int(fixture_id))
            summaries.append(build_match_summary(fixture, events))
        standings = fetch_standings()
    except (ProviderError, requests.RequestException, ValueError) as exc:
        print(f"SAFE STOP: {exc}", file=sys.stderr)
        return 2

    draft = create_script(summaries, report_date)
    draft["season"] = SEASON
    draft["standings_available"] = bool(standings)
    draft["standings"] = standings
    draft["source"] = "API-Football / API-Sports; verify current terms for your use"
    output_path = OUTPUT_DIR / f"mls_recap_{report_date}.json"
    output_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Fixtures retrieved: {len(summaries)}")
    print(f"Completed matches: {sum(1 for m in summaries if m['finished'])}")
    print(f"Draft output: {output_path}")
    print("No audio/video was rendered and no YouTube upload was attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
