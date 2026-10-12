# MLS Soccer Video Automation

A starter project for an automated, faceless MLS YouTube channel.

## Current status

The pipeline retrieves recent MLS match results, creates a JSON recap draft, generates narration with Edge TTS, and renders original scoreboard videos in vertical and landscape formats. YouTube uploading is not implemented yet.

## Data provider setup

This version uses Football Soccer API's free `/v1/results/yesterday` endpoint for recent scores. The key must be stored in Railway as `FOOTBALL_SOCCER_API_KEY`. The free endpoint returns the previous UTC day. Provider docs: https://footballsoccerapi.com/documentation/matches. The free endpoint does not guarantee goal-event details or full standings; those are not invented in the script.

## Run locally

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
python main.py
```

Output includes `output/mls_recap_YYYY-MM-DD.json`, `output/mls_short_YYYY-MM-DD.mp4`, and `output/mls_recap_YYYY-MM-DD.mp4`. The videos use generated narration and original scoreboards, not match footage. Set `ENABLE_VIDEO_RENDER=false` to skip rendering.

## Still to build

1. Validate provider coverage and returned data in a real run.
2. Generate original, substantive commentary and verified standings context.
1. Test the rendered audio/video output and improve the graphics and narration.
2. Add duplicate protection, retry logic, logging, and alerts.
3. Authorize YouTube uploads and begin with private test uploads. Unverified YouTube API projects may be restricted to private uploads until they pass Google's audit: https://developers.google.com/youtube/v3/docs/videos/.
4. Enable a Railway schedule only after end-to-end tests pass.

## Important

- Do not use unlicensed match footage, music, logos, or other protected assets.
- Keep commentary original and substantive. Repetitive or minimally transformed automated content may not qualify for monetization.
- YouTube upload and public publishing remain disabled. Railway containers are temporary, so rendered files are not yet delivered to a permanent storage location.
