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

    def test_probe_error_label_groups_connection_reset(self):
        self.assertEqual(mpspd.probe_error_label(ConnectionResetError()), "conn_reset")

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
            def do_GET(self):
                if self.path == "/325966/23670390/99/":
                    self.send_response(200)
                    self.send_header("content-type", "image/jpeg")
                    self.send_header("content-length", "12")
                    self.end_headers()
                    self.wfile.write(b"x")
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

    def test_scan_resumes_from_last_missed_probe_after_found_image(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/325966/15078616/84/":
                    self.send_response(200)
                    self.send_header("content-type", "image/jpeg")
                    self.send_header("content-length", "12")
                    self.end_headers()
                    self.wfile.write(b"x")
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
                seed_url = f"http://127.0.0.1:{server.server_port}/325966/15078616/84/"
                rc = mpspd.main(
                    [
                        "scan",
                        "--seed-url",
                        seed_url,
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "4",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                        "--health-check-interval",
                        "0",
                    ]
                )

                self.assertEqual(rc, 0)
                state = mpspd.load_state(Path(temp_dir) / mpspd.STATE_FILE)
                self.assertEqual(state.last_found_url, seed_url)
                self.assertEqual(state.next_photo_number, 83)
                self.assertEqual(state.next_photo_id, 15078612)
        finally:
            server.shutdown()
            server.server_close()

    def test_reset_scan_clears_existing_found_records(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/325966/15946366/90/":
                    self.send_response(200)
                    self.send_header("content-type", "image/jpeg")
                    self.send_header("content-length", "12")
                    self.end_headers()
                    self.wfile.write(b"x")
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
                output_dir = Path(temp_dir)
                old_record = mpspd.FoundRecord(
                    url=f"http://127.0.0.1:{server.server_port}/325966/23670390/99/",
                    profile_id=325966,
                    photo_id=23670390,
                    photo_number=99,
                    content_type="image/jpeg",
                    content_length=1,
                    status=200,
                    discovered_at="old",
                )
                mpspd.append_found_record(output_dir / mpspd.FOUND_FILE, old_record)

                rc = mpspd.main(
                    [
                        "scan",
                        "--seed-url",
                        f"http://127.0.0.1:{server.server_port}/325966/15946366/90/",
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "1",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                        "--reset",
                    ]
                )

                self.assertEqual(rc, 0)
                records = mpspd.load_found_records(output_dir / mpspd.FOUND_FILE)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].photo_number, 90)
        finally:
            server.shutdown()
            server.server_close()

    def test_health_check_stops_before_scanning_when_last_found_fails(self):
        class Handler(BaseHTTPRequestHandler):
            requests_seen: list[str] = []

            def do_GET(self):
                self.__class__.requests_seen.append(self.path)
                self.send_response(403)
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir)
                state = mpspd.ScanState(
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    profile_id=325966,
                    next_photo_id=15946365,
                    next_photo_number=89,
                    increment=-1,
                    last_found_url=f"http://127.0.0.1:{server.server_port}/325966/15946366/90/",
                )
                mpspd.save_state(output_dir / mpspd.STATE_FILE, state)
                mpspd.append_found_record(
                    output_dir / mpspd.FOUND_FILE,
                    mpspd.FoundRecord(
                        url=state.last_found_url,
                        profile_id=325966,
                        photo_id=15946366,
                        photo_number=90,
                        content_type="image/jpeg",
                        content_length=12,
                        status=200,
                        discovered_at="old",
                    ),
                )

                rc = mpspd.main(
                    [
                        "scan",
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "1",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                        "--health-check-interval",
                        "1",
                    ]
                )

                self.assertEqual(rc, 1)
                repaired = mpspd.load_state(output_dir / mpspd.STATE_FILE)
                self.assertEqual(repaired.last_status, "health_check_failed:403")
                self.assertEqual(repaired.scanned, 0)
                self.assertEqual(Handler.requests_seen, ["/325966/15946366/90/"])
        finally:
            server.shutdown()
            server.server_close()

    def test_health_check_failure_rolls_back_to_last_verified_cursor(self):
        with TemporaryDirectory() as temp_dir:
            seed_url = "https://images.example.test/325966/100/10/"
            first_image_probe = True

            def probe_once(candidate, timeout_seconds):
                nonlocal first_image_probe
                if candidate.photo_id == 100 and candidate.photo_number == 10 and first_image_probe:
                    first_image_probe = False
                    return mpspd.ProbeResult(
                        candidate=candidate,
                        status=200,
                        found=True,
                        content_type="image/jpeg",
                        content_length=123,
                    )
                return mpspd.ProbeResult(candidate=candidate, status=403, found=False)

            with patch("mpspd.probe_once", side_effect=probe_once):
                rc = mpspd.main(
                    [
                        "scan",
                        "--seed-url",
                        seed_url,
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "4",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                        "--health-check-interval",
                        "2",
                    ]
                )

            self.assertEqual(rc, 1)
            repaired = mpspd.load_state(Path(temp_dir) / mpspd.STATE_FILE)
            records = mpspd.load_found_records(Path(temp_dir) / mpspd.FOUND_FILE)
            self.assertEqual(len(records), 1)
            self.assertEqual(repaired.last_status, "health_check_failed:403")
            self.assertEqual(repaired.next_photo_id, 99)
            self.assertEqual(repaired.next_photo_number, 9)

    def test_scan_uses_manual_anchors_to_repair_bad_negative_state(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir)
                state = mpspd.ScanState(
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    profile_id=325966,
                    next_photo_id=-16718957,
                    next_photo_number=86,
                    increment=-1,
                    scanned=32665303,
                    last_status="runtime_limit_reached",
                )
                mpspd.save_state(output_dir / mpspd.STATE_FILE, state)
                (output_dir / mpspd.MANUAL_FILE).write_text(
                    "\n".join(
                        [
                            f"http://127.0.0.1:{server.server_port}/325966/15281379/86/",
                            f"http://127.0.0.1:{server.server_port}/325966/15078623/85/",
                            f"http://127.0.0.1:{server.server_port}/325966/15078616/84/",
                            f"http://127.0.0.1:{server.server_port}/325966/14582353/83/",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )

                rc = mpspd.main(
                    [
                        "scan",
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "1",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                        "--health-check-interval",
                        "0",
                    ]
                )

                self.assertEqual(rc, 0)
                repaired = mpspd.load_state(output_dir / mpspd.STATE_FILE)
                self.assertEqual(repaired.last_found_url, f"http://127.0.0.1:{server.server_port}/325966/14582353/83/")
                self.assertEqual(repaired.next_photo_number, 82)
                self.assertEqual(repaired.next_photo_id, 14582351)
        finally:
            server.shutdown()
            server.server_close()

    def test_transient_probe_errors_are_retried_on_next_run(self):
        with TemporaryDirectory() as temp_dir:
            seed_url = "https://images.example.test/325966/12345/10/"

            def reset_probe(candidate, timeout_seconds):
                return mpspd.ProbeResult(
                    candidate=candidate,
                    status=None,
                    found=False,
                    error="conn_reset",
                )

            with patch("mpspd.probe_once", side_effect=reset_probe):
                rc = mpspd.main(
                    [
                        "scan",
                        "--seed-url",
                        seed_url,
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "1",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            retry_records = mpspd.load_retry_records(Path(temp_dir) / mpspd.RETRY_FILE)
            self.assertEqual(len(retry_records), 1)
            state_after_error = mpspd.load_state(Path(temp_dir) / mpspd.STATE_FILE)
            self.assertEqual(state_after_error.next_photo_id, 12344)
            self.assertEqual(state_after_error.next_photo_number, 10)

            def found_probe(candidate, timeout_seconds):
                return mpspd.ProbeResult(
                    candidate=candidate,
                    status=200,
                    found=True,
                    content_type="image/jpeg",
                    content_length=123,
                )

            with patch("mpspd.probe_once", side_effect=found_probe):
                rc = mpspd.main(
                    [
                        "scan",
                        "--output-dir",
                        temp_dir,
                        "--max-candidates",
                        "1",
                        "--concurrency",
                        "1",
                        "--max-runtime-seconds",
                        "5",
                        "--retries",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            records = mpspd.load_found_records(Path(temp_dir) / mpspd.FOUND_FILE)
            retry_records = mpspd.load_retry_records(Path(temp_dir) / mpspd.RETRY_FILE)
            state_after_retry = mpspd.load_state(Path(temp_dir) / mpspd.STATE_FILE)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].photo_id, 12345)
            self.assertEqual(retry_records, {})
            self.assertEqual(state_after_retry.next_photo_id, 12344)
            self.assertEqual(state_after_retry.next_photo_number, 9)


if __name__ == "__main__":
    unittest.main()
