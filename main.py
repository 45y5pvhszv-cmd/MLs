"""MLS daily recap preparation using Football Soccer API's free results endpoint.

This stage creates score-based narration and original graphic video drafts. It does not upload.
The free tier supplies recent results; goal-event details and standings may require
a higher plan, so the script never invents scorers or current table positions.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import edge_tts
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

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
    # The free "latest" endpoint returns recent settled results across UTC dates.
    # We filter each kickoff into the user's configured report timezone below.
    payload = api_get("results/latest", {"country": COUNTRY, "limit": "1000"})
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ProviderError("Unexpected results format from Football Soccer API.")
    # Match only the top-flight Major League Soccer competition. A substring
    # check like "mls" also matches MLS NEXT Pro, which must never enter this recap.
    official_mls_names = {"mls", "major league soccer"}
    return [
        match for match in data
        if str(match.get("league_name", "")).strip().casefold() in official_mls_names
    ]


def fetch_espn_results(report_date: str) -> list[dict]:
    """Fetch MLS scoreboard data from ESPN's public scoreboard endpoint."""
    # MLS kickoff times span UTC dates. Query the previous, requested, and
    # following UTC dates, then let the caller filter by local kickoff date.
    base_day = datetime.fromisoformat(report_date).date()
    events_by_id = {}
    for offset in (-1, 0, 1):
        day = (base_day + timedelta(days=offset)).strftime("%Y%m%d")
        response = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
            params={"dates": day, "limit": "100"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        for event in payload.get("events", []):
            if event.get("id"):
                events_by_id[event["id"]] = event
    results = []
    for event in events_by_id.values():
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        status = ((event.get("status") or {}).get("type") or {}).get("name", "")
        completed = bool(((event.get("status") or {}).get("type") or {}).get("completed"))
        date_value = event.get("date", "")
        try:
            kickoff_dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            kickoff_ts = kickoff_dt.timestamp()
        except (ValueError, TypeError):
            continue
        results.append({
            "match_id": event.get("id"),
            "kickoff_date": date_value[:10],
            "kickoff_utc": kickoff_ts,
            "match_status": "finished" if completed else status.casefold(),
            "league_name": "MLS",
            "home_team_name": (home.get("team") or {}).get("displayName", "Home team"),
            "away_team_name": (away.get("team") or {}).get("displayName", "Away team"),
            "home_goals": home.get("score"),
            "away_goals": away.get("score"),
            "source": "ESPN public scoreboard",
        })
    return results


def build_match_summary(match: dict) -> dict:
    return {
        "match_id": match.get("match_id"),
        "date": match.get("kickoff_date"),
        "kickoff_utc": match.get("kickoff_utc"),
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
        f"MLS fans, here are the final scores for {report_date}. {match_text} "
        "That is your quick league check-in. Which result surprised you? "
        "Follow for more MLS scores and match updates."
    )
    long_script = (
        f"Welcome to the MLS Final Whistle for {report_date}. "
        f"Let's get straight to the final scores. {match_text} "
        "This recap includes completed matches confirmed by our results provider. "
        "We are keeping this edition focused on verified scores rather than guessing at goal scorers "
        "or standings implications. Check back for the next league roundup, and tell us which match "
        "you want us to break down next."
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



def load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_scoreboard(matches: list[dict], report_date: str, path: Path, vertical: bool) -> None:
    """Create bold, original sports-social graphics without unlicensed team marks."""
    width, height = (720, 1280) if vertical else (1280, 720)
    image = Image.new("RGB", (width, height), "#06101f")
    draw = ImageDraw.Draw(image)
    # Neon diagonal bands and stadium-style light panels are original graphic assets.
    draw.polygon([(0, 0), (int(width*.68), 0), (int(width*.48), int(height*.18)), (0, int(height*.28))], fill="#0b2840")
    draw.polygon([(width, int(height*.45)), (width, height), (int(width*.58), height), (int(width*.8), int(height*.74))], fill="#0b263b")
    draw.rectangle((0, 0, int(width*.018), height), fill="#31e0b1")
    margin = int(width * 0.065)
    title_font = load_font(int(width * (0.066 if vertical else 0.052)), True)
    sub_font = load_font(int(width * 0.024), True)
    team_font = load_font(int(width * (0.027 if vertical else 0.022)), True)
    score_font = load_font(int(width * (0.043 if vertical else 0.036)), True)
    small_font = load_font(int(width * 0.017), True)
    draw.text((margin, int(height*.055)), "THE MLS", font=title_font, fill="#FFFFFF", stroke_width=1, stroke_fill="#31e0b1")
    draw.text((margin, int(height*.055)+int(width*.075)), "FINAL WHISTLE  /  " + report_date, font=sub_font, fill="#31e0b1")
    # Accent divider gives the card a sports broadcast hierarchy.
    draw.rounded_rectangle((margin, int(height*.18), width-margin, int(height*.187)), radius=5, fill="#31e0b1")
    y = int(height * (0.225 if vertical else 0.235))
    max_rows = min(len(matches), 8 if vertical else 5)
    row_h = int(height * (0.078 if vertical else 0.12))
    for idx, match in enumerate(matches[:max_rows], start=1):
        bottom = y + row_h - int(height*.012)
        draw.rounded_rectangle((margin, y, width-margin, bottom), radius=22, fill="#10283e", outline="#24475d", width=2)
        draw.rounded_rectangle((margin, y, margin+int(width*.012), bottom), radius=5, fill="#31e0b1" if idx == 1 else "#426b83")
        left = margin + int(width*.032)
        draw.text((left, y+int(row_h*.13)), str(match.get("home_team","Home"))[:24], font=team_font, fill="#FFFFFF")
        draw.text((left, y+int(row_h*.56)), str(match.get("away_team","Away"))[:24], font=team_font, fill="#a8bdce")
        score = f'{match.get("home_score","-")}  -  {match.get("away_score","-")}'
        box = draw.textbbox((0,0), score, font=score_font)
        draw.text((width-margin-int(width*.035)-(box[2]-box[0]), y+int(row_h*.31)), score, font=score_font, fill="#31e0b1")
        y += row_h
    footer_y = height - margin*1.8
    draw.text((margin, footer_y), "SCORES. STORIES. SOCCER.", font=small_font, fill="#FFFFFF")
    draw.text((margin, footer_y+int(height*.025)), "Independent MLS coverage  •  Verified final scores", font=small_font, fill="#a8bdce")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=95)


async def create_narration(text: str, path: Path) -> None:
    voice = os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(path))


