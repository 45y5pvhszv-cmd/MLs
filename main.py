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
from gtts import gTTS
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

    def score_line(m: dict) -> str:
        return f'{m["home_team"]}, {m["home_score"]}; {m["away_team"]}, {m["away_score"]}.'

    # Put the hook first and vary the pacing, while sticking strictly to verified scores.
    ranked = sorted(
        completed,
        key=lambda m: abs(int(m.get("home_score") or 0) - int(m.get("away_score") or 0)),
    )
    close_games = [m for m in ranked if abs(int(m.get("home_score") or 0) - int(m.get("away_score") or 0)) <= 1]
    opener = (
        f"We have {len(completed)} final { 'score' if len(completed) == 1 else 'scores' } from MLS. "
        "Let's get into the results that shaped the night."
    )
    if close_games:
        m = close_games[0]
        opener = (
            f"MLS delivered a tight finish: {m['home_team']} and {m['away_team']} were separated "
            f"by just {abs(int(m.get('home_score') or 0) - int(m.get('away_score') or 0))} "
            f"at the final whistle. Here are the results."
        )
    lines = [score_line(m) for m in completed]
    match_text = " ".join(lines)
    short_script = (
        f"MLS Final Whistle! {len(completed)} final results, coming right up. "
        f"{match_text} That's the scoreboard. Which result matters most to your club? "
        "Drop your take in the comments and follow for the next MLS roundup."
    )
    long_script = (
        f"Welcome to MLS Final Whistle, your fast, no-favoritism league roundup for {report_date}. "
        f"{opener} {match_text} "
        "A quick note on the numbers: these are verified final scores, not a highlights package, "
        "and we're not going to invent scorers or table implications that we cannot confirm. "
        "The result to debate tonight is the one that surprised you most. Did your club get the job done, "
        "or did the scoreboard leave questions to answer? Tell us in the comments, and subscribe for "
        "the next MLS check-in."
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
    # Broadcast-inspired backdrop: floodlight beams, layered pitch panels and an original neon brand.
    draw.polygon([(0,0),(int(width*.82),0),(int(width*.57),int(height*.34)),(0,int(height*.25))], fill="#102f49")
    draw.polygon([(width,0),(width,int(height*.32)),(int(width*.72),int(height*.52)),(int(width*.88),0)], fill="#0a1c2d")
    draw.polygon([(0,int(height*.68)),(int(width*.34),int(height*.56)),(int(width*.63),height),(0,height)], fill="#0a2638")
    draw.polygon([(width,int(height*.58)),(width,height),(int(width*.48),height),(int(width*.76),int(height*.74))], fill="#10354a")
    # Stylized floodlights.
    for lx, ly in [(int(width*.78),int(height*.08)),(int(width*.9),int(height*.13))]:
        for k in range(4):
            draw.ellipse((lx+k*int(width*.014),ly,lx+k*int(width*.014)+int(width*.008),ly+int(width*.008)), fill="#d9fff7")
    draw.rectangle((0, 0, int(width*.018), height), fill="#31e0b1")
    margin = int(width * 0.065)
    title_font = load_font(int(width * (0.066 if vertical else 0.052)), True)
    sub_font = load_font(int(width * 0.024), True)
    team_font = load_font(int(width * (0.027 if vertical else 0.022)), True)
    score_font = load_font(int(width * (0.043 if vertical else 0.036)), True)
    small_font = load_font(int(width * 0.017), True)
    draw.text((margin, int(height*.055)), "MLS", font=title_font, fill="#FFFFFF", stroke_width=2, stroke_fill="#31e0b1")
    draw.text((margin, int(height*.055)+int(width*.075)), "FINAL WHISTLE  /  " + report_date, font=sub_font, fill="#31e0b1")
    draw.text((margin, int(height*.145)), "THE NIGHT IN SCORES", font=sub_font, fill="#FFFFFF")
    draw.rounded_rectangle((margin, int(height*.18), width-margin, int(height*.187)), radius=5, fill="#31e0b1")
    y = int(height * (0.225 if vertical else 0.235))
    max_rows = min(len(matches), 8 if vertical else 5)
    row_h = int(height * (0.078 if vertical else 0.12))
    for idx, match in enumerate(matches[:max_rows], start=1):
        bottom = y + row_h - int(height*.012)
        draw.rounded_rectangle((margin+5, y+5, width-margin+5, bottom+5), radius=22, fill="#020a12")
        draw.rounded_rectangle((margin, y, width-margin, bottom), radius=22, fill="#10283e", outline="#31e0b1" if idx == 1 else "#426b83", width=3)
        draw.rounded_rectangle((margin, y, margin+int(width*.012), bottom), radius=5, fill="#31e0b1" if idx == 1 else "#426b83")
        left = margin + int(width*.032)
        draw.text((left, y+int(row_h*.13)), str(match.get("home_team","Home"))[:24], font=team_font, fill="#FFFFFF")
        draw.text((left, y+int(row_h*.56)), str(match.get("away_team","Away"))[:24], font=team_font, fill="#b9cddd")
        score = f'{match.get("home_score","-")}  -  {match.get("away_score","-")}'
        box = draw.textbbox((0,0), score, font=score_font)
        draw.rounded_rectangle((width-margin-int(width*.30), y+int(row_h*.18), width-margin-int(width*.025), y+int(row_h*.83)), radius=12, fill="#071724", outline="#31e0b1", width=2)
        draw.text((width-margin-int(width*.035)-(box[2]-box[0]), y+int(row_h*.31),), score, font=score_font, fill="#31e0b1")
        y += row_h
    footer_y = height - margin*1.8
    draw.text((margin, footer_y), "SCORES  /  STORIES  /  SOCCER", font=small_font, fill="#FFFFFF")
    draw.text((margin, footer_y+int(height*.025)), "Independent MLS coverage  •  Verified final scores", font=small_font, fill="#a8bdce")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=95)


