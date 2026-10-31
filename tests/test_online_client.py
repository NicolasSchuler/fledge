"""The online client: address policy, redirects, budget, spacing, caching and diagnostics.

The client takes a resolver and a transport, so every case here runs without a network.
"""

from __future__ import annotations

import asyncio
import errno
import ssl
import unittest
from unittest.mock import AsyncMock, patch

from latexprep import __version__
from latexprep.online_http_session import ConnectFailure, OnlineClient
from latexprep.online_http_wire import Response
from latexprep.online_options import NetworkFailure
from latexprep.resource_budget import ResourceBudget

from .online_support import (
    PUBLIC_ADDRESS,
    PUBLIC_IPV6_ADDRESS,
    FakeTransport,
    options,
    public_resolver,
)


def client_for(answers, selected=None, budget=None, resolver=public_resolver):
    """Build an online client whose transport is a fake that answers with ``answers``.

    Args:
        answers: The fake transport's answers, as :class:`FakeTransport` takes them.
        selected: The online options, or ``None`` for those of :func:`options`.
        budget: The resource budget passed to the client, or ``None``.
        resolver: The host-name resolver; by default every host is one public address.

    Returns:
        The client and its fake transport.
    """
    transport = FakeTransport(answers)
    return OnlineClient(selected or options(), budget, resolver=resolver, transport=transport), (
        transport
    )


class AddressPolicyTests(unittest.IsolatedAsyncioTestCase):
    """The address policy: unsafe literals and answers, revalidated redirects, the user agent."""

    async def test_unsafe_literals_and_query_credentials_do_not_resolve(self):
        resolver = AsyncMock()
        client, transport = client_for(Response(200), resolver=resolver)
        for url in (
            "file:///etc/passwd",
            "https://user:secret@example.org/",
            "http://127.0.0.1/",
            "http://169.254.169.254/",
            "http://[::1]/",
            "https://example.org:22/",
            "https://example.org/?access_token=SECRET",
            "https://example.org/?auth=SECRET",
            "https://example.org/?id=1;api_key=SECRET",
            "http://metadata.internal/",
            "https://example.org/\r\nCookie:x",
        ):
            with self.subTest(url=url):
                result = await client.fetch(url)
                self.assertEqual(result.outcome, "unsafe_target")
                self.assertNotIn("SECRET", result.url)
                self.assertNotIn("user:secret", result.url)
        resolver.assert_not_called()
        self.assertEqual(transport.requests, [])

    async def test_one_private_address_among_the_answers_makes_the_host_unsafe(self):
        resolver = AsyncMock(return_value=(PUBLIC_ADDRESS, "10.0.0.1"))
        client, transport = client_for(Response(200), resolver=resolver)
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "unsafe_target")
        self.assertEqual(transport.requests, [])

    async def test_redirect_destinations_are_revalidated_and_rebinding_is_pinned(self):
        resolver = AsyncMock(side_effect=[(PUBLIC_ADDRESS,), ("127.0.0.1",)])
        redirect = Response(302, (("location", "https://internal.example.org/"),))
        client, transport = client_for(redirect, resolver=resolver)
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "unsafe_target")
        self.assertEqual(
            [(item.target.host, item.address) for item in transport.requests],
            [("example.org", PUBLIC_ADDRESS)],
        )
        self.assertEqual(resolver.await_args_list[1].args, ("internal.example.org", 443))

    async def test_https_to_http_redirect_is_refused_for_metadata_but_not_for_link_checks(self):
        downgrade = Response(302, (("location", "http://example.org/data"),))
        for want_body, outcome in ((True, "unsafe_target"), (False, "available")):
            with self.subTest(want_body=want_body):
                client, transport = client_for([downgrade, Response(200)])
                result = await client.fetch(
                    "https://api.crossref.org/works/10.1234%2Fexample", want_body=want_body
                )
                self.assertEqual(result.outcome, outcome)
                if want_body:
                    self.assertEqual(len(transport.requests), 1)
                    self.assertEqual(
                        result.message,
                        "A metadata request was redirected from HTTPS to plain HTTP",
                    )

    def test_user_agent_carries_the_contact_address_only_for_crossref(self):
        selected = options(online_contact_email="paper@example.org")
        plain = f"fledge/{__version__} (explicit reference checks)"
        self.assertEqual(
            selected.user_agent_for("api.crossref.org"),
            f"fledge/{__version__} (mailto:paper@example.org)",
        )
        for host in ("doi.org", "example.org", "api.crossref.org.example.org", "crossref.org"):
            self.assertEqual(selected.user_agent_for(host), plain)


