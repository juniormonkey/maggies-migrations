"""Scheduled sync from Discord channel 1493308738755625060 to Google Sheets + GitHub Pages."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps

DISCORD_API = "https://discord.com/api/v10"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
NOMINATIM_API = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_API = "https://nominatim.openstreetmap.org/reverse"
DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1YTSOYbDWXKjLt-E23Pk3qkp0fNwdZ6p4XqMXz9SeNqk/edit"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic")
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{6,})[^\s]*"
)
USER_AGENT = "MaggiesMigrationsBot/1.0 (+https://curiosityguild.org/maggie)"


def sync_config_json(data_dir: Path, live_sheet_url: str) -> None:
    """Keep docs/data/config.json aligned with the deployed Apps Script webhook URL."""
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    existing: dict[str, Any] = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    updated = {
        "liveSheetUrl": live_sheet_url,
        "mapTitle": existing.get("mapTitle", "Maggie's Migrations"),
        "mapSubtitle": existing.get("mapSubtitle", "Where has Maggie flown to recently?"),
    }
    config_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


def post_webhook(webhook_url: str, secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send an authenticated POST request to the Google Apps Script Web App."""
    body = dict(payload)
    body["secret"] = secret
    resp = requests.post(
        webhook_url,
        json=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        allow_redirects=True,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Apps Script error: {data}")
    return data


def _discord_get(url: str, bot_token: str, params: dict[str, Any] | None = None) -> requests.Response:
    """Perform a Discord GET request with basic 429 rate-limit backoff."""
    headers = {"Authorization": f"Bot {bot_token}", "User-Agent": USER_AGENT}
    for _ in range(4):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 2.0))
            time.sleep(min(retry_after + 0.5, 15.0))
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def fetch_discord_messages(channel_id: str, bot_token: str, after_id: str = "") -> list[dict[str, Any]]:
    """Fetch all non-bot messages after `after_id` sorted oldest-to-newest."""
    cursor = after_id or "0"
    collected: list[dict[str, Any]] = []
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    while True:
        batch = _discord_get(url, bot_token, {"limit": 100, "after": cursor}).json()
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        collected.extend(batch)
        cursor = batch[-1]["id"]
        if len(batch) < 100:
            break
    return [m for m in collected if not m.get("author", {}).get("bot")]


def is_image_attachment(att: dict[str, Any]) -> bool:
    """Return True if a Discord attachment represents an image."""
    ctype = str(att.get("content_type") or "").lower()
    if ctype.startswith("image/"):
        return True
    filename = str(att.get("filename") or "").lower()
    return filename.endswith(IMAGE_EXTENSIONS)


def extract_youtube_urls(text: str) -> list[str]:
    """Extract canonical YouTube watch URLs from message text."""
    seen: list[str] = []
    for match in YOUTUBE_RE.finditer(text or ""):
        canonical = f"https://www.youtube.com/watch?v={match.group(1)}"
        if canonical not in seen:
            seen.append(canonical)
    return seen


def split_tail_holdback(
    messages: list[dict[str, Any]],
    now_utc: dt.datetime | None = None,
    holdback_minutes: int = 15,
) -> tuple[list[dict[str, Any]], str | None]:
    """Hold back a brand-new bare photo message so a poster's follow-up text arrives together."""
    if not messages:
        return [], None
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=holdback_minutes)

    hold_idx = len(messages)
    while hold_idx > 0:
        msg = messages[hold_idx - 1]
        ts = dt.datetime.fromisoformat(msg["timestamp"])
        has_images = any(is_image_attachment(a) for a in msg.get("attachments", []))
        has_text = bool(str(msg.get("content") or "").strip())
        if ts > cutoff and has_images and not has_text:
            hold_idx -= 1
            continue
        break

    process = messages[:hold_idx]
    new_cursor = process[-1]["id"] if process else None
    return process, new_cursor


def _rational_to_float(val: Any) -> float:
    """Convert an EXIF rational, (num, den) tuple, or numeric scalar to float."""
    if isinstance(val, tuple) and len(val) == 2:
        num, den = val
        if float(den) == 0.0:
            raise ValueError("Zero denominator in EXIF rational")
        return float(num) / float(den)
    return float(val)


