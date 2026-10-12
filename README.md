# MLS Soccer Video Automation

A starter project for an automated, faceless MLS YouTube channel.

## Current status

The first pipeline stage can retrieve MLS match data and create a JSON file with a template-based recap script. It is intentionally fail-closed when no provider key is configured. **Audio, video rendering, and YouTube uploading are not implemented yet.**

## Data provider setup

This version uses API-Football (API-Sports) for fixtures, goal events, and standings. Its current public pricing page lists a free tier with 100 requests/day and includes fixtures, standings, and events; check current coverage, terms, and commercial-use permissions before relying on it: https://www.api-football.com/pricing/ and https://www.api-football.com/coverage.

In Railway, add the provider key as a service variable named `MLS_DATA_API_KEY`. Never put API keys in GitHub or send them in chat. Defaults are MLS league ID 253, current calendar year as season, and America/Chicago as the report timezone. These can be overridden with `MLS_LEAGUE_ID`, `MLS_SEASON`, and `REPORT_TIMEZONE`.

## Run locally

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
python main.py
```

Set `REPORT_DATE=YYYY-MM-DD` to test a specific date. Output is written to `output/mls_recap_YYYY-MM-DD.json`. The program stops safely if the data key is missing or the provider returns an error.

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