async def create_narration(text: str, path: Path) -> None:
    """Try Edge neural narration, then fall back to Google TTS if Edge blocks the host."""
    voice = os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural")
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(path))
        if path.exists() and path.stat().st_size > 1000:
            return
    except Exception as exc:
        print(f"Primary TTS unavailable; trying fallback voice: {exc}")
    try:
        await asyncio.to_thread(lambda: gTTS(text=text, lang="en", slow=False).save(str(path)))
        if path.exists() and path.stat().st_size > 1000:
            print(f"Fallback narration generated: {path.name}")
            return
    except Exception as exc:
        raise RuntimeError(f"Both narration providers failed: {exc}") from exc
    raise RuntimeError("Narration file was not created or was unexpectedly small.")


def render_mp4(image_path: Path, audio_path: Path, output_path: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    width, height = Image.open(image_path).size
    # Broadcast-style motion: steady push-in, moving scan bars, a sweeping accent,
    # and a pulsing top rail. All elements are original graphics; no match footage is used.
    motion_filter = (
        f"zoompan=z='min(zoom+0.00055,1.045)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={width}x{height}:fps=25,"
        f"drawbox=x='mod(t*520\\,{width+260})-260':y=0:w=260:h={height}:"
        "color=0x31e0b1@0.045:t=fill,"
        f"drawbox=x='mod(t*260\\,{width+220})-220':y={int(height*0.19)}:w=220:h=4:"
        "color=0x31e0b1@0.95:t=fill,"
        f"drawbox=x='mod(t*240\\,{width+180})-180':y={height-16}:w=180:h=8:"
        "color=0x31e0b1@0.95:t=fill,"
        f"drawbox=x=0:y=0:w={width}:h=5:color=0x31e0b1@'0.55+0.4*sin(6*t)':t=fill"
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


def make_title_card(path: Path, report_date: str, headline: str, vertical: bool) -> None:
    width, height = (720, 1280) if vertical else (1280, 720)
    image = Image.new("RGB", (width, height), "#06101f")
    draw = ImageDraw.Draw(image)
    draw.polygon([(0,0),(width,0),(int(width*.58),height),(0,int(height*.78))], fill="#102f49")
    draw.polygon([(width,0),(width,height),(int(width*.45),height)], fill="#0b263b")
    draw.rectangle((0,0,int(width*.018),height), fill="#31e0b1")
    margin = int(width*.07)
    draw.text((margin,int(height*.17)), "MLS", font=load_font(int(width*.1),True), fill="#fff", stroke_width=2, stroke_fill="#31e0b1")
    draw.text((margin,int(height*.31)), "FINAL WHISTLE", font=load_font(int(width*.045),True), fill="#31e0b1")
    # Wrap headline to fit either aspect ratio.
    words = headline.split()
    lines, current = [], ""
    font = load_font(int(width*(.052 if vertical else .06)), True)
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0,0), candidate, font=font)[2] > width - margin*2 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    y = int(height*.43)
    for line in lines[:4]:
        draw.text((margin,y), line, font=font, fill="#FFFFFF")
        y += int(height*.075)
    draw.text((margin,int(height*.82)), report_date + "  /  SCORES • STORIES • SOCCER", font=load_font(int(width*.021),True), fill="#b9cddd")
    path.parent.mkdir(parents=True,exist_ok=True)
    image.save(path,quality=95)


