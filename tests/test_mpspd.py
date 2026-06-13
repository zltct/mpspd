import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import mpspd


class MpspdCoreTests(unittest.TestCase):
    def test_parse_seed_url_accepts_image_resize_suffix(self):
        seed = mpspd.parse_seed_url(
            "https://images.meupatrocinio.com/325966/23670390/99/width=480,height=480"
        )

        self.assertEqual(seed.base_url, "https://images.meupatrocinio.com")
        self.assertEqual(seed.profile_id, 325966)
        self.assertEqual(seed.photo_id, 23670390)
        self.assertEqual(seed.photo_number, 99)
        self.assertEqual(seed.url, "https://images.meupatrocinio.com/325966/23670390/99/")

    def test_seed_state_from_local_images_uses_latest_fixture(self):
        with TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            (workdir / "83_14582353_325966.jpeg").write_bytes(b"old")
            (workdir / "99_23670390_325966.jpeg").write_bytes(b"latest")
            output_dir = workdir / "public"

            with patch("pathlib.Path.cwd", return_value=workdir):
                state = mpspd.seed_state_from_local_images(output_dir, increment=1)

            self.assertIsNotNone(state)
            self.assertEqual(state.profile_id, 325966)
            self.assertEqual(state.next_photo_id, 23670391)
            self.assertEqual(state.next_photo_number, 100)
            records = mpspd.load_found_records(output_dir / mpspd.FOUND_FILE)
            self.assertEqual(len(records), 2)

    def test_save_load_state_roundtrip(self):
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / mpspd.STATE_FILE
            state = mpspd.ScanState(
                base_url="https://images.example.test",
                profile_id=123,
                next_photo_id=456,
                next_photo_number=7,
                increment=1,
            )

            mpspd.save_state(state_path, state)
            loaded = mpspd.load_state(state_path)

            self.assertEqual(loaded.base_url, state.base_url)
            self.assertEqual(loaded.profile_id, 123)
            self.assertEqual(loaded.next_photo_id, 456)
            self.assertIsNotNone(loaded.updated_at)

    def test_render_index_contains_found_links_and_status(self):
        with TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / mpspd.INDEX_FILE
            state = mpspd.ScanState(
                base_url="https://images.example.test",
                profile_id=325966,
                next_photo_id=23670391,
                next_photo_number=100,
                increment=1,
                scanned=50,
                found=1,
                last_status="found",
            )
            record = mpspd.FoundRecord(
                url="https://images.example.test/325966/23670390/99/",
                profile_id=325966,
                photo_id=23670390,
                photo_number=99,
                content_type="image/jpeg",
                content_length=123,
                status=200,
                discovered_at="2026-06-13T00:00:00+00:00",
            )

            mpspd.render_index(index_path, state, [record])

            html = index_path.read_text(encoding="utf-8")
            self.assertIn("MPSPD found links", html)
            self.assertIn(record.url, html)
            self.assertIn("scanned: 50", html)

    def test_render_index_includes_manual_links_file(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            index_path = output_dir / mpspd.INDEX_FILE
            (output_dir / mpspd.MANUAL_FILE).write_text(
                "# skipped links\n"
                "https://images.example.test/325966/14582353/83/width=480,height=480\n",
                encoding="utf-8",
            )
            state = mpspd.ScanState(
                base_url="https://images.example.test",
                profile_id=325966,
                next_photo_id=14582352,
                next_photo_number=82,
                increment=-1,
            )

            mpspd.render_index(index_path, state, [])

            html = index_path.read_text(encoding="utf-8")
            self.assertIn("manual: 1", html)
            self.assertIn("displayed: 1", html)
            self.assertIn("https://images.example.test/325966/14582353/83/", html)
            self.assertIn(mpspd.MANUAL_FILE, html)

    def test_init_command_can_seed_from_url(self):
        with TemporaryDirectory() as temp_dir:
            rc = mpspd.main(
                [
                    "init",
                    "--seed-url",
                    "https://images.meupatrocinio.com/325966/23670390/99/",
                    "--output-dir",
                    temp_dir,
                ]
            )

            self.assertEqual(rc, 0)
            state = json.loads((Path(temp_dir) / mpspd.STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["profile_id"], 325966)
            self.assertTrue((Path(temp_dir) / mpspd.INDEX_FILE).exists())

    def test_scan_command_finds_image_from_fake_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                if self.path == "/325966/23670390/99/":
                    self.send_response(200)
                    self.send_header("content-type", "image/jpeg")
                    self.send_header("content-length", "12")
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as temp_dir:
                seed_url = f"http://127.0.0.1:{server.server_port}/325966/23670390/99/"
                rc = mpspd.main(
                    [
                        "scan",
                        "--seed-url",
                        seed_url,
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "2",
                        "--concurrency",
                        "2",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                    ]
                )

                self.assertEqual(rc, 0)
                records = mpspd.load_found_records(Path(temp_dir) / mpspd.FOUND_FILE)
                state = mpspd.load_state(Path(temp_dir) / mpspd.STATE_FILE)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].photo_id, 23670390)
                self.assertEqual(state.found, 1)
                self.assertEqual(state.next_photo_id, 23670389)
                self.assertEqual(state.next_photo_number, 98)
                self.assertTrue((Path(temp_dir) / mpspd.INDEX_FILE).exists())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
