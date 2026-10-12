# MLS Soccer Video Automation

A starter project for an automated, faceless MLS YouTube channel.

## Current status

This repository is a safe starter scaffold. It does **not** publish videos yet. Public publishing stays disabled until data access, narration/video generation, and YouTube OAuth are configured and tested.

## Planned pipeline

1. Fetch completed MLS fixtures, scores, and goal scorers from a licensed data provider.
2. Validate results and standings; stop if data is missing or inconsistent.
3. Generate original narration and a short-form and long-form script.
4. Render videos using original graphics and properly licensed audio/assets.
5. Upload as private/unlisted test videos first.
6. Enable scheduled public publishing only after end-to-end verification.

## Run locally

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
python main.py
```

By default, this only prints a status message. It does not access accounts, spend money, or publish content.

## Required configuration later

Use Railway environment variables for secrets. Never commit API keys or OAuth tokens.

- `MLS_DATA_API_KEY`: licensed soccer-data provider key
- `OPENAI_API_KEY`: narration/script generation (optional provider)
- `YOUTUBE_CLIENT_SECRETS_JSON` and OAuth token setup: YouTube upload authorization
- `PUBLISH_MODE`: keep `disabled` until verified; then use private/unlisted testing before public

## Important

- Verify provider coverage for MLS, goal-scorer detail, standings, and permitted usage.
- Do not use unlicensed match footage, music, or league/team graphics.
- Keep commentary original and substantive; automated repetition or simple data readouts may not qualify for monetization.
- Add retries, duplicate-upload protection, logging, and alerts before production.
