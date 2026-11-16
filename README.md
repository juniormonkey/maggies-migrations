# Maggie's Migrations

Auto-updating interactive map for [curiosityguild.org/maggie](https://www.curiosityguild.org/maggie), backed by Discord channel `1493308738755625060`, Google Sheets as source of truth, Google Apps Script for live JSON reads + review alert emails, and GitHub Pages (`docs/`).

## Architecture

1. **GitHub Actions (`.github/workflows/sync.yml`)** runs every 6 hours (or manually via `workflow_dispatch`), fetches new messages from Discord, downloads & optimizes photos into `docs/photos/`, parses free-form posts with Gemini 2.5 Flash, geocodes with Nominatim, and posts new rows to Google Sheets.
2. **Google Sheets + Apps Script (`apps_script/Code.gs`)** stores all pins using `@OnlyCurrentDoc` (`spreadsheets.currentonly`) so `status = approved` rows appear live on the map (`doGet`). When rows are `pending` or `needs_geocoding`, GitHub Actions opens an `@juniormonkey` GitHub Issue to trigger email alerts.
3. **GitHub Pages (`docs/`)** serves the responsive Leaflet map + native `<dialog>` photo/video lightbox for Wix embedding.

## Sheet Status Values

- `approved` — Published live on the map.
- `pending` — Auto-geocoded from Discord; awaiting review. Change to `approved` to publish.
- `needs_geocoding` — Could not determine coordinates automatically. Fill `location_query` (and leave `pending`, or fill `lat`/`lng` directly and set `approved`), or set `ignored`.
- `ignored` — Skipped permanently.

## Tests

```bash
python3 -m unittest scripts/test_sync.py
```