def render_mp4(image_path: Path, audio_path: Path, output_path: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    width, height = Image.open(image_path).size
    # Gentle push-in plus a moving teal accent adds motion without unlicensed footage.
    motion_filter = (
        f"zoompan=z='min(zoom+0.0007,1.06)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={width}x{height}:fps=25,"
        f"drawbox=x='t*180':y={height-12}:w=150:h=6:"
        "color=0x31e0b1@0.95:t=fill"
    )
    cmd = [
        ffmpeg, "-y", "-loop", "1", "-framerate", "25", "-i", str(image_path),
        "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
        "-vf", motion_filter,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        "-threads", "2", "-filter_threads", "1",
        "-shortest", "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode:
        diagnostic = "\n".join(
            line for line in result.stderr.splitlines()
            if "Error" in line or "error" in line or "failed" in line.lower()
            or "No such" in line or "Invalid" in line or "Conversion" in line
        )
        raise RuntimeError(
            "FFmpeg video render failed (exit " + str(result.returncode) + "): "
            + (diagnostic[-1200:] or result.stderr[-1200:])
        )


def render_videos(draft: dict, output_dir: Path) -> list[str]:
    if draft.get("status") != "draft_ready":
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    date = draft["date"]
    assets = output_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    short_audio = assets / f"short_{date}.mp3"
    long_audio = assets / f"long_{date}.mp3"
    short_image = assets / f"short_{date}.jpg"
    long_image = assets / f"long_{date}.jpg"
    short_video = output_dir / f"mls_short_{date}_v3.mp4"
    long_video = output_dir / f"mls_recap_{date}_v3.mp4"
    asyncio.run(create_narration(draft["short_script"], short_audio))
    asyncio.run(create_narration(draft["long_script"], long_audio))
    render_scoreboard(draft["matches"], date, short_image, True)
    render_scoreboard(draft["matches"], date, long_image, False)
    render_mp4(short_image, short_audio, short_video)
    render_mp4(long_image, long_audio, long_video)
    return [str(short_video), str(long_video)]


def main(report_date_override: str | None = None) -> int:
    # The scheduled run summarizes yesterday's local MLS slate. Explicit REPORT_DATE
    # remains available for manual testing, and dates are always filtered locally.
    local_today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    report_date = os.getenv("REPORT_DATE") or report_date_override or local_today
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"MLS recap preparation | report date: {report_date} | source: ESPN scoreboard with fallback")

    try:
        provider_results = fetch_espn_results(report_date)
        source_name = "ESPN public MLS scoreboard"
        print(f"ESPN scoreboard matches for requested date: {len(provider_results)}")
        if not provider_results:
            print("ESPN returned no events; checking configured results provider.")
            provider_results = fetch_yesterdays_results()
            source_name = "Football Soccer API (fallback)"
    except (requests.RequestException, ValueError, KeyError) as espn_exc:
        print(f"ESPN scoreboard unavailable: {espn_exc}")
        try:
            provider_results = fetch_yesterdays_results()
            source_name = "Football Soccer API (fallback)"
        except (ProviderError, requests.RequestException, ValueError) as exc:
            print(f"SAFE STOP: ESPN and fallback provider failed: {exc}", file=sys.stderr)
            return 2
    except ProviderError as exc:
        print(f"SAFE STOP: {exc}", file=sys.stderr)
        return 2

    # Provider kickoff_date is a UTC date, which can differ from the date fans
    # see locally. Use kickoff_utc as the authority and convert it to the
    # configured report timezone before selecting the day's games.
    report_day = datetime.fromisoformat(report_date).date()
    results = []
    for match in provider_results:
        kickoff = match.get("kickoff_utc")
        try:
            kickoff_timestamp = float(kickoff)
            local_kickoff_day = datetime.fromtimestamp(
                kickoff_timestamp, timezone.utc
            ).astimezone(ZoneInfo(TIMEZONE)).date()
        except (TypeError, ValueError, OSError, OverflowError):
            # Fail closed rather than accidentally mix in a match with an
            # unknown kickoff date.
            continue
        if local_kickoff_day == report_day:
            results.append(match)
    skipped = len(provider_results) - len(results)
    print(f"Provider MLS matches before date filter: {len(provider_results)}")
    print(f"Matches for report date {report_date}: {len(results)}")
    print(f"Matches excluded from other dates: {skipped}")

    summaries = [build_match_summary(match) for match in results]
    draft = create_script(summaries, report_date)
    draft["source"] = source_name
    draft["source_report_date"] = report_date
    draft["limitations"] = [
        "Matches are filtered by kickoff timestamp in the configured report timezone.",
        "Scorer event details and full standings are not claimed unless verified separately.",
        "Original scoreboard graphics only; no match footage or team logos are used.",
        "YouTube upload is not configured and was not attempted.",
    ]
    output_path = OUTPUT_DIR / f"mls_recap_{report_date}.json"
    output_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"MLS results returned: {len(summaries)}")
    print(f"Completed matches: {sum(1 for m in summaries if m['finished'])}")
    print(f"Draft output: {output_path}")
    if draft.get("status") == "draft_ready" and os.getenv("ENABLE_VIDEO_RENDER", "true").casefold() == "true":
        try:
            videos = render_videos(draft, OUTPUT_DIR)
            for video in videos:
                print(f"Rendered video: {video}")
        except Exception as exc:
            print(f"VIDEO RENDER FAILED: {exc}", file=sys.stderr)
            return 3
    else:
        print("Video render skipped (no completed matches or ENABLE_VIDEO_RENDER=false).")
    print("YouTube upload is not configured; nothing was published.")
    return 0


