# PolitiTrack API v2

Vercel serverless API serving **real** per-member FEC donation data and Congress.gov voting records. No database needed.

## What's New

**`GET /api/v1/district?zip=XXXXX`** — Full data package for My District:
- Real reps from whoismyrepresentative.com + Congress.gov
- Real FEC donations per member (top donors, industries, total raised)
- Real voting records on 5 tracked FY2026 bills
- All fetched in parallel

## Environment Variables (Vercel dashboard)

- `FEC_API_KEY` — from https://api.data.gov/signup/
- `CONGRESS_API_KEY` — same key from api.data.gov

## Deploy

Push to main. Vercel auto-deploys.

## Frontend Change

```js
// Old
const r = await fetch(`${apiBase}/api/v1/reps?zip=${zip}`);
// New — gets names + donors + votes in one call
const r = await fetch(`${apiBase}/api/v1/district?zip=${zip}`);
```