class RedirectAndRetryTests(unittest.IsolatedAsyncioTestCase):
    """Redirect loops and limits, asking again with GET after HEAD, and the provider throttle."""

    async def test_redirect_loop_and_redirect_limit_are_distinct(self):
        client, _ = client_for(Response(301, (("location", "/"),)))
        self.assertEqual((await client.fetch("https://example.org/")).outcome, "redirect_loop")
        client, _ = client_for(
            Response(301, (("location", "/next"),)), options(online_max_redirects=0)
        )
        self.assertEqual((await client.fetch("https://example.org/")).outcome, "redirect_limit")

    async def test_head_fallback_request_count_and_provider_throttle(self):
        client, transport = client_for(
            [Response(405), Response(200), Response(429)], options(online_max_requests=3)
        )
        self.assertEqual((await client.fetch("https://example.org/first")).outcome, "available")
        self.assertEqual([item.method for item in transport.requests], ["HEAD", "GET"])
        self.assertEqual((await client.fetch("https://example.org/second")).outcome, "rate_limited")
        self.assertEqual((await client.fetch("https://example.org/third")).outcome, "rate_limited")
        self.assertEqual(
            (await client.fetch("https://other.example.org/fourth")).outcome, "request_limit"
        )
        self.assertEqual(len(transport.requests), 3)

    async def test_a_refused_head_is_asked_again_with_get(self):
        for first, second, outcome in (
            (404, 200, "available"),
            (403, 200, "available"),
            (404, 404, "not_found"),
            (403, 403, "access_denied"),
        ):
            with self.subTest(first=first, second=second):
                client, transport = client_for([Response(first), Response(second)])
                result = await client.fetch("https://example.org/")
                self.assertEqual(result.outcome, outcome)
                self.assertEqual([item.method for item in transport.requests], ["HEAD", "GET"])
                # The retry reads no body: only the status matters for a link.
                self.assertFalse(transport.requests[1].want_body)


class AddressFallbackTests(unittest.IsolatedAsyncioTestCase):
    """When and how the client moves on to the next validated address."""

    async def test_connection_failure_moves_on_to_the_next_validated_address(self):
        attempts = []

        def answer(request):
            attempts.append(request.address)
            return ConnectFailure() if request.address == PUBLIC_ADDRESS else Response(200)

        resolver = AsyncMock(return_value=(PUBLIC_IPV6_ADDRESS, PUBLIC_ADDRESS))
        client, _ = client_for(answer, resolver=resolver)
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "available")
        # IPv4 is tried first; its failed connection leaves the IPv6 address to answer.
        self.assertEqual(attempts, [PUBLIC_ADDRESS, PUBLIC_IPV6_ADDRESS])

    async def test_only_a_failed_connection_justifies_another_address(self):
        resolver = AsyncMock(return_value=(PUBLIC_IPV6_ADDRESS, PUBLIC_ADDRESS))
        client, transport = client_for(ConnectFailure(), resolver=resolver)
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(result.message, "Could not connect to any of the server's 2 addresses")
        client, transport = client_for(
            ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), resolver=resolver
        )
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual([item.address for item in transport.requests], [PUBLIC_ADDRESS])

    async def test_connect_timeout_leaves_time_for_the_next_address(self):
        resolver = AsyncMock(side_effect=[(PUBLIC_ADDRESS, "93.184.216.35"), (PUBLIC_ADDRESS,)])
        client, transport = client_for(
            Response(200), options(online_timeout_seconds=8), None, resolver
        )
        await client.fetch("https://example.org/")
        await client.fetch("https://other.example.org/")
        self.assertEqual([item.connect_timeout for item in transport.requests], [4.0, None])


