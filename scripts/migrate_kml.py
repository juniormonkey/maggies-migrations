"""One-time migration of existing Google My Maps KML pins and photos."""

from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_KML_PATH = Path(
    "/Users/martin/.gemini/antigravity/brain/7fd32d8f-2bdf-4ba7-abdb-fc020bed88e7/"
    "browser_sandbox/downloads/Maggie_s_Migrations.kml"
)
LIVE_KML_URL = "https://www.google.com/maps/d/kml?mid=1woxkTDW_WPqFLeNdwEcUlcoV7Qw7oLM&forcekml=1"
PUBLIC_BASE_URL = "https://juniormonkey.github.io/maggies-migrations"
KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def parse_kml_coordinates(coord_text: str) -> tuple[float, float]:
    """Parse KML 'lng,lat,alt' coordinates into (lat, lng)."""
    parts = [p.strip() for p in coord_text.strip().split(",") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"Invalid KML coordinates: {coord_text!r}")
    lng = float(parts[0])
    lat = float(parts[1])
    return lat, lng


def extract_caption(description_html: str) -> str:
    """Strip HTML tags and embedded YouTube URLs from KML description HTML."""
    if not description_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", description_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"https?://(?:www\.)?youtube\.com/\S+", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_youtube_url(url: str) -> str:
    """Convert YouTube embed/shorts/youtu.be URLs to canonical watch URLs."""
    match = re.search(
        r"(?:youtube\.com/(?:embed/|shorts/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{6,})",
        url,
    )
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url


def download_bytes(url: str) -> bytes:
    """Download URL bytes with a browser User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return resp.read()


def convert_to_web_jpeg(raw_bytes: bytes, dest_path: Path) -> None:
    """Resize and convert raw image bytes into optimized JPEG using macOS sips."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [
                "/usr/bin/sips",
                "-Z",
                "1600",
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                "82",
                str(tmp_path),
                "--out",
                str(dest_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"sips failed ({result.returncode}): {result.stderr or result.stdout}")
        if not dest_path.exists() or dest_path.stat().st_size <= 2000:
            size = dest_path.stat().st_size if dest_path.exists() else 0
            raise RuntimeError(f"Output JPEG invalid or too small ({size} bytes): {dest_path}")
    finally:
        tmp_path.unlink(missing_ok=True)


def parse_placemarks(kml_xml: bytes) -> list[dict[str, object]]:
    """Extract raw placemark dictionaries from KML XML bytes."""
    root = ET.fromstring(kml_xml)
    placemarks: list[dict[str, object]] = []
    for folder in root.findall(".//kml:Folder", KML_NS):
        layer_el = folder.find("kml:name", KML_NS)
        layer = (layer_el.text or "2026").strip() if layer_el is not None else "2026"
        for pm in folder.findall("kml:Placemark", KML_NS):
            name_el = pm.find("kml:name", KML_NS)
            desc_el = pm.find("kml:description", KML_NS)
            coord_el = pm.find(".//kml:Point/kml:coordinates", KML_NS)
            if name_el is None or coord_el is None or not coord_el.text:
                continue
            lat, lng = parse_kml_coordinates(coord_el.text)
            media_raw: list[str] = []
            for data_el in pm.findall(".//kml:ExtendedData/kml:Data", KML_NS):
                if data_el.attrib.get("name") == "gx_media_links":
                    val_el = data_el.find("kml:value", KML_NS)
                    if val_el is not None and val_el.text:
                        media_raw.extend(val_el.text.split())
            placemarks.append(
                {
                    "place_name": (name_el.text or "").strip(),
                    "caption": extract_caption(desc_el.text if desc_el is not None and desc_el.text else ""),
                    "lat": round(lat, 7),
                    "lng": round(lng, 7),
                    "layer": layer,
                    "media_raw": media_raw,
                }
            )
    return placemarks


def write_outputs(pins: list[dict[str, object]], data_dir: Path) -> None:
    """Write pins.json, pins.csv, and initial config.json under docs/data."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "pins.json").open("w", encoding="utf-8") as fh:
        json.dump(pins, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

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
            media_list = pin.get("media", [])
            writer.writerow(
                {
                    "id": pin["id"],
                    "status": "approved",
                    "place_name": pin["place_name"],
                    "location_query": pin["place_name"],
                    "lat": pin["lat"],
                    "lng": pin["lng"],
                    "caption": pin["caption"],
                    "author": pin["author"],
                    "posted_at": pin["posted_at"],
                    "layer": pin["layer"],
                    "media_urls": " ".join(media_list) if isinstance(media_list, list) else "",
                    "discord_url": "",
                    "notes": "Migrated from Google My Maps",
                }
            )

    config_path = data_dir / "config.json"
    if not config_path.exists():
        with config_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "liveSheetUrl": "",
                    "mapTitle": "Maggie's Migrations",
                    "mapSubtitle": "Where has Maggie flown to recently?",
                },
                fh,
                indent=2,
            )
            fh.write("\n")


def migrate(kml_path: Path, repo_root: Path) -> list[dict[str, object]]:
    """Run full KML migration into docs/photos and docs/data."""
    kml_bytes = kml_path.read_bytes() if kml_path.exists() else download_bytes(LIVE_KML_URL)
    raw_placemarks = parse_placemarks(kml_bytes)

    photos_dir = repo_root / "docs" / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    pins: list[dict[str, object]] = []
    refreshed_kml_placemarks: list[dict[str, object]] | None = None

    for pin_idx, pm in enumerate(raw_placemarks, start=1):
        media_urls: list[str] = []
        img_counter = 0
        raw_links = list(pm["media_raw"])  # type: ignore[arg-type]
        for media_idx, url in enumerate(raw_links):
            if "youtube.com" in url or "youtu.be" in url:
                media_urls.append(normalize_youtube_url(url))
                continue

            img_counter += 1
            dest_name = f"legacy-{pin_idx}-{img_counter}.jpg"
            dest_path = photos_dir / dest_name
            try:
                raw_img = download_bytes(url)
            except (urllib.error.URLError, RuntimeError):
                if refreshed_kml_placemarks is None:
                    refreshed_kml_placemarks = parse_placemarks(download_bytes(LIVE_KML_URL))
                fresh_url = str(refreshed_kml_placemarks[pin_idx - 1]["media_raw"][media_idx])  # type: ignore[index]
                raw_img = download_bytes(fresh_url)

            convert_to_web_jpeg(raw_img, dest_path)
            media_urls.append(f"{PUBLIC_BASE_URL}/photos/{dest_name}")

        pins.append(
            {
                "id": f"legacy-{pin_idx}",
                "place_name": pm["place_name"],
                "lat": pm["lat"],
                "lng": pm["lng"],
                "caption": pm["caption"],
                "author": "Curiosity Guild",
                "posted_at": "2026-10-19",
                "layer": pm["layer"],
                "media": media_urls,
            }
        )

    write_outputs(pins, repo_root / "docs" / "data")
    return pins


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    kml_path = Path(os.environ.get("KML_PATH", str(DEFAULT_KML_PATH)))
    pins = migrate(kml_path, repo_root)
    photo_count = sum(1 for p in (repo_root / "docs" / "photos").glob("legacy-*.jpg"))
    print(f"Migrated {len(pins)} pins and {photo_count} JPEG photos.")


if __name__ == "__main__":
    main()
