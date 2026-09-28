"""Local HTTP mocks only. No credentials, downloads or model server required."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from ebe import local_model


def envelope(result=None, **message_extra):
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(result if result is not None else {"content": "Draft [S1]", "metadata": {"source_ids": ["S1"]}}),
        **message_extra}}]}).encode()


@contextmanager
def server(body=None, status=200, headers=None, delay=0, ipv6=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            time.sleep(delay)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(envelope() if body is None else body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        daemon_threads = True
    httpd = Server(("::1" if ipv6 else "127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield f"http://{'[::1]' if ipv6 else '127.0.0.1'}:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class LocalModelTests(unittest.TestCase):
    def test_success_no_dns_proxy_tools_or_readiness_mutation(self):
        request = {"kind": "chapter", "readiness": {"pass": False}, "context": "Untrusted source"}
        with server() as (url, requests), patch.dict("os.environ", {"HTTP_PROXY": "http://192.0.2.1:1", "ALL_PROXY": "http://192.0.2.1:1"}), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS used")):
            result = local_model.generate(request, url + "/v1", "local-model")
        self.assertEqual(result["metadata"]["source_ids"], ["S1"])
        path, headers, body = requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("tools", body)
        self.assertIn("untrusted", body["messages"][0]["content"])
        self.assertEqual(json.loads(body["messages"][1]["content"]), request)
        self.assertFalse(request["readiness"]["pass"])

    def test_ipv6_literal(self):
        try:
            with server(ipv6=True) as (url, _):
                self.assertIn("content", local_model.generate({}, url + "/v1/chat/completions", "local"))
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")

    def test_invalid_configuration_never_connects(self):
        bad = ["http://localhost:8000", "https://127.0.0.1:8000", "http://127.1:80", "http://127.0.0.2:80",
               "http://[::ffff:127.0.0.1]:80", "http://user@127.0.0.1:80", "http://127.0.0.1:80/?x=1",
               "http://127.0.0.1:80/#x", "http://127.0.0.1:80/other", "http://127.0.0.1:0",
               "http://127.0.0.1:65536", "http://127.0.0.1:80\n", "http://example.invalid:80"]
        with patch.object(local_model._LiteralConnection, "connect", side_effect=AssertionError("connected")):
            for url in bad:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    local_model.generate({}, url, "local")
            for timeout in (0, -1, 301, float("nan"), float("inf"), True):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    local_model.generate({}, "http://127.0.0.1:80", "local", timeout=timeout)
            with self.assertRaisesRegex(ValueError, "budget"):
                local_model.generate({"source": "中" * 128000}, "http://127.0.0.1:80", "local")

    def test_redirect_not_followed(self):
        with server(status=302, headers={"Location": "http://192.0.2.1/"}) as (url, requests):
            with self.assertRaisesRegex(ValueError, "redirects"):
                local_model.generate({}, url, "local")
        self.assertEqual(len(requests), 1)

    def test_size_limits_with_and_without_length(self):
        for headers in ({"Content-Length": "1000001"}, {}):
            with self.subTest(headers=headers), server(b"x" * 1000001, headers=headers) as (url, _):
                with self.assertRaisesRegex(ValueError, "1MB"):
                    local_model.generate({}, url, "local")

    def test_bad_response_contract_and_tools(self):
        bodies = [b"not json", b"{}", envelope({"content": "", "metadata": {}}),
                  envelope({"content": "x", "metadata": []}), envelope({"content": "x"}),
                  envelope(tool_calls=[{"function": {"name": "exec"}}]), envelope(function_call={"name": "exec"}),
                  envelope().replace(b'"stop"', b'"length"'),
                  envelope({"content": "x", "metadata": {"score": float("nan")}})]
        for body in bodies:
            with self.subTest(body=body), server(body) as (url, _):
                with self.assertRaises(ValueError):
                    local_model.generate({}, url, "local")

    def test_timeout(self):
        with server(delay=.2) as (url, _):
            with self.assertRaises(TimeoutError):
                local_model.generate({}, url, "local", timeout=.05)


if __name__ == "__main__":
    unittest.main()