def render_scene_sequence(parts: list[tuple[Path, Path]], output: Path, workdir: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    scene_files = []
    for idx, (image_path, audio_path) in enumerate(parts):
        scene_path = workdir / f"scene_{idx:02d}_{output.stem}.mp4"
        render_mp4(image_path, audio_path, scene_path)
        scene_files.append(scene_path)
    concat_file = workdir / f"concat_{output.stem}.txt"
    concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in scene_files), encoding="utf-8")
    result = subprocess.run([ffmpeg,"-y","-f","concat","-safe","0","-i",str(concat_file),"-c","copy","-movflags","+faststart",str(output)],capture_output=True,text=True,timeout=300)
    if result.returncode:
        raise RuntimeError("FFmpeg scene assembly failed: " + result.stderr[-1200:])
    for p in scene_files:
        p.unlink(missing_ok=True)
    concat_file.unlink(missing_ok=True)


def render_videos(draft: dict, output_dir: Path) -> list[str]:
    if draft.get("status") != "draft_ready":
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    date = draft["date"]
    assets = output_dir / "assets" / date
    assets.mkdir(parents=True, exist_ok=True)
    short_video = output_dir / f"mls_short_{date}_v5.mp4"
    long_video = output_dir / f"mls_recap_{date}_v5.mp4"
    matches = draft["matches"]
    # Shorts stay focused: lead with the closest games and cap the rundown at four.
    ranked = sorted(matches, key=lambda m: abs(int(m.get("home_score") or 0)-int(m.get("away_score") or 0)))
    short_matches = ranked[:4]
    short_parts, long_parts = [], []

    def add_scene(parts, tag, headline, narration, match, vertical):
        image_path = assets / f"{tag}.jpg"
        audio_path = assets / f"{tag}.mp3"
        make_title_card(image_path,date,headline,vertical) if match is None else render_scoreboard([match],date,image_path,vertical)
        asyncio.run(create_narration(narration,audio_path))
        parts.append((image_path,audio_path))

    hook_match = short_matches[0] if short_matches else matches[0]
    hook_gap = abs(int(hook_match.get("home_score") or 0) - int(hook_match.get("away_score") or 0))
    hook_line = f"{hook_match['home_team']} and {hook_match['away_team']} finished {hook_match['home_score']} to {hook_match['away_score']}."
    if hook_gap == 0:
        hook = f"Straight into the tightest finish: {hook_line} Here are the verified MLS finals."
    elif hook_gap == 1:
        hook = f"A one-goal finish leads the MLS roundup: {hook_line} Let's hit the rest of the scoreboard."
    else:
        hook = f"Here is your MLS scoreboard for {date}, starting with {hook_line}"
    add_scene(short_parts,"short_intro","START WITH THE CLOSEST FINISH",hook,None,True)
    for i,m in enumerate(short_matches,1):
        score=f'{m["home_team"]} {m["home_score"]}, {m["away_team"]} {m["away_score"]}.'
        add_scene(short_parts,f"short_match_{i}",f'{m["home_team"]} vs {m["away_team"]}',score,m,True)
    add_scene(short_parts,"short_outro","YOUR TAKE?", "Which result surprised you most? Tell us below and follow for the next MLS roundup.",None,True)

    add_scene(long_parts,"long_intro","THE FULL MLS RESULTS",f"Welcome to MLS Final Whistle for {date}. This is a verified results rundown, match by match, with no invented scorers or claims.",None,False)
    for i,m in enumerate(matches,1):
        gap=abs(int(m.get("home_score") or 0)-int(m.get("away_score") or 0))
        if gap == 0:
            angle="These sides finished level, so neither could find a winning margin."
        elif gap == 1:
            angle="A one-goal margin makes this one of the tighter results on the slate."
        elif gap >= 3:
            angle="That is a decisive scoreline by a three-goal or greater margin."
        else:
            angle="The final score gives us the result, without guessing at events we cannot verify."
        narration=f"Match {i}. {m['home_team']} {m['home_score']}, {m['away_team']} {m['away_score']}. {angle}"
        add_scene(long_parts,f"long_match_{i}",f'{m["home_team"]} vs {m["away_team"]}',narration,m,False)
    add_scene(long_parts,"long_outro","JOIN THE CONVERSATION","Which result stood out to you? Share your take, and subscribe for the next MLS Final Whistle. This rundown sticks to verified final scores rather than guessing at playoff implications.",None,False)
    render_scene_sequence(short_parts,short_video,assets)
    render_scene_sequence(long_parts,long_video,assets)
    return [str(short_video),str(long_video)]


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
