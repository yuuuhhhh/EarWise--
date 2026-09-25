"""Loopback transport tests on an ephemeral port with an isolated media root."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tornado.httpclient import HTTPClientError, HTTPRequest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect

from app import main
from app.controller import ExperimentError


class FakeController:
    def __init__(self):
        self.clients = set()
        self.catalog = [{"video_id": "attention_01", "path": "attention_video/01.mp4"}]
        self.actions = []

    def state(self):
        return {"mode": "simulation", "session": None, "quality": {"data_status": "waiting"}}

    def clock(self):
        return 123456789

    def utc(self):
        return "2026-09-25T12:00:00+00:00"

    async def action(self, name, body):
        self.actions.append((name, body))
        if name not in ("heartbeat", "preflight"):
            raise ExperimentError("无效测试操作")
        return {"accepted": name}


class HttpTests(AsyncHTTPTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mema-http-test-")
        self.root = Path(self.temporary.name)
        (self.root / "web").mkdir()
        (self.root / "web" / "index.html").write_text("<html>中文模拟测试</html>", encoding="utf-8")
        (self.root / "attention_video").mkdir()
        self.media_bytes = bytes(range(64))
        (self.root / "attention_video" / "01.mp4").write_bytes(self.media_bytes)
        (self.root / "attention_video" / "99.mp4").write_bytes(b"unlisted-media")
        (self.root / "private.txt").write_bytes(b"never-serve-this")
        self.controller = FakeController()
        self.root_patch = patch.object(main, "ROOT", self.root)
        self.root_patch.start()
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self.root_patch.stop()
            self.temporary.cleanup()

    def get_app(self):
        return main.make_app(self.controller)

    def test_local_page_state_and_clock_routes(self):
        page = self.fetch("/")
        self.assertEqual(page.code, 200)
        self.assertIn("中文模拟测试", page.body.decode())
        state = self.fetch("/api/state")
        self.assertEqual(state.code, 200)
        self.assertEqual(json.loads(state.body), self.controller.state())
        self.assertEqual(state.headers["Cache-Control"], "no-store")
        clock = self.fetch("/api/clock")
        self.assertEqual(json.loads(clock.body)["server_monotonic_ns"], 123456789)

    def test_api_accepts_object_and_rejects_malformed_or_nonobject_json(self):
        response = self.fetch("/api/preflight", method="POST", body=json.dumps({"client_id": "owner-page"}))
        self.assertEqual(response.code, 200)
        self.assertTrue(json.loads(response.body)["ok"])
        for body in ("{", "[]", "null", '"a string"'):
            with self.subTest(body=body):
                response = self.fetch("/api/preflight", method="POST", body=body)
                self.assertEqual(response.code, 400)
                result = json.loads(response.body)
                self.assertFalse(result["ok"])
                self.assertIn("error", result)

    def test_action_validation_failure_is_visible_not_http_success(self):
        response = self.fetch("/api/not_a_real_action", method="POST", body="{}")
        self.assertEqual(response.code, 400)
        self.assertEqual(json.loads(response.body)["error"], "无效测试操作")

    def test_remote_origin_is_rejected_for_api_and_media(self):
        for route in ("/api/state", "/media/attention_video/01.mp4", "/"):
            response = self.fetch(route, headers={"Origin": "https://outside.invalid"})
            self.assertEqual(response.code, 403)

    def test_rebound_host_is_rejected(self):
        response = self.fetch("/api/state", headers={"Host": "outside.invalid"})
        self.assertEqual(response.code, 403)

    def test_media_range_returns_exact_requested_bytes(self):
        response = self.fetch("/media/attention_video/01.mp4", headers={"Range": "bytes=10-19"})
        self.assertEqual(response.code, 206)
        self.assertEqual(response.body, self.media_bytes[10:20])
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/64")
        self.assertIn("video/mp4", response.headers["Content-Type"])

    def test_edited_local_page_invalidates_previous_etag(self):
        first = self.fetch("/")
        previous_etag = first.headers["Etag"]
        unchanged = self.fetch("/", headers={"If-None-Match": previous_etag})
        self.assertEqual(unchanged.code, 304)
        updated_html = "<html>中文模拟测试：页面文件已更新，必须重新载入内容</html>"
        (self.root / "web" / "index.html").write_text(updated_html, encoding="utf-8")
        updated = self.fetch("/", headers={"If-None-Match": previous_etag})
        self.assertEqual(updated.code, 200)
        self.assertNotEqual(updated.headers["Etag"], previous_etag)
        self.assertEqual(updated.body.decode("utf-8"), updated_html)

    def test_unlisted_media_and_arbitrary_project_files_are_not_served(self):
        for route in ("/media/attention_video/99.mp4", "/media/private.txt", "/private.txt",
                      "/media/%2e%2e/private.txt", "/media/attention_video/%2e%2e/%2e%2e/private.txt"):
            with self.subTest(route=route):
                response = self.fetch(route)
                self.assertIn(response.code, (403, 404))
                self.assertNotEqual(response.body, b"never-serve-this")

    @gen_test
    async def test_websocket_initial_state_is_read_only_snapshot(self):
        connection = await websocket_connect(self.get_url("/ws?client_id=observer-page").replace("http:", "ws:"))
        try:
            initial = json.loads(await connection.read_message())
            self.assertEqual(initial, {"type": "state", "state": self.controller.state()})
            self.assertEqual(self.controller.actions, [])
        finally:
            connection.close()

    @gen_test
    async def test_websocket_remote_origin_is_rejected(self):
        request = HTTPRequest(self.get_url("/ws?client_id=observer-page").replace("http:", "ws:"),
                              headers={"Origin": "https://outside.invalid"})
        with self.assertRaises(HTTPClientError) as raised:
            await websocket_connect(request)
        self.assertEqual(raised.exception.code, 403)

    def test_project_process_lock_prevents_second_server_until_release(self):
        with main.process_lock(self.root):
            with self.assertRaises(RuntimeError):
                with main.process_lock(self.root):
                    self.fail("A second project server must not acquire the same lock")
        with main.process_lock(self.root):
            pass


if __name__ == "__main__":
    unittest.main()
