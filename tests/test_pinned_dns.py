"""Name resolution in a disposable child: validation, failures, cancellation and the job cache."""

from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch

from latexprep.online_http_session import OnlineClient
from latexprep.online_options import NetworkFailure
from latexprep.pinned_dns import DNS_SCRIPT, DNS_UNRESOLVED_EXIT, JobResolver, resolve_public

from .online_support import FakeTransport, options


class ResolvePublicTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_ip_literal_needs_no_child_and_must_be_public(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("no child")):
            self.assertEqual(await resolve_public("93.184.216.34", 443), ("93.184.216.34",))
            with self.assertRaises(NetworkFailure) as raised:
                await resolve_public("10.0.0.1", 443)
        self.assertEqual(raised.exception.outcome, "unsafe_target")

    async def test_an_unresolvable_host_has_its_own_outcome(self):
        with patch("latexprep.pinned_dns.DNS_SCRIPT", f"raise SystemExit({DNS_UNRESOLVED_EXIT})"):
            with self.assertRaises(NetworkFailure) as raised:
                await resolve_public("no-such-host.example.org", 443)
        self.assertEqual(raised.exception.outcome, "host_unresolved")
        self.assertEqual(
            str(raised.exception), "The host name no-such-host.example.org could not be resolved"
        )

    async def test_any_other_resolver_failure_stays_generic(self):
        for script in ("raise SystemExit(2)", "print('not json')", "print('[]')"):
            with self.subTest(script=script), patch("latexprep.pinned_dns.DNS_SCRIPT", script):
                with self.assertRaises(NetworkFailure) as raised:
                    await resolve_public("example.org", 443)
            self.assertEqual(raised.exception.outcome, "unavailable")

    async def test_a_child_answer_with_a_private_address_is_unsafe(self):
        script = 'print(\'["93.184.216.34", "10.0.0.1"]\')'
        with patch("latexprep.pinned_dns.DNS_SCRIPT", script):
            with self.assertRaises(NetworkFailure) as raised:
                await resolve_public("example.org", 443)
        self.assertEqual(raised.exception.outcome, "unsafe_target")

    async def test_an_overlong_answer_is_refused_before_it_is_buffered(self):
        with patch("latexprep.pinned_dns.DNS_SCRIPT", "print('x' * 100000)"):
            with self.assertRaises(NetworkFailure) as raised:
                await resolve_public("example.org", 443)
        self.assertEqual(str(raised.exception), "DNS response exceeded its byte limit")

    async def test_the_shipped_script_exits_with_the_unresolved_status_for_a_bad_name(self):
        # Run the shipped script with a resolver that reports an unknown name.
        wrapper = (
            "import socket, sys\n"
            "def unknown(*args, **kwargs):\n"
            "    raise socket.gaierror(8, 'nodename nor servname provided')\n"
            "socket.getaddrinfo = unknown\n"
            "sys.argv = ['resolver', 'bad.example.org', '443']\n"
            "exec(compile(sys.stdin.read(), 'resolver', 'exec'))\n"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            wrapper,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.communicate(DNS_SCRIPT.encode()), 10)
        self.assertEqual(process.returncode, DNS_UNRESOLVED_EXIT)

    async def test_timeout_and_repeated_cancellation_reap_the_resolver_child(self):
        original = asyncio.create_subprocess_exec
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                children = []
                launched, release = asyncio.Event(), asyncio.Event()

                async def launch(
                    *args,
                    children=children,
                    launched=launched,
                    cancel=cancel,
                    release=release,
                    **kwargs,
                ):
                    child = await original(*args, **kwargs)
                    children.append(child)
                    launched.set()
                    if cancel:
                        await release.wait()
                    return child

                with (
                    patch("latexprep.pinned_dns.DNS_SCRIPT", "import time; time.sleep(30)"),
                    patch("asyncio.create_subprocess_exec", launch),
                ):
                    if cancel:
                        task = asyncio.create_task(resolve_public("example.org", 443))
                        await asyncio.wait_for(launched.wait(), 2)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 3)
                    else:
                        client = OnlineClient(
                            options(online_timeout_seconds=1),
                            None,
                            transport=FakeTransport(AssertionError("must not connect")),
                        )
                        result = await client.fetch("https://example.org/")
                        self.assertEqual(result.outcome, "timeout")
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)


class JobResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_host_is_looked_up_once_per_job(self):
        lookup = AsyncMock(return_value=("93.184.216.34",))
        resolver = JobResolver(lookup)
        for _ in range(3):
            self.assertEqual(await resolver("example.org", 443), ("93.184.216.34",))
        await resolver("other.example.org", 443)
        await resolver("example.org", 80)
        self.assertEqual(lookup.await_count, 3)

    async def test_a_failed_lookup_is_not_remembered(self):
        lookup = AsyncMock(
            side_effect=[NetworkFailure("unavailable", "no answer"), ("93.184.216.34",)]
        )
        resolver = JobResolver(lookup)
        with self.assertRaises(NetworkFailure):
            await resolver("example.org", 443)
        self.assertEqual(await resolver("example.org", 443), ("93.184.216.34",))


if __name__ == "__main__":
    unittest.main()