def _dms_to_decimal(dms: Any, ref: str) -> float | None:
    """Convert EXIF degrees/minutes/seconds triplet + hemisphere ref to signed decimal degrees."""
    if not dms or len(dms) != 3:
        return None
    deg = _rational_to_float(dms[0])
    minutes = _rational_to_float(dms[1])
    seconds = _rational_to_float(dms[2])
    decimal = abs(deg) + (minutes / 60.0) + (seconds / 3600.0)
    if not math.isfinite(decimal):
        return None
    if str(ref or "").strip().upper() in ("S", "W"):
        decimal = -decimal
    return decimal


def extract_exif_gps(img: Image.Image) -> tuple[float, float] | None:
    """Extract decimal (lat, lng) from an image's EXIF GPS IFD before stripping/resizing."""
    try:
        exif = img.getexif()
        gps_ifd = exif.get_ifd(0x8825) if exif else {}
        if not gps_ifd:
            return None
        lat = _dms_to_decimal(gps_ifd.get(2), str(gps_ifd.get(1) or "N"))
        lng = _dms_to_decimal(gps_ifd.get(4), str(gps_ifd.get(3) or "E"))
        if lat is None or lng is None:
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            return None
        if abs(lat) < 1e-5 and abs(lng) < 1e-5:
            return None
        return round(lat, 7), round(lng, 7)
    except Exception:
        return None


def download_and_resize_image(
    url: str,
    dest_path: Path,
    max_dim: int = 1600,
) -> tuple[bytes, tuple[float, float] | None]:
    """Download image URL, read EXIF GPS before normalization, resize, and write JPEG."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with Image.open(io.BytesIO(resp.content)) as img:
        exif_gps = extract_exif_gps(img)
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        jpeg_bytes = buf.getvalue()
    dest_path.write_bytes(jpeg_bytes)
    return jpeg_bytes, exif_gps


def resize_for_vision(jpeg_bytes: bytes, max_dim: int = 1280) -> str:
    """Create a base64 JPEG at up to 1280px so signs and small placard text remain legible."""
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")


def _gemini_post(body: dict[str, Any], api_key: str, timeout: int = 180) -> dict[str, Any]:
    """POST to Gemini generateContent using header auth with timeout/429/5xx retries."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    retryable_statuses = {429, 500, 502, 503, 504}
    last_exc: Exception | None = None

    for attempt in range(4):
        try:
            resp = requests.post(GEMINI_API, headers=headers, json=body, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < 3:
                time.sleep(5.0 * (attempt + 1))
                continue
            raise

        if resp.status_code in retryable_statuses and attempt < 3:
            if resp.status_code == 429:
                retry_hdr = float(resp.headers.get("Retry-After", 0) or 0)
                delay = max(retry_hdr, 15.0 * (attempt + 1))
            else:
                delay = 5.0 * (attempt + 1)
            time.sleep(delay)
            continue

        resp.raise_for_status()
        return resp.json()

    assert last_exc is not None
    raise last_exc


def _parse_gemini_json(resp_json: dict[str, Any]) -> Any:
    """Extract and parse JSON text from a Gemini generateContent response."""
    raw_text = resp_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL).strip()
    return json.loads(raw_text)