def scheduled_daily_runner() -> None:
    """Run the prior local calendar day's recap every morning at 5:00 AM."""
    zone = ZoneInfo(TIMEZONE)
    while True:
        now = datetime.now(zone)
        next_run = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = max(1, (next_run - now).total_seconds())
        print(f"Next automatic MLS recap scheduled for {next_run.isoformat()}")
        time.sleep(wait_seconds)
        report_date = (datetime.now(zone).date() - timedelta(days=1)).isoformat()
        print(f"Starting scheduled MLS recap for {report_date}")
        try:
            main(report_date)
        except Exception as exc:
            print(f"SCHEDULED RECAP FAILED for {report_date}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    # Serve generated files continuously; generate yesterday's recap at startup,
    # then repeat every morning. A no-game day produces no video files.
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    OUTPUT_DIR = OUTPUT_DIR.resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", "8080"))
    handler = partial(SimpleHTTPRequestHandler, directory=str(OUTPUT_DIR))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    zone = ZoneInfo(TIMEZONE)
    yesterday = (datetime.now(zone).date() - timedelta(days=1)).isoformat()
    worker = threading.Thread(target=main, args=(yesterday,), name="mls-recap-worker", daemon=True)
    scheduler = threading.Thread(target=scheduled_daily_runner, name="mls-daily-scheduler", daemon=True)
    worker.start()
    scheduler.start()
    print(f"Video file server ready on port {port}; daily recap generation is enabled")
    server.serve_forever()
