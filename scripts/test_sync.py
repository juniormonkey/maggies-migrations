"""Unit tests for KML migration and Discord sync pipeline."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import migrate_kml
import sync_discord


class TestMigrateKml(unittest.TestCase):
    def test_parse_kml_coordinates_order(self) -> None:
        lat, lng = migrate_kml.parse_kml_coordinates("-122.5811177,37.8970199,0")
        self.assertAlmostEqual(lat, 37.8970199)
        self.assertAlmostEqual(lng, -122.5811177)

    def test_extract_caption_strips_html_and_youtube(self) -> None:
        raw = (
            '<img src="https://example.com/a.jpg" height="200" /><br><br>'
            '<a href="https://www.youtube.com/embed/1oofSFaYUrk">https://www.youtube.com/embed/1oofSFaYUrk</a><br>'
            "Here we see Maggie at a castle."
        )
        self.assertEqual(migrate_kml.extract_caption(raw), "Here we see Maggie at a castle.")


class TestSyncDiscord(unittest.TestCase):
    def test_tail_holdback_defers_recent_bare_photo(self) -> None:
        now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
        msgs = [
            {
                "id": "100",
                "timestamp": "2026-11-16T10:00:00+00:00",
                "content": "Maggie in Tokyo!",
                "attachments": [{"content_type": "image/jpeg", "filename": "a.jpg"}],
            },
            {
                "id": "101",
                "timestamp": "2026-11-16T11:55:00+00:00",
                "content": "",
                "attachments": [{"content_type": "image/png", "filename": "b.png"}],
            },
        ]
        ready, cursor = sync_discord.split_tail_holdback(msgs, now_utc=now, holdback_minutes=15)
        self.assertEqual([m["id"] for m in ready], ["100"])
        self.assertEqual(cursor, "100")

    def test_tail_holdback_processes_old_bare_photo(self) -> None:
        now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
        msgs = [
            {
                "id": "101",
                "timestamp": "2026-11-16T11:30:00+00:00",
                "content": "",
                "attachments": [{"content_type": "image/png", "filename": "b.png"}],
            }
        ]
        ready, cursor = sync_discord.split_tail_holdback(msgs, now_utc=now, holdback_minutes=15)
        self.assertEqual([m["id"] for m in ready], ["101"])
        self.assertEqual(cursor, "101")

    def test_ensure_all_media_posts_captured_never_drops_photo(self) -> None:
        msgs = [
            {
                "id": "200",
                "content": "",
                "attachments": [{"content_type": "image/jpeg", "filename": "maggie.jpg"}],
            },
            {
                "id": "201",
                "content": "So cute!",
                "attachments": [],
            },
        ]
        captured = sync_discord.ensure_all_media_posts_captured(msgs, [])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["primary_message_id"], "200")
        self.assertEqual(captured[0]["location_query"], "")

    def test_nominatim_hit_and_miss(self) -> None:
        limiter = sync_discord.NominatimLimiter(min_interval=0.0)
        with mock.patch("sync_discord.requests.get") as mock_get:
            resp_hit = mock.MagicMock()
            resp_hit.json.return_value = [{"lat": "48.8583701", "lon": "2.2944813"}]
            resp_miss = mock.MagicMock()
            resp_miss.json.return_value = []
            mock_get.side_effect = [resp_hit, resp_miss]

            lat, lng = limiter.geocode("Eiffel Tower")
            self.assertAlmostEqual(lat, 48.8583701)
            self.assertAlmostEqual(lng, 2.2944813)

            miss_lat, miss_lng = limiter.geocode("Nonexistent Nowhere Place")
            self.assertIsNone(miss_lat)
            self.assertIsNone(miss_lng)

    def test_download_resize_and_write_static_cache(self) -> None:
        buf = io.BytesIO()
        Image.new("RGBA", (2400, 1200), (10, 100, 200, 255)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dest = root / "photos" / "test.jpg"
            with mock.patch("sync_discord.requests.get") as mock_get:
                resp = mock.MagicMock()
                resp.content = png_bytes
                mock_get.return_value = resp
                sync_discord.download_and_resize_image("https://cdn.example.com/a.png", dest, max_dim=1600)

            with Image.open(dest) as out_img:
                self.assertEqual(out_img.format, "JPEG")
                self.assertEqual(out_img.size, (1600, 800))

            pins = [
                {
                    "id": "discord-99",
                    "place_name": "Eiffel Tower",
                    "lat": 48.8584,
                    "lng": 2.2945,
                    "caption": "Bonjour",
                    "author": "Alice",
                    "posted_at": "2026-11-16",
                    "layer": "2026",
                    "media": ["https://example.com/test.jpg"],
                }
            ]
            data_dir = root / "data"
            sync_discord.write_static_cache(pins, data_dir)
            loaded = json.loads((data_dir / "pins.json").read_text(encoding="utf-8"))
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["place_name"], "Eiffel Tower")

            with (data_dir / "pins.csv").open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "approved")
            self.assertEqual(rows[0]["media_urls"], "https://example.com/test.jpg")

    def test_code_gs_only_current_doc_and_no_mail_scopes(self) -> None:
        code_path = Path(__file__).resolve().parent.parent / "apps_script" / "Code.gs"
        source = code_path.read_text(encoding="utf-8")
        self.assertTrue(source.lstrip().startswith("/**\n * @OnlyCurrentDoc\n */"))
        for forbidden in ("MailApp", "GmailApp", "Session", "DriveApp", "UrlFetchApp", "openById"):
            self.assertNotIn(forbidden, source)

    def test_build_alert_issue_payload(self) -> None:
        sheet_url = "https://docs.google.com/spreadsheets/d/test/edit"
        self.assertIsNone(sync_discord.build_alert_issue_payload([], [], sheet_url))

        new_rows = [
            {
                "id": "discord-10",
                "status": "needs_geocoding",
                "place_name": "",
                "location_query": "",
                "caption": "Mystery fountain",
                "author": "Alice",
                "posted_at": "2026-11-16",
                "media_urls": "https://juniormonkey.github.io/maggies-migrations/photos/discord-10-1.jpg",
                "discord_url": "https://discord.com/channels/1/2/10",
            },
            {
                "id": "discord-11",
                "status": "pending",
                "place_name": "Eiffel Tower",
                "location_query": "Eiffel Tower, Paris",
                "caption": "Maggie in Paris",
                "author": "Bob",
                "posted_at": "2026-11-16",
                "media_urls": "https://juniormonkey.github.io/maggies-migrations/photos/discord-11-1.jpg",
                "discord_url": "https://discord.com/channels/1/2/11",
            },
        ]
        alert = sync_discord.build_alert_issue_payload(new_rows, [], sheet_url)
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["title"], "[Maggie's Migrations] 1 needs geocoding, 1 awaiting approval")
        self.assertEqual(alert["assignees"], ["juniormonkey"])
        self.assertIn("@juniormonkey", alert["body"])
        self.assertIn(sheet_url, alert["body"])
        self.assertIn("https://discord.com/channels/1/2/10", alert["body"])
        self.assertIn("https://juniormonkey.github.io/maggies-migrations/photos/discord-10-1.jpg", alert["body"])


if __name__ == "__main__":
    unittest.main()