def call_gemini_group_sightings(messages: list[dict[str, Any]], api_key: str) -> list[dict[str, Any]]:
    """Group chronological Discord messages into sightings and extract place/caption."""
    if not messages:
        return []

    compact = [
        {
            "id": m["id"],
            "timestamp": m["timestamp"],
            "author": m.get("author", {}).get("global_name") or m.get("author", {}).get("username") or "Member",
            "author_id": m.get("author", {}).get("id", ""),
            "reply_to_id": (m.get("message_reference") or {}).get("message_id", ""),
            "image_count": sum(1 for a in m.get("attachments", []) if is_image_attachment(a)),
            "has_youtube": bool(extract_youtube_urls(m.get("content", ""))),
            "text": m.get("content", ""),
        }
        for m in messages
    ]

    prompt = (
        "You extract travel sightings of our community mascot 'Maggie' (a toy magpie) from a Discord channel.\n"
        "Rules:\n"
        "1. Posters often upload Maggie photos in one message and explain where Maggie is in the same message "
        "OR in an immediately following message or reply.\n"
        "2. Merge consecutive messages by the same author (close in time) or reply threads that describe the same "
        "photo/video post into ONE sighting object.\n"
        "3. Use surrounding messages in the batch by the same author as trip context when a post omits the city/state "
        '(e.g., "The OG Heinz factory!" right after a Pittsburgh post).\n'
        "4. Ignore pure off-topic chatter that has image_count=0, has_youtube=false, and doesn't explain an adjacent photo.\n"
        "5. Every message with image_count > 0 or has_youtube=true MUST either be the primary_message_id of a sighting "
        "or included inside grouped_message_ids of an adjacent sighting.\n"
        "6. Never join two separate venues with 'and' in location_query; put a single geocodable venue + city/country "
        'and expand colloquial names (e.g. "Great Salt Flats" -> "Bonneville Salt Flats, Utah, USA").\n'
        "7. Return ONLY a JSON array of objects with keys:\n"
        '   - "primary_message_id": string (first/photo message id in the group)\n'
        '   - "grouped_message_ids": array of all message ids belonging to this sighting\n'
        '   - "is_sighting": boolean\n'
        '   - "place_name": concise clean display title (e.g. "Eiffel Tower, Paris" or "" if unknown)\n'
        '   - "location_query": geocodable place/address string for OpenStreetMap Nominatim (or "" if unknown)\n'
        '   - "fallback_queries": array of 1-3 progressively broader Nominatim queries '
        '(e.g. ["Southbank, Melbourne, Australia", "Melbourne, Australia"])\n'
        '   - "needs_visual_check": boolean (true if location_query is empty or ambiguous/missing city-country '
        'such as "Ocean Beach" or "Dark Horse")\n'
        '   - "caption": cleaned human story/notes combining relevant text from grouped messages (without bare URLs)\n\n'
        f"Messages JSON:\n{json.dumps(compact, ensure_ascii=False)}"
    )

    resp_json = _gemini_post(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "thinkingConfig": {"thinkingBudget": 512},
            },
        },
        api_key,
        timeout=180,
    )
    parsed = _parse_gemini_json(resp_json)
    if isinstance(parsed, dict):
        first_list = next((v for v in parsed.values() if isinstance(v, list)), None)
        if first_list is not None:
            parsed = first_list
    if not isinstance(parsed, list):
        raise ValueError(f"Expected JSON list from Gemini, got {type(parsed)}")
    return [item for item in parsed if item.get("is_sighting", True)]


