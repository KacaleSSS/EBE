"""Bounded public HTTP transport; DNS is checked once and the connection pinned.

No ambient proxy, cookie jar, authentication redirect, or private-network fallback.
Application policy supplements, but does not replace, OS network isolation.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
import zlib
from urllib.parse import urlsplit, urlunsplit, urljoin


class NetworkError(ValueError):
    pass


def resolve_public(url: str):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise NetworkError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or port != (443 if parts.scheme == "https" else 80)):
            raise NetworkError("invalid_public_endpoint")
        host = parts.hostname.encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise NetworkError("dns_empty")
        for family, kind, protocol, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
                raise NetworkError("non_public_destination")
        return parts, host, port, addresses[0]
    except (ValueError, UnicodeError, OSError) as exc:
        if isinstance(exc, NetworkError):
            raise
        raise NetworkError("invalid_or_unresolved_endpoint") from None


def _request(url, *, payload=None, headers=None, max_bytes=15*1024*1024,
             timeout=25, redirects=4):
    if not 1 <= max_bytes <= 50*1024*1024 or not 0 < timeout <= 300:
        raise NetworkError("invalid_transport_limits")
    deadline = time.monotonic() + timeout
    for hop in range(redirects + 1):
        parts, host, port, address = resolve_public(url)
        family, kind, protocol, _, sockaddr = address
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkError("request_deadline")
        connection = http.client.HTTPConnection(host, port, timeout=remaining)
        raw = None
        response = None
        try:
            raw = socket.socket(family, kind, protocol)
            raw.settimeout(remaining)
            raw.connect(sockaddr)  # numeric sockaddr only; no second DNS lookup
            if parts.scheme == "https":
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            connection.sock = raw
            request_headers = {"User-Agent": "EBE/1.0 research", "Accept-Encoding": "identity",
                               "Connection": "close", **(headers or {})}
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("POST" if payload is not None else "GET", path, body=payload, headers=request_headers)
            # Keep socket ownership until the bounded body read finishes. The
            # normal getresponse() closes it early for Connection: close, making
            # per-read deadline updates fail on Windows (WSAENOTSOCK).
            response = http.client.HTTPResponse(raw)
            response.begin()
            if response.status in {301, 302, 303, 307, 308}:
                if payload is not None or headers or hop == redirects:
                    raise NetworkError("redirect_refused")
                url = urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200:
                raise NetworkError(f"http_{response.status}")
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding not in {"", "identity", "gzip", "deflate"}:
                raise NetworkError("unsupported_content_encoding")
            decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
            length = response.getheader("Content-Length")
            if length is not None and (not length.isascii() or not length.isdigit() or int(length) > max_bytes):
                raise NetworkError("response_too_large")
            # Chunked framing takes precedence over Content-Length in HTTPResponse.
            expected_bytes = int(length) if length is not None and response.chunked is not True else None
            chunks, size, wire_size = [], 0, 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkError("request_deadline")
                raw.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - wire_size))
                if not chunk:
                    break
                wire_size += len(chunk)
                if wire_size > max_bytes:
                    raise NetworkError("response_too_large")
                if decoder:
                    chunk = decoder.decompress(chunk, max_bytes + 1 - size)
                size += len(chunk)
                if size > max_bytes:
                    raise NetworkError("response_too_large")
                chunks.append(chunk)
            # read1() can return EOF without raising IncompleteRead. Compare the
            # encoded wire bytes, not the decompressed body, before accepting it.
            if expected_bytes is not None and wire_size != expected_bytes:
                raise NetworkError("incomplete_response_body")
            if decoder and (not decoder.eof or decoder.unused_data):
                raise NetworkError("invalid_compressed_body")
            mime = response.getheader("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
            return b"".join(chunks), mime, url
        except NetworkError:
            raise
        except (OSError, http.client.HTTPException, ValueError, zlib.error) as exc:
            raise NetworkError("transport_failure_" + type(exc).__name__) from None
        finally:
            if response is not None:
                response.close()
            connection.close()
            if raw:
                raw.close()
    raise NetworkError("redirect_limit")


def fetch(url, max_bytes=15*1024*1024, timeout=25):
    return _request(url, max_bytes=max_bytes, timeout=timeout)


def request_json(url, payload=None, key=None, max_bytes=5_000_000, timeout=25, headers=None):
    if urlsplit(url).scheme != "https":
        raise NetworkError("provider_requires_https")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if key:
        if not isinstance(key, str) or any(ord(c) < 33 for c in key):
            raise NetworkError("invalid_key")
        request_headers["Authorization"] = "Bearer " + key
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    data, _, _ = _request(url, payload=body, headers=request_headers,
                           max_bytes=max_bytes, timeout=timeout, redirects=0)
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise NetworkError("invalid_json_response") from None
    if not isinstance(result, dict):
        raise NetworkError("response_not_object")
    return result
