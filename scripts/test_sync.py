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
                jpeg_bytes, exif_gps = sync_discord.download_and_resize_image(
                    "https://cdn.example.com/a.png", dest, max_dim=1600
                )
            self.assertGreater(len(jpeg_bytes), 0)
            self.assertIsNone(exif_gps)

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

    def test_split_into_chunks_keeps_same_author_followup_together(self) -> None:
        base_ts = dt.datetime(2026, 11, 16, 10, 0, tzinfo=dt.timezone.utc)
        msgs = []
        for idx in range(25):
            msgs.append(
                {
                    "id": str(100 + idx),
                    "timestamp": (base_ts + dt.timedelta(minutes=idx * 5)).isoformat(),
                    "author": {"id": f"user-{idx}"},
                    "content": f"post {idx}",
                    "attachments": [],
                }
            )
        # Index 25 is a follow-up from user-24 two minutes after message 24
        msgs.append(
            {
                "id": "125",
                "timestamp": (base_ts + dt.timedelta(minutes=24 * 5 + 2)).isoformat(),
                "author": {"id": "user-24"},
                "content": "That was in Golden Gate Park!",
                "attachments": [],
            }
        )
        # Index 26 is from another user
        msgs.append(
            {
                "id": "126",
                "timestamp": (base_ts + dt.timedelta(minutes=24 * 5 + 3)).isoformat(),
                "author": {"id": "user-99"},
                "content": "Another location",
                "attachments": [],
            }
        )

        chunks = sync_discord.split_into_chunks(msgs, target_size=25, max_size=35)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([m["id"] for m in chunks[0][-2:]], ["124", "125"])
        self.assertEqual([m["id"] for m in chunks[1]], ["126"])

    def test_gemini_post_retries_on_timeout_and_uses_header(self) -> None:
        import requests

        ok_resp = mock.MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "[]"}]}}]
        }
        msgs = [
            {
                "id": "1",
                "timestamp": "2026-11-16T10:00:00+00:00",
                "author": {"id": "u1", "username": "Alice"},
                "content": "hi",
                "attachments": [],
            }
        ]
        with (
            mock.patch("sync_discord.time.sleep") as mock_sleep,
            mock.patch("sync_discord.requests.post", side_effect=[requests.ReadTimeout("timeout"), ok_resp]) as mock_post,
        ):
            res = sync_discord.call_gemini_group_sightings(msgs, "secret-key")

        self.assertEqual(res, [])
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(mock_post.call_args.args[0], sync_discord.GEMINI_API)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "secret-key")
        self.assertEqual(kwargs["json"]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 512})

    def test_extract_exif_gps_dms_and_null_island(self) -> None:
        img = Image.new("RGB", (64, 64), (10, 20, 30))
        exif = img.getexif()
        exif[0x8825] = {
            1: "N",
            2: (37.0, 47.0, 49.92),
            3: "W",
            4: (122.0, 25.0, 9.12),
        }
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())
        jpg_with_gps = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "gps.jpg"
            with mock.patch("sync_discord.requests.get") as mock_get:
                resp = mock.MagicMock()
                resp.content = jpg_with_gps
                mock_get.return_value = resp
                _, gps = sync_discord.download_and_resize_image("https://cdn.example.com/gps.jpg", dest)

            self.assertIsNotNone(gps)
            assert gps is not None
            self.assertAlmostEqual(gps[0], 37.7972, places=4)
            self.assertAlmostEqual(gps[1], -122.4192, places=4)

            with Image.open(dest) as saved:
                self.assertIsNone(sync_discord.extract_exif_gps(saved))

        null_img = Image.new("RGB", (32, 32), (0, 0, 0))
        null_exif = null_img.getexif()
        null_exif[0x8825] = {1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)}
        null_buf = io.BytesIO()
        null_img.save(null_buf, format="JPEG", exif=null_exif.tobytes())
        with Image.open(io.BytesIO(null_buf.getvalue())) as reopened_null:
            self.assertIsNone(sync_discord.extract_exif_gps(reopened_null))

    def test_geocode_with_fallbacks_handles_unindexed_poi(self) -> None:
        geocoder = sync_discord.NominatimLimiter(min_interval=0.0)
        with mock.patch.object(
            geocoder,
            "geocode",
            side_effect=[
                (None, None),
                (-37.8142454, 144.9631732),
            ],
        ) as mock_geo:
            lat, lng, matched = sync_discord.geocode_with_fallbacks(
                geocoder, "Koori Heritage Trust, Melbourne, Australia"
            )
        self.assertAlmostEqual(lat, -37.8142454)
        self.assertAlmostEqual(lng, 144.9631732)
        self.assertEqual(matched, "Melbourne, Australia")
        self.assertEqual(mock_geo.call_count, 2)

    def test_vision_multi_photo_1280px_and_context(self) -> None:
        buf = io.BytesIO()
        Image.new("RGB", (2400, 1600), (80, 90, 100)).save(buf, format="JPEG")
        b64_img = sync_discord.resize_for_vision(buf.getvalue())
        with Image.open(io.BytesIO(sync_discord.base64.b64decode(b64_img))) as resized:
            self.assertEqual(resized.size, (1280, 853))

        fake_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "place_name": "Ocean Beach, San Francisco",
                                        "location_query": "Ocean Beach, San Francisco, CA",
                                        "fallback_queries": ["San Francisco, CA"],
                                        "visual_evidence": "Muni bus sign visible in background",
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        with mock.patch("sync_discord._gemini_post", return_value=fake_resp) as mock_post:
            res = sync_discord.infer_location_from_photos_and_context(
                [b64_img, b64_img, b64_img],
                "secret-key",
                caption="Maggie at Ocean Beach",
                tentative_query="Ocean Beach",
                author_context=["2026-10-16: Cable Car Museum, San Francisco"],
                exif_gps=(37.7594, -122.5107),
            )

        self.assertEqual(res["location_query"], "Ocean Beach, San Francisco, CA")
        self.assertEqual(res["visual_evidence"], "Muni bus sign visible in background")
        self.assertEqual(res["fallback_queries"], ["San Francisco, CA"])
        parts = mock_post.call_args.args[0]["contents"][0]["parts"]
        self.assertEqual(len(parts), 4)
        self.assertIn("Cable Car Museum, San Francisco", parts[0]["text"])
        self.assertIn("37.75940, -122.51070", parts[0]["text"])


if __name__ == "__main__":
    unittest.main()
