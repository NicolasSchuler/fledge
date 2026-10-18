"""The HTTP/1.1 wire format and the pinned connection, without any outside network.

``read_response`` is exercised by feeding bytes to an ``asyncio.StreamReader``; the
connection is exercised against a server on the loopback interface.
"""

from __future__ import annotations

import asyncio
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.online_http_session import (
    ConnectFailure,
    ExchangeRequest,
    default_ssl_context,
    exchange,
    open_pinned,
)
from latexprep.online_http_wire import build_request, read_response
from latexprep.online_options import NetworkFailure
from latexprep.public_url import Target, parse_public_url


def reader_of(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def read(payload: bytes, *, head_only: bool = False, limit: int = 1024):
    return await read_response(reader_of(payload), head_only=head_only, limit=limit)


class ReadResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_three_body_framings_read_the_same_body(self):
        framed = {
            "content-length": b"Content-Length: 5\r\n\r\nhello",
            "chunked": b"Transfer-Encoding: chunked\r\n\r\n2\r\nhe\r\n3\r\nllo\r\n0\r\n\r\n",
            "until close": b"\r\nhello",
        }
        for name, framing in framed.items():
            with self.subTest(framing=name):
                response = await read(b"HTTP/1.1 200 OK\r\n" + framing)
                self.assertEqual((response.status, response.body), (200, b"hello"))

    async def test_headers_are_lowercased_and_found_by_any_case(self):
        response = await read(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        self.assertEqual(response.headers, (("content-type", "application/json"),))
        self.assertEqual(response.header("Content-TYPE"), "application/json")

    async def test_a_body_is_read_only_for_a_wanted_2xx_answer(self):
        body = b"Content-Length: 2\r\n\r\n{}"
        head = await read(b"HTTP/1.1 200 OK\r\n" + body, head_only=True)
        self.assertEqual(head.body, b"")
        missing = await read(b"HTTP/1.1 404 Not Found\r\n" + body)
        self.assertEqual((missing.status, missing.body), (404, b""))

    async def test_interim_responses_are_skipped_and_never_taken_as_the_final_answer(self):
        response = await read(
            b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        )
        self.assertEqual((response.status, response.body), (200, b"{}"))
        for payload in (
            b"HTTP/1.1 100 Continue\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n",
            (b"HTTP/1.1 103 Early Hints\r\n\r\n" * 5) + b"HTTP/1.1 200 OK\r\n\r\n",
        ):
            with self.subTest(payload=payload[:24]), self.assertRaises(NetworkFailure) as raised:
                await read(payload, head_only=True)
            self.assertEqual(raised.exception.outcome, "malformed")

    async def test_oversize_and_ambiguous_bodies_are_refused(self):
        for headers, outcome in (
            (b"Content-Length: 1025\r\n\r\n", "response_limit"),
            (b"Content-Length: 1\r\nContent-Length: 2\r\n\r\n", "malformed"),
            (b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n", "malformed"),
            (b"Transfer-Encoding: gzip\r\n\r\n", "malformed"),
            (b"Content-Encoding: gzip\r\nContent-Length: 1\r\n\r\nx", "malformed"),
            (b"Transfer-Encoding: chunked\r\n\r\n401\r\n", "response_limit"),
            (b"Transfer-Encoding: chunked\r\n\r\nzz\r\n", "malformed"),
            (b"Transfer-Encoding: chunked\r\n\r\n1\r\nxx0\r\n", "malformed"),
        ):
            with self.subTest(headers=headers), self.assertRaises(NetworkFailure) as raised:
                await read(b"HTTP/1.1 200 OK\r\n" + headers)
            self.assertEqual(raised.exception.outcome, outcome)

    async def test_a_body_without_length_is_bounded_while_it_is_read(self):
        with self.assertRaises(NetworkFailure) as raised:
            await read(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 2048)
        self.assertEqual(raised.exception.outcome, "response_limit")

    async def test_malformed_heads_are_refused(self):
        for payload in (
            b"",
            b"garbage\r\n\r\n",
            b"HTTP/1.1 200\n\r\n",
            b"HTTP/2 200 OK\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nBad Header: x\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nno colon\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nX: y\r\n",
            b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 101 + b"\r\n",
        ):
            with self.subTest(payload=payload[:30]), self.assertRaises(NetworkFailure) as raised:
                await read(payload, head_only=True)
            self.assertEqual(raised.exception.outcome, "malformed")

    async def test_a_connection_that_ends_inside_the_body_is_incomplete(self):
        with self.assertRaises(asyncio.IncompleteReadError):
            await read(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort")

    async def test_repeated_location_headers_are_ambiguous(self):
        response = await read(
            b"HTTP/1.1 302 Found\r\nLocation: /a\r\nLocation: /b\r\n\r\n", head_only=True
        )
        with self.assertRaises(NetworkFailure):
            response.header("location")


class BuildRequestTests(unittest.TestCase):
    def test_the_request_names_the_host_and_carries_no_ambient_credentials(self):
        target = parse_public_url("https://example.org/data?id=1")
        request = build_request("GET", target, "agent/1").decode("ascii")
        self.assertTrue(request.startswith("GET /data?id=1 HTTP/1.1\r\nHost: example.org\r\n"))
        self.assertIn("User-Agent: agent/1\r\n", request)
        self.assertIn("Accept-Encoding: identity\r\n", request)
        self.assertTrue(request.endswith("Connection: close\r\n\r\n"))
        for header in ("Authorization", "Cookie", "Proxy"):
            self.assertNotIn(header, request)

    def test_an_ip_literal_host_is_bracketed(self):
        target = parse_public_url("https://[2606:2800:220:1:248:1893:25c8:1946]/")
        self.assertIn(
            "Host: [2606:2800:220:1:248:1893:25c8:1946]\r\n",
            build_request("HEAD", target, "a").decode(),
        )


def loopback_target(port: int, *, scheme: str = "http") -> Target:
    """A target for a server on this machine; the public-URL policy would refuse it."""
    return Target(f"{scheme}://example.org/data", scheme, "example.org", port, "/data")


class LoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def test_a_request_and_its_response_cross_a_real_connection(self):
        received = []

        async def handler(reader, writer):
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            writer.write(b'4\r\n{"a"\r\n3\r\n:1}\r\n0\r\n\r\n')
            await writer.drain()
            writer.close()

        port = await self.serve(handler)
        target = loopback_target(port)
        reader, writer = await open_pinned(target, "127.0.0.1", ssl_context=None)
        writer.write(build_request("GET", target, "agent/1"))
        response = await read_response(reader, head_only=False, limit=1024)
        writer.close()
        self.assertEqual((response.status, response.body), (200, b'{"a":1}'))
        self.assertTrue(received[0].startswith(b"GET /data HTTP/1.1\r\nHost: example.org\r\n"))

    async def test_a_server_that_never_answers_is_stopped_by_the_deadline(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(10)

        port = await self.serve(handler)
        target = loopback_target(port)
        reader, writer = await open_pinned(target, "127.0.0.1", ssl_context=None)
        writer.write(build_request("GET", target, "agent/1"))
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.2):
                await read_response(reader, head_only=False, limit=1024)
        writer.close()

    async def test_a_server_that_hangs_up_mid_body_gives_an_incomplete_read(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")
            await writer.drain()
            writer.close()

        port = await self.serve(handler)
        target = loopback_target(port)
        reader, writer = await open_pinned(target, "127.0.0.1", ssl_context=None)
        writer.write(build_request("GET", target, "agent/1"))
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_response(reader, head_only=False, limit=1024)
        writer.close()

    async def test_a_refused_connection_is_a_connect_failure_with_its_cause(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(ConnectFailure) as raised:
            await open_pinned(loopback_target(port), "127.0.0.1", ssl_context=None)
        self.assertIsInstance(raised.exception.__cause__, OSError)

    async def test_the_real_transport_refuses_a_loopback_address(self):
        request = ExchangeRequest(loopback_target(80), "127.0.0.1", "GET", False, 1024)
        with self.assertRaises(NetworkFailure) as raised:
            await exchange(request)
        self.assertEqual(raised.exception.outcome, "unsafe_target")


class TlsConnectionTests(unittest.IsolatedAsyncioTestCase):
    def test_the_verifying_context_is_built_once_and_checks_host_names(self):
        context = default_ssl_context()
        self.assertIs(context, default_ssl_context())
        self.assertTrue(context.check_hostname)

    async def test_the_numeric_address_is_dialled_but_the_original_host_name_is_verified(self):
        reader, writer = asyncio.StreamReader(), SimpleNamespace()
        sock = SimpleNamespace(setblocking=lambda _: None, close=lambda: None)
        loop = asyncio.get_running_loop()
        target = parse_public_url("https://example.org/data")
        with (
            patch("socket.socket", return_value=sock),
            patch.object(loop, "sock_connect", AsyncMock()) as connect,
            patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))) as opening,
            patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve again")),
        ):
            await open_pinned(
                target, "93.184.216.34", ssl_context=default_ssl_context(), connect_timeout=None
            )
        assert connect.await_args is not None and opening.await_args is not None
        self.assertEqual(connect.await_args.args[1], ("93.184.216.34", 443))
        self.assertEqual(opening.await_args.kwargs["server_hostname"], "example.org")
        self.assertIs(opening.await_args.kwargs["ssl"], default_ssl_context())


if __name__ == "__main__":
    unittest.main()