class BudgetAndCacheTests(unittest.IsolatedAsyncioTestCase):
    """The shared request budget, cancellation, the response cache and shared fetches."""

    async def test_shared_budget_and_cancellation_release_requests(self):
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def transport(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        budget = ResourceBudget(1, 128)
        client = OnlineClient(options(), budget, resolver=public_resolver, transport=transport)
        first = asyncio.create_task(client.fetch("https://first.example.org/"))
        await entered.wait()
        second = asyncio.create_task(client.fetch("https://second.example.org/"))
        await asyncio.sleep(0)
        self.assertEqual(budget.statistics()["active_cpu"], 1)
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["queued"], 0)
        self.assertEqual(client.in_flight, 0)

    async def test_request_limit_is_atomic_after_cross_host_budget_admission(self):
        budget = ResourceBudget(1, 128)
        client, transport = client_for(Response(200), options(online_max_requests=1), budget)
        entered, release = asyncio.Event(), asyncio.Event()

        async def occupy():
            async with budget.lease("occupied", memory_mb=64):
                entered.set()
                await release.wait()

        async def queued():
            while budget.statistics()["queued"] != 2:
                await asyncio.sleep(0)

        async with asyncio.TaskGroup() as group:
            group.create_task(occupy())
            await entered.wait()
            first = group.create_task(client.fetch("https://first.example.org/"))
            second = group.create_task(client.fetch("https://second.example.org/"))
            try:
                await asyncio.wait_for(queued(), 2)
                self.assertEqual(client.requests, 0)
                self.assertEqual(transport.requests, [])
            finally:
                release.set()
        results = [first.result(), second.result()]
        self.assertEqual(
            sorted(result.outcome for result in results), ["available", "request_limit"]
        )
        self.assertEqual(client.requests, 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_response_cache_evicts_large_bodies_before_accumulation(self):
        client, transport = client_for(
            Response(200, body=b"x" * 3_000_000), options(online_max_response_bytes=4_194_304)
        )
        for host in ("first", "second", "first"):
            result = await client.fetch(f"https://{host}.example.org/", want_body=True)
            self.assertEqual(result.outcome, "available")
            held = sum(len(item.response.body) for item in client.cache.values() if item.response)
            self.assertLessEqual(held, 4_194_304)
        self.assertEqual(len(transport.requests), 3)

    async def test_identical_concurrent_fetches_share_one_request_and_leave_no_lock_behind(self):
        gate = asyncio.Event()

        async def transport(request):
            await gate.wait()
            return Response(200)

        client = OnlineClient(options(), None, resolver=public_resolver, transport=transport)
        tasks = [asyncio.create_task(client.fetch("https://example.org/")) for _ in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(client.in_flight, 1)
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual({result.outcome for result in results}, {"available"})
        self.assertEqual(client.requests, 1)
        self.assertEqual(client.in_flight, 0)


class DeadlineAndDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Request and fetch deadlines, and the host, time and error code a fetch records."""

    async def test_request_timeout_is_inconclusive(self):
        async def slow(request):
            await asyncio.sleep(2)
            return Response(200)

        client = OnlineClient(
            options(online_timeout_seconds=1), None, resolver=public_resolver, transport=slow
        )
        result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(result.message, "No complete answer arrived within 1 seconds")

    async def test_the_requests_of_one_fetch_share_an_overall_deadline(self):
        async def slow(request):
            await asyncio.sleep(0.3)
            return Response(302, (("location", "/next"),))

        with patch("latexprep.online_http_session.FETCH_TIMEOUT_MULTIPLE", 0.5):
            client = OnlineClient(
                options(online_timeout_seconds=1), None, resolver=public_resolver, transport=slow
            )
            result = await client.fetch("https://example.org/")
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(
            result.message,
            "The redirects and retries of this address used more than 0.5 seconds in total",
        )
        self.assertGreaterEqual(result.elapsed_seconds, 0.5)

    async def test_a_failed_fetch_records_host_time_and_error_code(self):
        client, _ = client_for(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"))
        result = await client.fetch("https://example.org/data")
        self.assertEqual(
            (result.outcome, result.host, result.error_code),
            ("unavailable", "example.org", "ECONNRESET"),
        )
        self.assertIn("ConnectionResetError: Connection reset by peer", result.message)
        self.assertGreaterEqual(result.elapsed_seconds, 0)

        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_message = "unable to get local issuer certificate"
        error.reason = "CERTIFICATE_VERIFY_FAILED"
        client, _ = client_for(error)
        result = await client.fetch("https://example.org/data")
        self.assertEqual(
            (result.outcome, result.error_code), ("tls_failure", "CERTIFICATE_VERIFY_FAILED")
        )

        client, _ = client_for(NetworkFailure(result.outcome, "x"))
        result = await client.fetch("https://example.org/data")
        self.assertEqual((result.host, result.error_code), ("example.org", None))

    async def test_a_successful_fetch_records_host_and_time_too(self):
        client, _ = client_for(Response(200))
        result = await client.fetch("https://example.org/data")
        self.assertEqual((result.outcome, result.host), ("available", "example.org"))
        self.assertGreaterEqual(result.elapsed_seconds, 0)


if __name__ == "__main__":
    unittest.main()
