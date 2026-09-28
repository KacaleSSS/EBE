"""Transport contracts exercised without DNS, sockets, TLS, or HTTP traffic."""
from __future__ import annotations

import gzip
import io
import os
import socket
import unittest
import zlib
from unittest import mock

from ebe import network

REAL_HTTP_RESPONSE = network.http.client.HTTPResponse


def address(ip="93.184.216.34", port=443):
    ipv6 = ":" in ip
    return (socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_STREAM,
            socket.IPPROTO_TCP, "", (ip, port, 0, 0) if ipv6 else (ip, port))


class NetworkTest(unittest.TestCase):
    def setUp(self):
        # A regression must fail locally, never accidentally access the network.
        self.dns = self.enterContext(mock.patch.object(network.socket, "getaddrinfo",
                                                       return_value=[address()]))
        self.raw = mock.Mock(name="raw_socket")
        self.socket = self.enterContext(mock.patch.object(network.socket, "socket", return_value=self.raw))
        self.enterContext(mock.patch.object(network.socket, "create_connection",
                                            side_effect=AssertionError("second DNS/connection forbidden")))
        self.tls = mock.Mock(name="tls_context")
        self.tls.wrap_socket.return_value = self.raw
        self.enterContext(mock.patch.object(network.ssl, "create_default_context", return_value=self.tls))
        self.response_factory = self.enterContext(mock.patch.object(network.http.client, "HTTPResponse"))
        self.enterContext(mock.patch.object(network.http.client.HTTPConnection, "connect",
                                            side_effect=AssertionError("must use the pinned raw socket")))
        self.enterContext(mock.patch.object(network.http.client.HTTPConnection, "getresponse",
                                            side_effect=AssertionError("use HTTPResponse(raw)")))

    def response(self, body=b"body", status=200, headers=None):
        response = mock.Mock(name="http_response")
        response.status = status
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        response.getheader.side_effect = lambda name, default=None: headers.get(name.lower(), default)
        stream = io.BytesIO(body)
        response.read1.side_effect = stream.read
        response.read.side_effect = AssertionError("unbounded response.read forbidden")
        self.response_factory.return_value = response
        return response

    def test_private_ipv4_ipv6_and_transition_addresses_rejected(self):
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                   "0.0.0.0", "100.64.0.1", "::1", "::", "fe80::1", "fd00::1",
                   "::ffff:127.0.0.1", "::ffff:8.8.8.8", "2002:0808:0808::1",
                   "2001:0000:4136:e378:8000:63bf:3fff:fdd2"):
            with self.subTest(ip=ip):
                self.dns.return_value = [address(ip)]
                with self.assertRaisesRegex(network.NetworkError, "non_public_destination"):
                    network.fetch("https://public.example/")
        self.socket.assert_not_called()

    def test_mixed_dns_answers_rejected_regardless_of_order(self):
        for addresses in ([address(), address("10.0.0.1")],
                          [address("::1"), address("2606:4700:4700::1111")],
                          [address(), address("fd00::1")]):
            with self.subTest(addresses=addresses):
                self.dns.return_value = addresses
                with self.assertRaisesRegex(network.NetworkError, "non_public_destination"):
                    network.resolve_public("https://example.org/")
        self.socket.assert_not_called()

    def test_userinfo_and_nondefault_ports_rejected_before_dns(self):
        for url in ("https://user:password@example.org/", "https://@example.org/",
                    "https://:password@example.org/", "http://example.org:443/",
                    "https://example.org:80/", "https://example.org:8443/",
                    "https://example.org:65536/", "https://example.org:bad/", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(network.NetworkError):
                network.resolve_public(url)
        self.dns.assert_not_called()

    def test_explicit_port_zero_is_not_a_default_port(self):
        with self.assertRaises(network.NetworkError):
            network.resolve_public("https://example.org:0/")

    def test_default_ports_and_public_ipv6(self):
        for url, port in (("http://example.org/", 80), ("http://example.org:80/", 80),
                          ("https://example.org/", 443), ("https://example.org:443/", 443),
                          ("https://[2606:4700:4700::1111]/", 443)):
            with self.subTest(url=url):
                self.dns.return_value = [address("2606:4700:4700::1111", port)]
                self.assertEqual(network.resolve_public(url)[2], port)
                self.assertEqual(self.dns.call_args.args[1], port)

    def test_ip_pinned_one_dns_no_proxy_and_httpresponse_raw(self):
        response = self.response(b"hello", headers={"Content-Type": "Text/Plain; charset=UTF-8"})
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:1",
                                         "HTTP_PROXY": "http://proxy.invalid:1",
                                         "ALL_PROXY": "http://proxy.invalid:1", "NO_PROXY": ""}):
            result = network.fetch("https://example.org/path?q=1")
        self.assertEqual(result, (b"hello", "text/plain", "https://example.org/path?q=1"))
        self.dns.assert_called_once_with("example.org", 443, type=socket.SOCK_STREAM)
        self.raw.connect.assert_called_once_with(("93.184.216.34", 443))
        self.tls.wrap_socket.assert_called_once_with(self.raw, server_hostname="example.org")
        self.response_factory.assert_called_once_with(self.raw)
        response.begin.assert_called_once()
        response.close.assert_called_once()
        self.raw.close.assert_called()
        sent = b"".join(call.args[0] for call in self.raw.sendall.call_args_list)
        self.assertIn(b"GET /path?q=1 HTTP/1.1", sent)
        self.assertIn(b"Host: example.org", sent)
        self.assertNotIn(b"CONNECT ", sent)
        self.assertNotIn(b"proxy.invalid", sent)

    def test_provider_redirect_refused_without_forwarding_credentials(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = self.response(status=status, headers={"Location": "https://other.example/"})
                self.dns.reset_mock()
                with self.assertRaisesRegex(network.NetworkError, "redirect_refused"):
                    network.request_json("https://provider.example/", key="TEST-SECRET")
                self.dns.assert_called_once()
                response.read1.assert_not_called()
                response.close.assert_called_once()

    def test_redirect_private_dns_is_revalidated(self):
        response = self.response(status=302, headers={"Location": "https://internal.example/"})
        self.dns.side_effect = [[address()], [address("127.0.0.1")]]
        with self.assertRaisesRegex(network.NetworkError, "non_public_destination"):
            network.fetch("https://public.example/")
        self.raw.connect.assert_called_once()
        response.close.assert_called_once()

    def test_gzip_and_deflate_are_bounded_after_decompression(self):
        for encoding, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            with self.subTest(encoding=encoding):
                response = self.response(compress(b"A" * 10000), headers={"Content-Encoding": encoding})
                with self.assertRaisesRegex(network.NetworkError, "response_too_large"):
                    network.fetch("https://example.org/", max_bytes=128)
                response.close.assert_called_once()
                self.assertTrue(all(0 < c.args[0] <= 129 for c in response.read1.call_args_list))
                body = b"A" * 128
                self.response(compress(body), headers={"Content-Encoding": encoding})
                self.assertEqual(network.fetch("https://example.org/", max_bytes=128)[0], body)

    def test_truncated_or_concatenated_gzip_rejected(self):
        for body in (gzip.compress(b"hello")[:-3], gzip.compress(b"hello") + gzip.compress(b"hidden")):
            self.response(body, headers={"Content-Encoding": "gzip"})
            with self.assertRaisesRegex(network.NetworkError, "invalid_compressed_body"):
                network.fetch("https://example.org/")

    def test_wire_limit_and_content_length_rejected(self):
        response = self.response(b"x" * 33)
        with self.assertRaisesRegex(network.NetworkError, "response_too_large"):
            network.fetch("https://example.org/", max_bytes=32)
        self.assertEqual(response.read1.call_args.args, (33,))
        for length in ("33", "not-a-number"):
            response = self.response(headers={"Content-Length": length})
            with self.assertRaisesRegex(network.NetworkError, "response_too_large"):
                network.fetch("https://example.org/", max_bytes=32)
            response.read1.assert_not_called()

    def test_read_deadline_error_redaction_and_cleanup(self):
        response = self.response()
        with mock.patch.object(network.time, "monotonic", side_effect=[0, 0, 26]):
            with self.assertRaisesRegex(network.NetworkError, "request_deadline"):
                network.fetch("https://example.org/", timeout=25)
        response.close.assert_called_once()
        response = self.response()
        response.read1.side_effect = OSError("SECRET https://user:pass@private/")
        with self.assertRaises(network.NetworkError) as caught:
            network.fetch("https://example.org/")
        self.assertEqual(str(caught.exception), "transport_failure_OSError")
        response.close.assert_called_once()

    def framed_response(self, body, headers):
        """Use the real HTTP parser over memory; retain the socket/DNS mocks."""
        head = "HTTP/1.1 200 OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        self.raw.makefile.return_value = io.BytesIO(head.encode("ascii") + b"\r\n" + body)
        self.response_factory.side_effect = REAL_HTTP_RESPONSE

    def test_content_length_premature_eof_rejected_by_real_parser(self):
        for body in (b"", b"abc"):
            with self.subTest(size=len(body)):
                self.framed_response(body, {"Content-Length": "100"})
                with self.assertRaisesRegex(network.NetworkError, "^incomplete_response_body$"):
                    network.fetch("https://example.org/")
                self.assertTrue(self.raw.makefile.return_value.closed)
                self.raw.close.assert_called()

    def test_content_length_counts_compressed_wire_bytes(self):
        plain = b"synthetic evidence " * 100
        for encoding, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            encoded = compress(plain)
            with self.subTest(encoding=encoding):
                self.framed_response(encoded, {"Content-Encoding": encoding, "Content-Length": str(len(encoded))})
                self.assertEqual(network.fetch("https://example.org/")[0], plain)
                # The compression stream is complete, but the HTTP body is short.
                self.framed_response(encoded, {"Content-Encoding": encoding, "Content-Length": str(len(encoded) + 1)})
                with self.assertRaisesRegex(network.NetworkError, "^incomplete_response_body$"):
                    network.fetch("https://example.org/")

    def test_complete_zero_close_delimited_and_chunked_bodies(self):
        cases = (
            (b"abc", {"Content-Length": "3"}, b"abc"),
            (b"", {"Content-Length": "0"}, b""),
            (b"abc", {}, b"abc"),
            (b"3\r\nabc\r\n0\r\n\r\n", {"Transfer-Encoding": "chunked"}, b"abc"),
            (b"3\r\nabc\r\n0\r\n\r\n", {"Transfer-Encoding": "chunked", "Content-Length": "100"}, b"abc"),
        )
        for body, headers, expected in cases:
            with self.subTest(headers=headers):
                self.framed_response(body, headers)
                self.assertEqual(network.fetch("https://example.org/")[0], expected)


if __name__ == "__main__":
    unittest.main()
