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
    # This endpoint is explicitly available on the free plan and uses the
    # provider's yesterday-in-UTC window. Do not query older dates on free tier.
    payload = api_get("results/yesterday", {"country": COUNTRY, "limit": "1000"})
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
    # 720p-class graphics reduce peak memory in the small Railway container.
    width, height = (720, 1280) if vertical else (1280, 720)
    image = Image.new("RGB", (width, height), "#071426")
    draw = ImageDraw.Draw(image)
    margin = int(width * 0.07)
    title_font = load_font(int(width * (0.062 if vertical else 0.047)), True)
    sub_font = load_font(int(width * 0.025), False)
    team_font = load_font(int(width * (0.027 if vertical else 0.022)), True)
    score_font = load_font(int(width * (0.038 if vertical else 0.032)), True)
    small_font = load_font(int(width * 0.018), False)
    draw.rounded_rectangle((margin, margin, width-margin, height-margin), radius=32, outline="#31C7A8", width=5)
    draw.text((margin*1.4, margin*1.35), "MLS DAILY RECAP", font=title_font, fill="#FFFFFF")
    draw.text((margin*1.4, margin*1.35 + int(width*.08)), report_date, font=sub_font, fill="#31C7A8")
    y = int(height * (0.24 if vertical else 0.28))
    max_rows = min(len(matches), 8 if vertical else 6)
    row_h = int(height * (0.075 if vertical else 0.095))
    for match in matches[:max_rows]:
        draw.rounded_rectangle((margin, y, width-margin, y+row_h-int(height*.012)), radius=18, fill="#10263E")
        left = margin + int(width*.025)
        draw.text((left, y+int(row_h*.12)), str(match.get("home_team","Home"))[:24], font=team_font, fill="#FFFFFF")
        draw.text((left, y+int(row_h*.55)), str(match.get("away_team","Away"))[:24], font=team_font, fill="#A9BCD0")
        score = f'{match.get("home_score","-")}  -  {match.get("away_score","-")}'
        box = draw.textbbox((0,0), score, font=score_font)
        draw.text((width-margin-int(width*.03)-(box[2]-box[0]), y+int(row_h*.28)), score, font=score_font, fill="#31C7A8")
        y += row_h
    footer_y = height - margin*2
    draw.text((margin*1.4, footer_y), "Scores only • No unlicensed match footage", font=small_font, fill="#A9BCD0")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=95)


async def create_narration(text: str, path: Path) -> None:
    voice = os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(path))


def render_mp4(image_path: Path, audio_path: Path, output_path: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-loop", "1", "-framerate", "25", "-i", str(image_path),
        "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p",
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
    short_video = output_dir / f"mls_short_{date}.mp4"
    long_video = output_dir / f"mls_recap_{date}.mp4"
    asyncio.run(create_narration(draft["short_script"], short_audio))
    asyncio.run(create_narration(draft["long_script"], long_audio))
    render_scoreboard(draft["matches"], date, short_image, True)
    render_scoreboard(draft["matches"], date, long_image, False)
    render_mp4(short_image, short_audio, short_video)
    render_mp4(long_image, long_audio, long_video)
    return [str(short_video), str(long_video)]


def main() -> int:
    # Provider endpoint returns the previous UTC day, so label the report in UTC.
    utc_yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    report_date = os.getenv("REPORT_DATE", utc_yesterday)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"MLS recap preparation | report date: {report_date} | source: Football Soccer API")

    try:
        provider_results = fetch_yesterdays_results()
    except (ProviderError, requests.RequestException, ValueError) as exc:
        print(f"SAFE STOP: {exc}", file=sys.stderr)
        return 2

    # The provider's "yesterday" endpoint can include matches from more than
    # one calendar date. Keep only matches whose kickoff date matches the
    # report date, so yesterday's games are not mixed into today's recap.
    results = [
        match for match in provider_results
        if str(match.get("kickoff_date", "")).strip()[:10] == report_date
    ]
    skipped = len(provider_results) - len(results)
    print(f"Provider MLS matches before date filter: {len(provider_results)}")
    print(f"Matches for report date {report_date}: {len(results)}")
    print(f"Matches excluded from other dates: {skipped}")

    summaries = [build_match_summary(match) for match in results]
    draft = create_script(summaries, report_date)
    draft["source"] = "Football Soccer API (free results endpoint)"
    draft["source_report_date"] = report_date
    draft["limitations"] = [
        "Free endpoint results are for the previous UTC day; report date is UTC.",
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


if __name__ == "__main__":
    exit_code = main()
    if exit_code == 0:
        # Keep the service alive and make generated drafts available for review.
        # Railway's persistent volume is mounted at /data; OUTPUT_DIR points there.
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        os.chdir(OUTPUT_DIR)
        port = int(os.getenv("PORT", "8080"))
        server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"Video files available on the service HTTP port {port}")
        server.serve_forever()
    raise SystemExit(exit_code)
