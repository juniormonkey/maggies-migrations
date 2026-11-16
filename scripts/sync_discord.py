"""Scheduled sync from Discord channel 1493308738755625060 to Google Sheets + GitHub Pages."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import json
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


def download_and_resize_image(url: str, dest_path: Path, max_dim: int = 1600) -> bytes:
    """Download image URL, normalize EXIF orientation, resize, and write JPEG."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with Image.open(io.BytesIO(resp.content)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        jpeg_bytes = buf.getvalue()
    dest_path.write_bytes(jpeg_bytes)
    return jpeg_bytes


def resize_for_vision(jpeg_bytes: bytes, max_dim: int = 512) -> str:
    """Create a small base64 JPEG thumbnail for optional Gemini visual landmark fallback."""
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode("ascii")


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
        "3. Ignore pure off-topic chatter that has image_count=0, has_youtube=false, and doesn't explain an adjacent photo.\n"
        "4. Every message with image_count > 0 or has_youtube=true MUST either be the primary_message_id of a sighting "
        "or included inside grouped_message_ids of an adjacent sighting.\n"
        "5. Return ONLY a JSON array of objects with keys:\n"
        '   - "primary_message_id": string (first/photo message id in the group)\n'
        '   - "grouped_message_ids": array of all message ids belonging to this sighting\n'
        '   - "is_sighting": boolean\n'
        '   - "place_name": concise clean display title (e.g. "Eiffel Tower, Paris" or "" if unknown)\n'
        '   - "location_query": geocodable place/address string for OpenStreetMap Nominatim (or "" if location cannot be determined)\n'
        '   - "caption": cleaned human story/notes combining relevant text from grouped messages (without bare URLs)\n\n'
        f"Messages JSON:\n{json.dumps(compact, ensure_ascii=False)}"
    )

    resp = requests.post(
        f"{GEMINI_API}?key={api_key}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        },
        timeout=60,
    )
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])
    raw_text = candidates[0]["content"]["parts"][0]["text"]
    parsed = json.loads(raw_text)
    if not isinstance(parsed, list):
        raise ValueError(f"Expected JSON list from Gemini, got {type(parsed)}")
    return [item for item in parsed if item.get("is_sighting", True)]


def infer_location_from_photo(jpeg_b64: str, api_key: str) -> tuple[str, str]:
    """Use Gemini Vision only when text lacked a location, returning (place_name, location_query) or ('', '')."""
    prompt = (
        "This photo shows a mascot toy magpie traveling somewhere. ONLY if there is an unmistakable landmark, "
        "street sign, museum sign, airport code, or recognizable monument that uniquely identifies the city/place, "
        'return {"place_name": "...", "location_query": "..."}. If uncertain, return {"place_name": "", "location_query": ""}.'
    )
    resp = requests.post(
        f"{GEMINI_API}?key={api_key}",
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/jpeg", "data": jpeg_b64}},
                    ]
                }
            ],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    return str(data.get("place_name") or "").strip(), str(data.get("location_query") or "").strip()


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

    def geocode(self, query: str) -> tuple[float | None, float | None]:
        clean = query.strip()
        if not clean:
            return None, None
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

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


def build_discord_jump_url(guild_id: str | None, channel_id: str, message_id: str) -> str:
    """Build a Discord jump link when guild_id is available."""
    guild_segment = guild_id or "@me"
    return f"https://discord.com/channels/{guild_segment}/{channel_id}/{message_id}"


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


def process_messages_in_chunks(
    messages: list[dict[str, Any]],
    api_key: str,
    chunk_size: int = 40,
) -> list[dict[str, Any]]:
    """Run Gemini grouping across chunks of up to `chunk_size` messages."""
    all_sightings: list[dict[str, Any]] = []
    for i in range(0, len(messages), chunk_size):
        chunk = messages[i : i + chunk_size]
        grouped = call_gemini_group_sightings(chunk, api_key)
        all_sightings.extend(ensure_all_media_posts_captured(chunk, grouped))
    return all_sightings


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

    geocoder = NominatimLimiter()
    geocoded_updates: list[dict[str, Any]] = []
    for item in retry_geocoding:
        lat, lng = geocoder.geocode(str(item.get("location_query") or ""))
        if lat is not None and lng is not None:
            geocoded_updates.append(
                {
                    "id": item["id"],
                    "place_name": item.get("place_name") or item["location_query"],
                    "status": "pending",
                    "lat": lat,
                    "lng": lng,
                    "notes": "Geocoded from updated location_query — change status to approved to publish",
                }
            )

    raw_messages = fetch_discord_messages(channel_id, discord_token, last_message_id)
    messages, new_cursor = split_tail_holdback(raw_messages)

    msg_by_id = {m["id"]: m for m in messages}
    sightings = process_messages_in_chunks(messages, gemini_key) if messages else []

    new_rows: list[dict[str, Any]] = []
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
        first_jpeg_bytes: bytes | None = None

        for gid in grouped_ids:
            m = msg_by_id[gid]
            img_idx = 0
            for att in m.get("attachments", []):
                if not is_image_attachment(att):
                    continue
                img_idx += 1
                filename = f"discord-{gid}-{img_idx}.jpg"
                jpeg_bytes = download_and_resize_image(att["url"], photos_dir / filename)
                if first_jpeg_bytes is None:
                    first_jpeg_bytes = jpeg_bytes
                media_urls.append(f"{public_base_url}/photos/{filename}")
            for yt_url in extract_youtube_urls(m.get("content", "")):
                if yt_url not in media_urls:
                    media_urls.append(yt_url)

        if not media_urls:
            continue

        place_name = str(s.get("place_name") or "").strip()
        location_query = str(s.get("location_query") or "").strip()

        if not location_query and first_jpeg_bytes is not None:
            vis_place, vis_query = infer_location_from_photo(resize_for_vision(first_jpeg_bytes), gemini_key)
            if vis_query:
                place_name = place_name or vis_place
                location_query = vis_query

        lat, lng = geocoder.geocode(location_query) if location_query else (None, None)
        if lat is not None and lng is not None:
            status = "pending"
            notes = "Auto-geocoded; change status to approved to publish"
        else:
            status = "needs_geocoding"
            notes = "Could not geocode — enter location_query or lat/lng, or set status=ignored"

        ts_dt = dt.datetime.fromisoformat(primary_msg["timestamp"])
        author = (
            primary_msg.get("author", {}).get("global_name")
            or primary_msg.get("author", {}).get("username")
            or "Discord Member"
        )
        new_rows.append(
            {
                "id": pin_id,
                "status": status,
                "place_name": place_name or location_query,
                "location_query": location_query,
                "lat": lat if lat is not None else "",
                "lng": lng if lng is not None else "",
                "caption": str(s.get("caption") or "").strip(),
                "author": author,
                "posted_at": ts_dt.strftime("%Y-%m-%d"),
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
    print(
        f"Sync complete: {len(new_rows)} new rows, {len(geocoded_updates)} geocoded retries, "
        f"{len(approved_pins)} total approved pins."
    )


def main() -> None:
    run_sync(Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()
