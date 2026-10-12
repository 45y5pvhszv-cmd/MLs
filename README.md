# MLS Soccer Video Automation

A starter project for an automated, faceless MLS YouTube channel.

## Current status

The first pipeline stage can retrieve MLS match data and create a JSON file with a template-based recap script. It is intentionally fail-closed when no provider key is configured. **Audio, video rendering, and YouTube uploading are not implemented yet.**

## Data provider setup

This version uses Football Soccer API's free `/v1/matches` endpoint for recent match scores. The key must be stored in Railway as `FOOTBALL_SOCCER_API_KEY`. The code queries the requested report date and filters returned USA matches for MLS / Major League Soccer. Provider docs: https://footballsoccerapi.com/documentation/matches. The free endpoint does not guarantee goal-event details or full standings; those are not invented in the script. Defaults use America/Chicago and the previous local calendar date; set `REPORT_DATE=YYYY-MM-DD` to test a date.

## Run locally

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
python main.py
```

Output is written to `output/mls_recap_YYYY-MM-DD.json`. The program stops safely if the data key is missing or the provider returns an error.

## Still to build

1. Validate provider coverage and returned data in a real run.
2. Generate original, substantive commentary and verified standings context.
3. Add licensed AI narration and render vertical Shorts plus a longer recap with original graphics.
4. Add duplicate protection, retry logic, logging, and alerts.
5. Authorize YouTube uploads and begin with private test uploads. Unverified YouTube API projects may be restricted to private uploads until they pass Google's audit: https://developers.google.com/youtube/v3/docs/videos/.
6. Enable a Railway schedule only after end-to-end tests pass.

## Important

- Do not use unlicensed match footage, music, logos, or other protected assets.
- Keep commentary original and substantive. Repetitive or minimally transformed automated content may not qualify for monetization.
- Public publishing remains disabled. This code does not create audio/video and does not upload anything.
