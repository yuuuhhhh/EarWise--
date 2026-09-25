"""Repeated launches reuse a verified loopback server without opening a session."""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tornado.testing import AsyncHTTPTestCase

from app import instance, main


@contextlib.contextmanager
def local_server(response=None, raw_body=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            payload = response() if callable(response) else response
            body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="earwise-instance-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_verified_server_uses_the_recorded_custom_port(self):
        with local_server(lambda: metadata) as (port, requests):
            metadata = instance.make_instance(port, True)
            instance.write_instance(self.root, metadata)
            self.assertEqual(instance.verify_instance(self.root), metadata)
            self.assertEqual(requests, ["/api/instance"])

    def test_lost_server_is_not_treated_as_a_running_instance(self):
        with local_server({}) as (port, _):
            metadata = instance.make_instance(port, True)
            instance.write_instance(self.root, metadata)
        with self.assertRaises(RuntimeError):
            instance.verify_instance(self.root, timeout=0.1)

    def test_missing_or_legacy_metadata_does_not_guess_an_instance(self):
        metadata_path = self.root / "work" / "server-instance.json"
        metadata_path.parent.mkdir()
        with self.assertRaises(RuntimeError):
            instance.verify_instance(self.root)
        for body in ('{}', '{"port": 8765}', 'null', '{'):
            with self.subTest(body=body):
                metadata_path.write_text(body, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    instance.verify_instance(self.root)

    def test_invalid_server_json_is_rejected(self):
        for body in (b'{', b'[]', b'null'):
            with self.subTest(body=body), local_server(raw_body=body) as (port, _):
                instance.write_instance(self.root, instance.make_instance(port, True))
                with self.assertRaises(RuntimeError):
                    instance.verify_instance(self.root)

    def test_a_different_live_instance_or_mode_cannot_be_reused(self):
        with local_server(lambda: live) as (port, requests):
            metadata = instance.make_instance(port, True)
            instance.write_instance(self.root, metadata)
            for key, value in (("instance_id", "another-instance"), ("app", "another-app"),
                               ("mode", instance.make_instance(port, False)["mode"]),
                               ("url", "http://127.0.0.1:1")):
                with self.subTest(key=key):
                    live = dict(metadata, **{key: value})
                    with self.assertRaises(RuntimeError):
                        instance.verify_instance(self.root)
            self.assertEqual(requests, ["/api/instance"] * 4)

    def test_unsafe_metadata_urls_are_rejected_before_any_network_access(self):
        urls = ("http://outside.invalid:8765", "https://127.0.0.1:8765",
                "http://user:password@127.0.0.1:8765", "http://127.0.0.1:8765/other",
                "http://127.0.0.1:8765?other=1", "http://127.0.0.1:8765#other",
                "http://127.0.0.1:0", "http://127.0.0.1:65536")
        with patch.object(instance, "build_opener") as opener:
            for url in urls:
                with self.subTest(url=url):
                    metadata = instance.make_instance(8765, True)
                    metadata["url"] = url
                    instance.write_instance(self.root, metadata)
                    with self.assertRaises(RuntimeError):
                        instance.verify_instance(self.root)
            opener.assert_not_called()

    def test_probe_timeout_is_reported_without_opening_a_browser(self):
        instance.write_instance(self.root, instance.make_instance(8765, True))
        with patch.object(instance, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("test timeout")
            with self.assertRaises(RuntimeError):
                instance.verify_instance(self.root, timeout=0.125)
            opener.return_value.open.assert_called_once_with(
                "http://127.0.0.1:8765/api/instance", timeout=0.125)

    def test_repeat_launch_opens_the_verified_custom_port_not_requested_port(self):
        with local_server(lambda: metadata) as (port, _):
            metadata = instance.make_instance(port, True)
            instance.write_instance(self.root, metadata)
            args = argparse.Namespace(simulate=True, no_browser=False, port=8765)
            with patch.object(instance.webbrowser, "open", return_value=True) as browser:
                with contextlib.redirect_stdout(io.StringIO()):
                    instance.reopen_instance(self.root, args)
                browser.assert_called_once_with(metadata["url"])

    def test_no_browser_reuses_server_without_opening_a_page(self):
        with local_server(lambda: metadata) as (port, _):
            metadata = instance.make_instance(port, True)
            instance.write_instance(self.root, metadata)
            args = argparse.Namespace(simulate=True, no_browser=True, port=8765)
            with patch.object(instance.webbrowser, "open") as browser:
                with contextlib.redirect_stdout(io.StringIO()):
                    instance.reopen_instance(self.root, args)
                browser.assert_not_called()

    def test_mode_mismatch_never_opens_the_wrong_mode(self):
        with local_server(lambda: metadata) as (port, _):
            for existing_simulation in (True, False):
                with self.subTest(existing_simulation=existing_simulation):
                    metadata = instance.make_instance(port, existing_simulation)
                    instance.write_instance(self.root, metadata)
                    args = argparse.Namespace(simulate=not existing_simulation, no_browser=False, port=8765)
                    with patch.object(instance.webbrowser, "open") as browser:
                        with self.assertRaises(RuntimeError) as raised:
                            instance.reopen_instance(self.root, args)
                        self.assertIn(metadata["url"], str(raised.exception))
                        browser.assert_not_called()

    def test_browser_failure_prints_a_manual_url(self):
        url = "http://127.0.0.1:9876"
        with patch.object(instance.webbrowser, "open", return_value=False):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                instance.open_browser(url)
            self.assertIn(url, output.getvalue())

    def test_clear_only_removes_metadata_owned_by_that_instance(self):
        metadata = instance.make_instance(8765, True)
        instance.write_instance(self.root, metadata)
        metadata_path = self.root / "work" / "server-instance.json"
        instance.clear_instance(self.root, "another-instance")
        self.assertEqual(json.loads(metadata_path.read_text(encoding="utf-8")), metadata)
        instance.clear_instance(self.root, metadata["instance_id"])
        self.assertFalse(metadata_path.exists())

    def test_valid_ports_and_out_of_range_ports(self):
        self.assertEqual(main.parse_port("1"), 1)
        self.assertEqual(main.parse_port("65535"), 65535)
        for value in ("0", "65536", "-1", "not-a-port", "8765.5"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                main.parse_port(value)

    def test_lock_conflict_reopens_without_initializing_or_recovering_a_controller(self):
        with patch.object(main, "process_lock", side_effect=instance.AlreadyRunning), \
                patch.object(main, "ROOT", self.root), \
                patch.object(main, "reopen_instance") as reopen, \
                patch.object(main.asyncio, "run") as run, \
                patch.object(main, "Controller") as controller, \
                patch("sys.argv", ["app.main", "--simulate"]):
            main.main()
            reopen.assert_called_once()
            self.assertEqual(reopen.call_args.args[0], self.root)
            self.assertTrue(reopen.call_args.args[1].simulate)
            run.assert_not_called()
            controller.assert_not_called()

    def test_other_startup_errors_do_not_attempt_to_reuse_an_instance(self):
        with patch.object(main, "process_lock", side_effect=OSError("permission denied")), \
                patch.object(main, "reopen_instance") as reopen, \
                patch("sys.argv", ["app.main", "--simulate"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main.main()
            self.assertEqual(raised.exception.code, 1)
            reopen.assert_not_called()


class InstanceEndpointTests(AsyncHTTPTestCase):
    def get_app(self):
        self.metadata = instance.make_instance(8765, True)
        # The identity endpoint must be independent of controller/session methods.
        return main.make_app(object(), self.metadata)

    def test_instance_endpoint_returns_its_identity_without_controller_access(self):
        response = self.fetch("/api/instance")
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body), self.metadata)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_instance_endpoint_keeps_existing_local_origin_and_host_restrictions(self):
        for headers in ({"Origin": "https://outside.invalid"}, {"Host": "outside.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.fetch("/api/instance", headers=headers).code, 403)


if __name__ == "__main__":
    unittest.main()