def infer_location_from_photos_and_context(
    jpeg_b64_list: list[str],
    api_key: str,
    caption: str = "",
    tentative_query: str = "",
    author_context: list[str] | None = None,
    exif_gps: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Inspect up to 4 1280px photos + OCR/trip context to infer or disambiguate a location."""
    if not jpeg_b64_list:
        return {"place_name": "", "location_query": "", "fallback_queries": [], "visual_evidence": ""}

    context_block = {
        "caption": caption,
        "tentative_query": tentative_query,
        "recent_posts_by_same_author": author_context or [],
        "exif_gps_hint": f"{exif_gps[0]:.5f}, {exif_gps[1]:.5f}" if exif_gps else "",
    }
    prompt = (
        "These photos show a community mascot toy magpie ('Maggie') traveling somewhere.\n"
        "Tasks:\n"
        "1. Carefully read ALL visible text across every image (signs, museum placards, menus, bottle labels, "
        "street names, badges, storefronts, transit maps) and inspect landmarks or recognizable scenery.\n"
        "2. If tentative_query is empty OR ambiguous (like 'Ocean Beach' or 'Dark Horse'), use visible clues, "
        "signage, and recent_posts_by_same_author to identify or disambiguate the exact city/venue. If tentative_query "
        "is just a non-geographic event/joke title and no real place is identifiable, return empty strings.\n"
        "3. Never guess from generic indoor rooms, food, or trees. Only return a place if backed by clear visual/text clues "
        "or strong author trip continuity.\n"
        "4. Return ONLY a JSON object with keys:\n"
        '   - "place_name": concise clean display title (or "")\n'
        '   - "location_query": specific OpenStreetMap Nominatim query including city/region/country (or "")\n'
        '   - "fallback_queries": array of 1-3 broader Nominatim queries (e.g. ["Neighborhood, City", "City, Country"])\n'
        '   - "visual_evidence": short 1-phrase note of what sign/text/landmark/context identified the place (or "")\n\n'
        f"Context:\n{json.dumps(context_block, ensure_ascii=False)}"
    )
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for b64 in jpeg_b64_list[:4]:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    resp_json = _gemini_post(
        {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "thinkingConfig": {"thinkingBudget": 512},
            },
        },
        api_key,
        timeout=120,
    )
    data = _parse_gemini_json(resp_json)
    if not isinstance(data, dict):
        return {"place_name": "", "location_query": "", "fallback_queries": [], "visual_evidence": ""}
    fallbacks = data.get("fallback_queries")
    return {
        "place_name": str(data.get("place_name") or "").strip(),
        "location_query": str(data.get("location_query") or "").strip(),
        "fallback_queries": [str(q).strip() for q in fallbacks if str(q).strip()] if isinstance(fallbacks, list) else [],
        "visual_evidence": str(data.get("visual_evidence") or "").strip(),
    }


def ensure_all_media_posts_captured(
    messages: list[dict[str, Any]],
    sightings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Guarantee every message with image attachments or YouTube URLs appears in a sighting."""
    covered_ids: set[str] = set()
    for s in sightings:
        if s.get("primary_message_id"):
            covered_ids.add(str(s["primary_message_id"]))
        for gid in s.get("grouped_message_ids", []):
            covered_ids.add(str(gid))

    result = list(sightings)
    for msg in messages:
        has_images = any(is_image_attachment(a) for a in msg.get("attachments", []))
        has_yt = bool(extract_youtube_urls(msg.get("content", "")))
        if (has_images or has_yt) and msg["id"] not in covered_ids:
            result.append(
                {
                    "primary_message_id": msg["id"],
                    "grouped_message_ids": [msg["id"]],
                    "is_sighting": True,
                    "place_name": "",
                    "location_query": "",
                    "caption": str(msg.get("content") or "").strip(),
                }
            )
    return result


class NominatimLimiter:
    """Rate-limited wrapper around OpenStreetMap Nominatim geocoding."""

    def __init__(self, min_interval: float = 1.1) -> None:
        self.min_interval = min_interval
        self._last_call = 0.0

    def _wait_for_slot(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def geocode(self, query: str) -> tuple[float | None, float | None]:
        clean = query.strip()
        if not clean:
            return None, None
        self._wait_for_slot()
        resp = requests.get(
            NOMINATIM_API,
            params={"q": clean, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        hits = resp.json()
        if not hits:
            return None, None
        return round(float(hits[0]["lat"]), 7), round(float(hits[0]["lon"]), 7)

    def reverse_geocode(self, lat: float, lng: float) -> str:
        """Resolve decimal coordinates into a concise human-readable place label."""
        fallback_label = f"{lat:.5f}, {lng:.5f}"
        try:
            self._wait_for_slot()
            resp = requests.get(
                NOMINATIM_REVERSE_API,
                params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 14},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            addr = data.get("address") or {}
            name = str(data.get("name") or "").strip()
            if not name and data.get("display_name"):
                name = str(data["display_name"]).split(",")[0].strip()
            city = str(
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("state")
                or ""
            ).strip()
            country = str(addr.get("country") or "").strip()
            parts: list[str] = []
            for seg in (name, city, country):
                if seg and seg.lower() not in {p.lower() for p in parts}:
                    parts.append(seg)
            return ", ".join(parts) if parts else fallback_label
        except Exception:
            return fallback_label


def build_query_candidates(primary_query: str, fallback_queries: list[str] | None = None) -> list[str]:
    """Build ordered, deduplicated Nominatim candidates including progressive comma-tail fallbacks."""
    seeds = [str(q).strip() for q in [primary_query, *(fallback_queries or [])] if str(q).strip()]
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            candidates.append(q)

    for seed in seeds:
        _add(seed)
    for seed in seeds:
        parts = [p.strip() for p in seed.split(",") if p.strip()]
        if len(parts) >= 3:
            for start_idx in range(1, len(parts) - 1):
                _add(", ".join(parts[start_idx:]))
    return candidates


def geocode_with_fallbacks(
    geocoder: NominatimLimiter,
    primary_query: str,
    fallback_queries: list[str] | None = None,
) -> tuple[float | None, float | None, str]:
    """Try primary and fallback Nominatim queries in order, returning (lat, lng, matched_query)."""
    for candidate in build_query_candidates(primary_query, fallback_queries):
        lat, lng = geocoder.geocode(candidate)
        if lat is not None and lng is not None:
            return lat, lng, candidate
    return None, None, ""


def build_discord_jump_url(guild_id: str | None, channel_id: str, message_id: str) -> str:
    """Build a Discord jump link when guild_id is available."""
    guild_segment = guild_id or "@me"
    return f"https://discord.com/channels/{guild_segment}/{channel_id}/{message_id}"


def build_alert_issue_payload(
    new_rows: list[dict[str, Any]],
    geocoded_updates: list[dict[str, Any]],
    sheet_url: str,
    notify_user: str = "juniormonkey",
) -> dict[str, Any] | None:
    """Build a GitHub Issue notification payload when new/updated rows need attention."""
    combined = list(new_rows) + list(geocoded_updates)
    if not combined:
        return None

    needs_geocoding_count = sum(1 for r in combined if r.get("status") == "needs_geocoding")
    pending_count = sum(1 for r in combined if r.get("status") == "pending")

    summary_parts: list[str] = []
    if needs_geocoding_count > 0:
        noun = "needs geocoding" if needs_geocoding_count == 1 else "need geocoding"
        summary_parts.append(f"{needs_geocoding_count} {noun}")
    if pending_count > 0:
        summary_parts.append(f"{pending_count} awaiting approval")
    if not summary_parts:
        summary_parts.append(f"{len(combined)} updated")

    title = f"[Maggie's Migrations] {', '.join(summary_parts)}"

    lines = [
        f"@{notify_user} New Maggie sightings require review in the [Maggie's Migrations Google Sheet]({sheet_url}).",
        "",
        "- Change `status` from `pending` to `approved` to publish to the map.",
        "- For `needs_geocoding` rows, fill in `location_query` (or `lat`/`lng`) and set `status` to `approved` (or leave `needs_geocoding`/`pending` to auto-geocode on next run).",
        "",
    ]
    for item in combined:
        status = item.get("status")
        badge = "NEEDS GEOCODING" if status == "needs_geocoding" else "PENDING APPROVAL"
        label = item.get("place_name") or item.get("location_query") or "(unknown location)"
        author = item.get("author") or "Unknown"
        posted_at = item.get("posted_at") or ""
        discord_url = str(item.get("discord_url") or "").strip()
        caption = str(item.get("caption") or "").strip()
        media = [u for u in str(item.get("media_urls") or "").split() if u]
        first_image = next((u for u in media if "youtube.com" not in u and "youtu.be" not in u), "")

        meta_parts = [f"Author: {author}"]
        if posted_at:
            meta_parts.append(f"Date: {posted_at}")
        if discord_url:
            meta_parts.append(f"[Discord message]({discord_url})")

        lines.append(f"- **[{badge}] {label}** (`{item.get('id', '')}`)")
        lines.append(f"  {' · '.join(meta_parts)}")
        if caption:
            lines.append(f"  > {caption}")
        if first_image:
            lines.append(f'  <br><img src="{first_image}" width="240" alt="{label}">')

    return {
        "title": title,
        "body": "\n".join(lines),
        "assignees": [notify_user],
    }


def write_static_cache(pins: list[dict[str, Any]], data_dir: Path) -> None:
    """Write approved pins snapshot to docs/data/pins.json and docs/data/pins.csv."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pins.json").write_text(
        json.dumps(pins, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    fieldnames = [
        "id",
        "status",
        "place_name",
        "location_query",
        "lat",
        "lng",
        "caption",
        "author",
        "posted_at",
        "layer",
        "media_urls",
        "discord_url",
        "notes",
    ]
    with (data_dir / "pins.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for pin in pins:
            media = pin.get("media", [])
            writer.writerow(
                {
                    "id": pin.get("id", ""),
                    "status": "approved",
                    "place_name": pin.get("place_name", ""),
                    "location_query": pin.get("place_name", ""),
                    "lat": pin.get("lat", ""),
                    "lng": pin.get("lng", ""),
                    "caption": pin.get("caption", ""),
                    "author": pin.get("author", ""),
                    "posted_at": pin.get("posted_at", ""),
                    "layer": pin.get("layer", ""),
                    "media_urls": " ".join(media) if isinstance(media, list) else "",
                    "discord_url": "",
                    "notes": "",
                }
            )


def split_into_chunks(
    messages: list[dict[str, Any]],
    target_size: int = 25,
    max_size: int = 35,
) -> list[list[dict[str, Any]]]:
    """Split messages into chunks without severing an author's follow-up text or replies."""
    if not messages:
        return []
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_ids: set[str] = set()

    for msg in messages:
        if len(current) >= target_size:
            prev = current[-1]
            prev_author = str((prev.get("author") or {}).get("id") or "")
            nxt_author = str((msg.get("author") or {}).get("id") or "")
            reply_id = str((msg.get("message_reference") or {}).get("message_id") or "")
            within_window = (
                dt.datetime.fromisoformat(msg["timestamp"])
                - dt.datetime.fromisoformat(prev["timestamp"])
            ) <= dt.timedelta(minutes=30)
            keep_together = (
                (bool(prev_author) and prev_author == nxt_author and within_window)
                or (bool(reply_id) and reply_id in current_ids)
            )
            if not keep_together or len(current) >= max_size:
                chunks.append(current)
                current = []
                current_ids = set()

        current.append(msg)
        current_ids.add(str(msg["id"]))

    if current:
        chunks.append(current)
    return chunks


def process_messages_in_chunks(
    messages: list[dict[str, Any]],
    api_key: str,
    target_size: int = 25,
    max_size: int = 35,
) -> list[dict[str, Any]]:
    """Run Gemini grouping across author-aware message chunks."""
    chunks = split_into_chunks(messages, target_size=target_size, max_size=max_size)
    all_sightings: list[dict[str, Any]] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        print(f"Parsing chunk {idx}/{total} ({len(chunk)} messages)...", flush=True)
        grouped = call_gemini_group_sightings(chunk, api_key)
        all_sightings.extend(ensure_all_media_posts_captured(chunk, grouped))
        if idx < total:
            time.sleep(2.0)
    return all_sightings


def _build_author_context(
    author: str,
    posted_at: str,
    approved_pins: list[dict[str, Any]],
    session_sightings: list[tuple[str, str, str]],
) -> list[str]:
    """Collect up to 5 recent sightings by the same author within ±7 days for trip continuity."""
    try:
        target_date = dt.date.fromisoformat(posted_at)
    except ValueError:
        return []
    author_key = author.strip().lower()
    if not author_key:
        return []

    records: list[tuple[dt.date, str]] = []
    for p in approved_pins:
        p_author = str(p.get("author") or "").strip().lower()
        p_date_str = str(p.get("posted_at") or "").strip()
        p_place = str(p.get("place_name") or "").strip()
        if p_author == author_key and p_date_str and p_place:
            try:
                records.append((dt.date.fromisoformat(p_date_str), p_place))
            except ValueError:
                continue

    for s_author, s_date_str, s_place in session_sightings:
        if s_author.strip().lower() == author_key and s_date_str and s_place:
            try:
                records.append((dt.date.fromisoformat(s_date_str), s_place))
            except ValueError:
                continue

    nearby = [
        (abs((d - target_date).days), d, f"{d.isoformat()}: {place}")
        for d, place in records
        if abs((d - target_date).days) <= 7
    ]
    nearby.sort(key=lambda item: (item[0], item[1]))

    result: list[str] = []
    seen: set[str] = set()
    for _, _, entry in nearby:
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
            if len(result) >= 5:
                break
    return result


def run_sync(repo_root: Path) -> None:
    """Execute one full incremental Discord -> Google Sheet -> static cache sync."""
    discord_token = os.environ["DISCORD_BOT_TOKEN"]
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "1493308738755625060")
    gemini_key = os.environ["GEMINI_API_KEY"]
    webhook_url = os.environ["GSHEET_WEBHOOK_URL"]
    webhook_secret = os.environ["GSHEET_WEBHOOK_SECRET"]
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "https://juniormonkey.github.io/maggies-migrations").rstrip("/")

    data_dir = repo_root / "docs" / "data"
    photos_dir = repo_root / "docs" / "photos"
    sync_config_json(data_dir, webhook_url)

    state = post_webhook(webhook_url, webhook_secret, {"action": "get_state"})
    existing_ids = set(state.get("existing_ids", []))
    last_message_id = str(state.get("last_message_id") or "")
    retry_geocoding = list(state.get("retry_geocoding", []))
    approved_pins_state = list(state.get("approved_pins", []))

    geocoder = NominatimLimiter()
    geocoded_updates: list[dict[str, Any]] = []
    for item in retry_geocoding:
        query = str(item.get("location_query") or "")
        lat, lng, matched_query = geocode_with_fallbacks(geocoder, query)
        if lat is not None and lng is not None:
            note = "Geocoded from updated location_query — change status to approved to publish"
            if matched_query.lower() != query.strip().lower():
                note = (
                    f"Geocoded via fallback '{matched_query}' from updated location_query "
                    "— change status to approved to publish"
                )
            geocoded_updates.append(
                {
                    "id": item["id"],
                    "place_name": item.get("place_name") or item["location_query"],
                    "status": "pending",
                    "lat": lat,
                    "lng": lng,
                    "author": item.get("author", ""),
                    "posted_at": item.get("posted_at", ""),
                    "caption": item.get("caption", ""),
                    "media_urls": item.get("media_urls", ""),
                    "discord_url": item.get("discord_url", ""),
                    "notes": note,
                }
            )

    raw_messages = fetch_discord_messages(channel_id, discord_token, last_message_id)
    messages, new_cursor = split_tail_holdback(raw_messages)
    print(
        f"Fetched {len(raw_messages)} Discord message(s); processing {len(messages)} "
        f"(retry_geocoding={len(retry_geocoding)}).",
        flush=True,
    )

    msg_by_id = {m["id"]: m for m in messages}
    sightings = process_messages_in_chunks(messages, gemini_key) if messages else []
    print(f"Identified {len(sightings)} sighting candidate(s).", flush=True)

    new_rows: list[dict[str, Any]] = []
    session_sightings: list[tuple[str, str, str]] = []
    for s in sightings:
        grouped_ids = [str(gid) for gid in s.get("grouped_message_ids", []) if str(gid) in msg_by_id]
        primary_id = str(s.get("primary_message_id") or (grouped_ids[0] if grouped_ids else ""))
        if not primary_id or primary_id not in msg_by_id:
            continue
        if primary_id not in grouped_ids:
            grouped_ids.insert(0, primary_id)

        pin_id = f"discord-{primary_id}"
        if pin_id in existing_ids:
            continue

        primary_msg = msg_by_id[primary_id]
        media_urls: list[str] = []
        photo_bytes_list: list[bytes] = []
        exif_gps: tuple[float, float] | None = None

        for gid in grouped_ids:
            m = msg_by_id[gid]
            img_idx = 0
            for att in m.get("attachments", []):
                if not is_image_attachment(att):
                    continue
                img_idx += 1
                filename = f"discord-{gid}-{img_idx}.jpg"
                jpeg_bytes, img_gps = download_and_resize_image(att["url"], photos_dir / filename)
                if len(photo_bytes_list) < 4:
                    photo_bytes_list.append(jpeg_bytes)
                if exif_gps is None and img_gps is not None:
                    exif_gps = img_gps
                media_urls.append(f"{public_base_url}/photos/{filename}")
            for yt_url in extract_youtube_urls(m.get("content", "")):
                if yt_url not in media_urls:
                    media_urls.append(yt_url)

        if not media_urls:
            continue

        ts_dt = dt.datetime.fromisoformat(primary_msg["timestamp"])
        posted_at = ts_dt.strftime("%Y-%m-%d")
        author = (
            primary_msg.get("author", {}).get("global_name")
            or primary_msg.get("author", {}).get("username")
            or "Discord Member"
        )
        caption = str(s.get("caption") or "").strip()

        place_name = str(s.get("place_name") or "").strip()
        location_query = str(s.get("location_query") or "").strip()
        fallback_queries = [
            str(q).strip() for q in (s.get("fallback_queries") or []) if str(q).strip()
        ]
        visual_evidence = ""

        is_ambiguous = bool(s.get("needs_visual_check")) or (bool(location_query) and "," not in location_query)
        lat: float | None = None
        lng: float | None = None
        matched_query = ""
        primary_query = location_query

        if location_query and not is_ambiguous:
            print(f"Geocoding {pin_id}: {location_query}", flush=True)
            lat, lng, matched_query = geocode_with_fallbacks(geocoder, location_query, fallback_queries)

        if (lat is None or lng is None or is_ambiguous) and photo_bytes_list:
            author_context = _build_author_context(author, posted_at, approved_pins_state, session_sightings)
            try:
                vis = infer_location_from_photos_and_context(
                    [resize_for_vision(b) for b in photo_bytes_list[:4]],
                    gemini_key,
                    caption=caption,
                    tentative_query=location_query,
                    author_context=author_context,
                    exif_gps=exif_gps,
                )
                visual_evidence = str(vis.get("visual_evidence") or "").strip()
                if vis.get("location_query"):
                    location_query = str(vis["location_query"]).strip()
                    primary_query = location_query
                    if vis.get("place_name"):
                        place_name = str(vis["place_name"]).strip()
                    elif not place_name:
                        place_name = location_query
                    vis_fallbacks = [str(q).strip() for q in (vis.get("fallback_queries") or []) if str(q).strip()]
                    fallback_queries = vis_fallbacks + [q for q in fallback_queries if q not in vis_fallbacks]
                    lat = None
                    lng = None
            except Exception as exc:
                print(f"Vision fallback skipped for {pin_id}: {exc}", flush=True)

        if (lat is None or lng is None) and location_query:
            print(f"Geocoding {pin_id}: {location_query}", flush=True)
            lat, lng, matched_query = geocode_with_fallbacks(geocoder, location_query, fallback_queries)

        if lat is not None and lng is not None:
            status = "pending"
            note_parts = ["Auto-geocoded"]
            if primary_query and matched_query.lower() != primary_query.lower():
                note_parts[0] += f" via fallback '{matched_query}'"
            if visual_evidence:
                note_parts.append(f"[vision: {visual_evidence}]")
            if exif_gps:
                if max(abs(lat - exif_gps[0]), abs(lng - exif_gps[1])) > 0.25:
                    note_parts.append(f"[WARNING: EXIF GPS differs ({exif_gps[0]:.5f}, {exif_gps[1]:.5f})]")
                else:
                    note_parts.append("[matches EXIF GPS]")
            notes = " ".join(note_parts) + "; change status to approved to publish"
            session_sightings.append((author, posted_at, place_name or location_query))
        elif exif_gps is not None:
            lat, lng = exif_gps
            if not place_name:
                place_name = geocoder.reverse_geocode(lat, lng)
            status = "pending"
            notes = f"From photo EXIF GPS ({lat:.5f}, {lng:.5f}) — verify location before approving"
            session_sightings.append((author, posted_at, place_name))
        else:
            status = "needs_geocoding"
            notes = "Could not geocode — enter location_query or lat/lng, or set status=ignored"

        new_rows.append(
            {
                "id": pin_id,
                "status": status,
                "place_name": place_name or location_query,
                "location_query": location_query,
                "lat": lat if lat is not None else "",
                "lng": lng if lng is not None else "",
                "caption": caption,
                "author": author,
                "posted_at": posted_at,
                "layer": ts_dt.strftime("%Y"),
                "media_urls": " ".join(media_urls),
                "discord_url": build_discord_jump_url(primary_msg.get("guild_id"), channel_id, primary_id),
                "notes": notes,
            }
        )
        existing_ids.add(pin_id)

    batch_result = post_webhook(
        webhook_url,
        webhook_secret,
        {
            "action": "sync_batch",
            "new_rows": new_rows,
            "geocoded_updates": geocoded_updates,
            "last_message_id": new_cursor or last_message_id,
        },
    )

    approved_pins = batch_result.get("approved_pins", [])
    write_static_cache(approved_pins, data_dir)

    alert_out = os.environ.get("ALERT_JSON_PATH", "").strip()
    if alert_out:
        sheet_url = str(batch_result.get("sheet_url") or DEFAULT_SHEET_URL)
        alert_payload = build_alert_issue_payload(new_rows, geocoded_updates, sheet_url)
        if alert_payload is not None:
            Path(alert_out).write_text(json.dumps(alert_payload, indent=2), encoding="utf-8")

    print(
        f"Sync complete: {len(new_rows)} new rows, {len(geocoded_updates)} geocoded retries, "
        f"{len(approved_pins)} total approved pins."
    )


def main() -> None:
    run_sync(Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()
