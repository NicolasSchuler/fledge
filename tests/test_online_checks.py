from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.models import PreparationError
from latexprep.online_checks import (
    NetworkFailure,
    OnlineOptions,
    Response,
    _Client,
    _exchange,
    _public_ip,
    _resolve,
    _target,
    check_online_references,
)
from latexprep.scheduler import ResourceBudget, cancellation_point


def metadata(**changes):
    return {
        "DOI": "10.1234/example",
        "title": ["A reference title"],
        "author": [{"family": "Smith", "given": "Ada"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Example Journal"],
        "type": "journal-article",
        **changes,
    }


def document(message):
    return Response(200, body=json.dumps({"status": "ok", "message": message}).encode())


def options(**changes):
    return OnlineOptions(online=True, online_provider_interval_seconds=0.1, **changes)


class OnlineCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bib = self.root / "references.bib"
        # Entries are only requested through the selected graph's declared resources.
        (self.root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}Text.\n"
            "\\bibliography{references}\n\\end{document}\n"
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def reference(self, extra="doi={10.1234/example}", kind="article"):
        self.bib.write_text(
            f"@{kind}{{key, title={{A reference title}}, author={{Smith, Ada}}, "
            f"year={{2024}}, journal={{Example Journal}}, {extra}}}\n"
        )

    async def run_check(self, selected, response, rule, code):
        original = self.bib.read_bytes() if self.bib.exists() else None
        calls = []

        async def exchange(target, address, method, body, maximum, *extra):
            calls.append((target, address, method, body, *extra))
            self.assertEqual(address, "93.184.216.34")
            return response(target) if callable(response) else response

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            findings = await check_online_references(self.root, "main.tex", options=selected)
        found = next(item for item in findings if item.rule == rule)
        self.assertEqual(found.code, code)
        self.assertTrue(found.details["outcome"])
        if original is not None:
            self.assertEqual(original, self.bib.read_bytes())
        return found, findings, calls

    async def test_offline_default_and_unselected_online_make_zero_dns_or_http_calls(self):
        self.reference()
        with (
            patch("latexprep.online_checks._collect", side_effect=AssertionError("no scan")),
            patch("latexprep.online_checks._resolve", side_effect=AssertionError("no DNS")),
            patch("latexprep.online_checks._exchange", side_effect=AssertionError("no HTTP")),
            patch("socket.getaddrinfo", side_effect=AssertionError("no ambient DNS")),
        ):
            self.assertEqual(await check_online_references(self.root), [])
            self.assertEqual(await check_online_references(self.root, options=options()), [])
            findings = await check_online_references(
                self.root, options=OnlineOptions(online_metadata=True)
            )
            self.assertEqual(len(findings), 1)
            self.assertEqual((findings[0].code, findings[0].status), ("NET002", "skipped"))
            self.assertEqual(findings[0].details["requests"], 0)

    async def test_declared_resources_bound_the_requested_bibliographies(self):
        self.reference()
        spare = self.root / "old" / "references-backup.bib"
        spare.parent.mkdir()
        spare.write_text("@article{spare, title={Spare copy}, doi={10.9999/spare}}\n")
        selected = options(online_doi_resolution=True)

        _, findings, calls = await self.run_check(
            selected, Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(
            [item.path for item in findings if item.rule == "online.doi_resolution"],
            ["references.bib"],
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("10.9999/spare", repr(findings))

        # A bibliography-only directory keeps its documented whole-tree scope.
        (self.root / "main.tex").unlink()
        _, findings, calls = await self.run_check(
            selected, Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            sorted(item.path for item in findings if item.rule == "online.doi_resolution"),
            ["old/references-backup.bib", "references.bib"],
        )

    async def test_unresolvable_resource_declaration_requests_nothing(self):
        self.reference()
        (self.root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n\\bibliography{absent}\n\\end{document}\n"
        )
        finding, _, calls = await self.run_check(
            options(online_doi_resolution=True), Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(finding.status, "inconclusive")
        self.assertEqual(finding.details["outcome"], "input_coverage_incomplete")
        self.assertFalse(calls)

    async def test_contact_email_reaches_only_the_user_agent_header(self):
        self.reference()
        _, _, calls = await self.run_check(
            options(online_doi_resolution=True), Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(calls[0][4], "fledge/0.1 (explicit reference checks)")
        selected = options(online_doi_resolution=True, online_contact_email="paper@example.org")
        _, findings, calls = await self.run_check(
            selected, Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(calls[0][4], "fledge/0.1 (mailto:paper@example.org)")
        self.assertNotIn("paper@example.org", repr(findings))
        for value in (
            "",
            "not-an-email",
            "paper@example",
            "paper@example.org, other@example.org",
            "paper@example.org\r\nX-Injected: 1",
            "p" * 250 + "@example.org",
            b"paper@example.org",
        ):
            with self.subTest(value=value), self.assertRaises(PreparationError):
                replace(selected, online_contact_email=value)

    async def test_doi_resolution_valid_invalid_uncertain(self):
        self.reference()
        selected = options(online_doi_resolution=True)
        for response, status, outcome in (
            (Response(200), "passed", "available"),
            (Response(404), "failed", "not_found"),
            (Response(401), "inconclusive", "authentication_required"),
        ):
            with self.subTest(outcome=outcome):
                finding, findings, calls = await self.run_check(
                    selected, response, "online.doi_resolution", "NET001"
                )
                self.assertEqual((finding.status, finding.details["outcome"]), (status, outcome))
                self.assertEqual((finding.path, finding.line), ("references.bib", 1))
                self.assertEqual(finding.severity, "info" if status == "passed" else "warning")
                self.assertEqual(calls[0][0].host, "doi.org")
                self.assertIn(
                    "bibliographic title",
                    next(item for item in findings if item.rule == "online.sharing").details[
                        "shared_fields"
                    ],
                )

    async def test_metadata_valid_invalid_uncertain(self):
        self.reference()
        selected = options(online_metadata=True)
        for work, expected in (
            (metadata(), "passed"),
            (
                metadata(
                    title=["Different title"],
                    author=[{"family": "Jones"}],
                    issued={"date-parts": [[2023]]},
                    **{"container-title": ["Other Journal"]},
                ),
                "failed",
            ),
            (metadata(author=None), "inconclusive"),
        ):
            with self.subTest(expected=expected):
                finding, _, _ = await self.run_check(
                    selected, document(work), "online.metadata", "NET002"
                )
                self.assertEqual(finding.status, expected)
                self.assertEqual(finding.evidence, "heuristic")
                self.assertEqual(finding.path, "references.bib")
                if expected == "failed":
                    self.assertTrue(
                        all(not row["matches"] for row in finding.details["comparisons"].values())
                    )
                if expected == "inconclusive":
                    self.assertIn("authors", finding.details["unmeasured_fields"])
        finding, _, _ = await self.run_check(
            selected, document(metadata(DOI="10.1234/other")), "online.metadata", "NET002"
        )
        self.assertEqual(finding.details["outcome"], "identifier_mismatch")

    async def test_missing_doi_valid_invalid_uncertain(self):
        self.reference("url={https://example.org/paper}")
        selected = options(online_missing_doi=True)
        for items, outcome, status in (
            ([], "no_match", "passed"),
            ([metadata()], "candidate", "failed"),
            ([metadata(), metadata(DOI="10.1234/second")], "ambiguous_candidates", "inconclusive"),
        ):
            with self.subTest(outcome=outcome):
                finding, _, calls = await self.run_check(
                    selected, document({"items": items}), "online.missing_doi", "NET003"
                )
                self.assertEqual((finding.status, finding.details["outcome"]), (status, outcome))
                self.assertFalse(finding.details["identity_confirmed"])
                self.assertIn("query.bibliographic", calls[0][0].path)
                self.assertEqual(finding.evidence, "heuristic")
        finding, _, _ = await self.run_check(
            selected, document({"items": [{"title": ["No DOI"]}]}), "online.missing_doi", "NET003"
        )
        self.assertEqual(finding.details["outcome"], "malformed")

    async def test_published_version_valid_invalid_uncertain(self):
        self.reference("doi={10.1234/example}, archiveprefix={arXiv}", kind="unpublished")
        selected = options(online_published_versions=True)
        relation = {
            "is-preprint-of": [
                {"id-type": "doi", "id": "10.1234/published", "asserted-by": "subject"}
            ]
        }
        finding, _, _ = await self.run_check(
            selected, document(metadata(relation=relation)), "online.published_version", "NET004"
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("failed", "declared_relation")
        )
        self.assertEqual(finding.details["relations"][0]["doi"], "10.1234/published")
        finding, _, _ = await self.run_check(
            selected, Response(503), "online.published_version", "NET004"
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("inconclusive", "unavailable")
        )

        def no_match(target):
            return document({"items": []}) if "?" in target.path else document(metadata())

        finding, _, _ = await self.run_check(
            selected, no_match, "online.published_version", "NET004"
        )
        self.assertEqual((finding.status, finding.details["outcome"]), ("passed", "no_match"))
        finding, _, _ = await self.run_check(
            selected,
            document(metadata(relation={"is-preprint-of": "broken"})),
            "online.published_version",
            "NET004",
        )
        self.assertEqual(finding.details["outcome"], "malformed")

    async def test_notices_valid_invalid_uncertain(self):
        self.reference()
        selected = options(online_notices=True)
        for kind in ("correction", "retraction"):
            notice = metadata(
                DOI="10.1234/notice", **{"update-to": [{"DOI": "10.1234/example", "type": kind}]}
            )
            finding, _, calls = await self.run_check(
                selected,
                document({"items": [notice], "total-results": 1}),
                "online.notices",
                "NET005",
            )
            self.assertEqual(finding.status, "failed")
            self.assertEqual(finding.details["notices"][0]["type"], kind)
            self.assertEqual(finding.details["notices"][0]["notice_doi"], "10.1234/notice")
            self.assertIn("updates%3A10.1234", calls[0][0].path)
        finding, _, _ = await self.run_check(
            selected, document({"items": [], "total-results": 0}), "online.notices", "NET005"
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("passed", "no_notice_found")
        )
        finding, _, _ = await self.run_check(
            selected, document({"items": [metadata()]}), "online.notices", "NET005"
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("inconclusive", "malformed")
        )

    async def test_reference_link_valid_invalid_uncertain(self):
        self.reference("url={https://example.org/reference}")
        for status, expected in ((200, "passed"), (410, "failed"), (429, "inconclusive")):
            finding, _, _ = await self.run_check(
                options(online_reference_links=True),
                Response(status),
                "online.reference_link",
                "NET006",
            )
            self.assertEqual(finding.status, expected)
            self.assertEqual(finding.details["http_status"], status)
            self.assertEqual(finding.path, "references.bib")

    async def test_replication_link_valid_invalid_uncertain(self):
        (self.root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "% \\url{https://hidden.example.org/}\n"
            "\\url{https://example.org/artifact}\n\\end{document}\n"
        )
        for status, expected in ((200, "passed"), (404, "failed"), (403, "inconclusive")):
            finding, _, calls = await self.run_check(
                options(online_replication_links=True),
                Response(status),
                "online.replication_link",
                "NET007",
            )
            self.assertEqual(finding.status, expected)
            self.assertEqual((finding.path, finding.line), ("main.tex", 4))
            self.assertEqual(len(calls), 1)
            self.assertIn("reproducibility", finding.message)

    async def test_malformed_json_and_nonliteral_inputs_never_pass(self):
        self.reference()
        for response in (
            Response(200, body=b"not-json"),
            document({"items": []}),
            document(metadata(DOI="broken")),
        ):
            finding, _, _ = await self.run_check(
                options(online_metadata=True), response, "online.metadata", "NET002"
            )
            self.assertEqual(finding.status, "inconclusive")
        self.reference("doi=identifier")
        finding, _, calls = await self.run_check(
            options(online_doi_resolution=True), Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(finding.details["outcome"], "input_unavailable")
        self.assertFalse(calls)

    async def test_oversized_metadata_fields_and_extra_candidates_are_inconclusive(self):
        self.reference()
        for work in (
            metadata(title=["x" * 2049]),
            metadata(author=[{"family": "Smith"}] * 201),
            metadata(author=[{"family": "x" * 257}]),
        ):
            finding, _, _ = await self.run_check(
                options(online_metadata=True), document(work), "online.metadata", "NET002"
            )
            self.assertEqual(finding.status, "inconclusive")
        self.reference("url={https://example.org/paper}")
        for items in ([metadata()] * 6, [metadata(title=["x" * 2049])]):
            finding, _, _ = await self.run_check(
                options(online_missing_doi=True),
                document({"items": items}),
                "online.missing_doi",
                "NET003",
            )
            self.assertEqual(
                (finding.status, finding.details["outcome"]), ("inconclusive", "malformed")
            )
        self.reference("doi={10.1234/example}, archiveprefix={arXiv}", kind="unpublished")
        finding, _, _ = await self.run_check(
            options(online_published_versions=True),
            document(
                metadata(
                    relation={
                        "is-preprint-of": [{"id-type": "doi", "id": "10.1234/published"}] * 21
                    }
                )
            ),
            "online.published_version",
            "NET004",
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("inconclusive", "malformed")
        )
        finding, _, _ = await self.run_check(
            options(online_notices=True),
            document(
                {
                    "items": [
                        metadata(
                            DOI="10.1234/notice",
                            **{"update-to": [{"DOI": "10.1234/example", "type": "x" * 129}]},
                        )
                    ]
                }
            ),
            "online.notices",
            "NET005",
        )
        self.assertEqual(
            (finding.status, finding.details["outcome"]), ("inconclusive", "malformed")
        )

    async def test_credential_urls_are_redacted_from_reference_and_replication_findings(self):
        url = "https://user:SECRET@example.org/?token=SECRET"
        self.reference(f"url={{{url}}}")
        for selected, rule, code in (
            (options(online_reference_links=True), "online.reference_link", "NET006"),
            (
                options(online_replication_links=True, online_replication_urls=(url,)),
                "online.replication_link",
                "NET007",
            ),
        ):
            finding, findings, calls = await self.run_check(selected, Response(200), rule, code)
            self.assertEqual(finding.details["outcome"], "unsafe_target")
            self.assertFalse(calls)
            self.assertNotIn("SECRET", repr(findings))

    async def test_requested_metadata_is_shared_once_across_checks_and_entries(self):
        self.reference("doi={10.1234/example}, archiveprefix={arXiv}", kind="unpublished")
        relation = {"is-preprint-of": [{"id-type": "doi", "id": "10.1234/published"}]}
        _, _, calls = await self.run_check(
            options(online_metadata=True, online_published_versions=True),
            document(metadata(relation=relation)),
            "online.metadata",
            "NET002",
        )
        self.assertEqual(len(calls), 1)

    async def test_cancellation_drains_owned_input_reader_before_return(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        def collect(*_args):
            entered.set()
            try:
                release.wait(3)
                try:
                    cancellation_point()
                except PreparationError:
                    return [], [], []
                return [], [], []
            finally:
                stopped.set()

        budget = ResourceBudget(1, 256)
        with patch("latexprep.online_checks._collect", collect):
            task = asyncio.create_task(
                check_online_references(
                    self.root, options=options(online_metadata=True), budget=budget
                )
            )
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(budget.statistics()["active_cpu"], 0)


class NetworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_literals_and_query_credentials_do_not_resolve(self):
        client = _Client(options(), None)
        with patch("latexprep.online_checks._resolve", AsyncMock()) as resolver:
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

    async def test_private_mixed_dns_and_transition_addresses_are_rejected(self):
        for address in (
            "10.0.0.1",
            "192.168.1.1",
            "100.64.0.1",
            "198.18.0.1",
            "224.0.0.1",
            "0.0.0.0",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
            "192.0.0.9",
            "192.88.99.2",
            "2001:20::1",
            "3fff::1",
            "5f00::1",
        ):
            with self.subTest(address=address):
                with self.assertRaises(NetworkFailure):
                    _public_ip(address)
        with (
            patch(
                "latexprep.online_checks._resolve",
                AsyncMock(return_value=("93.184.216.34", "10.0.0.1")),
            ),
            patch("latexprep.online_checks._exchange", AsyncMock()) as exchange,
        ):
            result = await _Client(options(), None).fetch("https://example.org/")
        self.assertEqual(result.outcome, "unsafe_target")
        exchange.assert_not_called()

    async def test_redirect_destinations_are_revalidated_and_rebinding_is_pinned(self):
        calls = []

        async def exchange(target, address, *_args):
            calls.append((target.host, address))
            return Response(302, (("location", "https://internal.example.org/"),))

        resolver = AsyncMock(side_effect=[("93.184.216.34",), ("127.0.0.1",)])
        with (
            patch("latexprep.online_checks._resolve", resolver),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            result = await _Client(options(), None).fetch("https://example.org/")
        self.assertEqual(result.outcome, "unsafe_target")
        self.assertEqual(calls, [("example.org", "93.184.216.34")])
        self.assertEqual(resolver.await_args_list[1].args, ("internal.example.org", 443))

    async def test_redirect_loop_and_redirect_limit_are_distinct(self):
        with patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))):
            with patch(
                "latexprep.online_checks._exchange",
                AsyncMock(return_value=Response(301, (("location", "/"),))),
            ):
                result = await _Client(options(), None).fetch("https://example.org/")
                self.assertEqual(result.outcome, "redirect_loop")
            with patch(
                "latexprep.online_checks._exchange",
                AsyncMock(return_value=Response(301, (("location", "/next"),))),
            ):
                result = await _Client(options(online_max_redirects=0), None).fetch(
                    "https://example.org/"
                )
                self.assertEqual(result.outcome, "redirect_limit")

    async def test_head_fallback_request_count_and_provider_throttle(self):
        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch(
                "latexprep.online_checks._exchange",
                AsyncMock(side_effect=[Response(405), Response(200), Response(429)]),
            ) as exchange,
        ):
            client = _Client(options(online_max_requests=3), None)
            self.assertEqual((await client.fetch("https://example.org/first")).outcome, "available")
            self.assertEqual([call.args[2] for call in exchange.await_args_list], ["HEAD", "GET"])
            self.assertEqual(
                (await client.fetch("https://example.org/second")).outcome, "rate_limited"
            )
            self.assertEqual(
                (await client.fetch("https://example.org/third")).outcome, "rate_limited"
            )
            self.assertEqual(
                (await client.fetch("https://other.example.org/fourth")).outcome, "request_limit"
            )
            self.assertEqual(exchange.await_count, 3)

    async def test_shared_budget_and_cancellation_release_requests(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def exchange(*_args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        budget = ResourceBudget(1, 128)
        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            client = _Client(options(), budget)
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

    async def test_request_limit_is_atomic_after_cross_host_budget_admission(self):
        budget = ResourceBudget(1, 128)
        client = _Client(options(online_max_requests=1), budget)
        entered, release = asyncio.Event(), asyncio.Event()

        async def occupy():
            async with budget.lease("occupied", memory_mb=64):
                entered.set()
                await release.wait()

        async def queued():
            while budget.statistics()["queued"] != 2:
                await asyncio.sleep(0)

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch(
                "latexprep.online_checks._exchange", AsyncMock(return_value=Response(200))
            ) as exchange,
        ):
            async with asyncio.TaskGroup() as group:
                group.create_task(occupy())
                await entered.wait()
                first = group.create_task(client.fetch("https://first.example.org/"))
                second = group.create_task(client.fetch("https://second.example.org/"))
                try:
                    await asyncio.wait_for(queued(), 2)
                    self.assertEqual(client.requests, 0)
                    exchange.assert_not_awaited()
                finally:
                    release.set()
            results = [first.result(), second.result()]
        self.assertEqual(
            sorted(result.outcome for result in results), ["available", "request_limit"]
        )
        self.assertEqual(client.requests, 1)
        self.assertEqual(exchange.await_count, 1)
        self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_response_cache_evicts_large_bodies_before_accumulation(self):
        client = _Client(options(online_max_response_bytes=4_194_304), None)
        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch(
                "latexprep.online_checks._exchange",
                AsyncMock(return_value=Response(200, body=b"x" * 3_000_000)),
            ) as exchange,
        ):
            for host in ("first", "second", "first"):
                result = await client.fetch(f"https://{host}.example.org/", body=True)
                self.assertEqual(result.outcome, "available")
                self.assertLessEqual(
                    sum(len(item.response.body) for item in client.cache.values() if item.response),
                    4_194_304,
                )
        self.assertEqual(exchange.await_count, 3)

    async def test_request_timeout_is_inconclusive(self):
        async def exchange(*_args):
            await asyncio.sleep(2)
            return Response(200)

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=("93.184.216.34",))),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            result = await _Client(options(online_timeout_seconds=1), None).fetch(
                "https://example.org/"
            )
        self.assertEqual(result.outcome, "timeout")

    async def test_timeout_and_repeated_cancellation_reap_resolver_child(self):
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
                    patch("latexprep.online_checks._DNS_SCRIPT", "import time; time.sleep(30)"),
                    patch("latexprep.online_checks.asyncio.create_subprocess_exec", launch),
                ):
                    if cancel:
                        task = asyncio.create_task(_resolve("example.org", 443))
                        await asyncio.wait_for(launched.wait(), 2)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 3)
                    else:
                        client = _Client(options(online_timeout_seconds=1), None)
                        result = await client.fetch("https://example.org/")
                        self.assertEqual(result.outcome, "timeout")
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)

    async def test_socket_connect_uses_numeric_ip_and_original_tls_hostname(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        reader.feed_eof()
        writes = []
        transport = SimpleNamespace(abort=lambda: None)
        writer = SimpleNamespace(
            write=writes.append, drain=AsyncMock(), close=lambda: None, transport=transport
        )
        fake_socket = SimpleNamespace(setblocking=lambda _: None, close=lambda: None)
        loop = asyncio.get_running_loop()
        with (
            patch("latexprep.online_checks.socket.socket", return_value=fake_socket),
            patch.object(loop, "sock_connect", AsyncMock()) as connect,
            patch(
                "latexprep.online_checks.asyncio.open_connection",
                AsyncMock(return_value=(reader, writer)),
            ) as opening,
            patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve again")),
        ):
            response = await _exchange(
                _target("https://example.org/data"), "93.184.216.34", "GET", True, 1024
            )
        self.assertEqual(response.body, b"{}")
        assert connect.await_args is not None and opening.await_args is not None
        self.assertEqual(connect.await_args.args[1], ("93.184.216.34", 443))
        self.assertEqual(opening.await_args.kwargs["server_hostname"], "example.org")
        self.assertTrue(opening.await_args.kwargs["ssl"].check_hostname)
        self.assertIn(b"Host: example.org\r\n", writes[0])
        self.assertNotIn(b"Authorization:", writes[0])
        self.assertNotIn(b"Cookie:", writes[0])

    async def test_transport_rejects_oversize_and_ambiguous_body_framing(self):
        for headers in (
            b"Content-Length: 1025\r\n",
            b"Content-Length: 1\r\nContent-Length: 2\r\n",
            b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n",
        ):
            reader = asyncio.StreamReader()
            reader.feed_data(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n")
            reader.feed_eof()
            writer = SimpleNamespace(
                write=lambda _: None,
                drain=AsyncMock(),
                close=lambda: None,
                transport=SimpleNamespace(abort=lambda: None),
            )
            sock = SimpleNamespace(setblocking=lambda _: None, close=lambda: None)
            with (
                patch("latexprep.online_checks.socket.socket", return_value=sock),
                patch.object(asyncio.get_running_loop(), "sock_connect", AsyncMock()),
                patch(
                    "latexprep.online_checks.asyncio.open_connection",
                    AsyncMock(return_value=(reader, writer)),
                ),
            ):
                with self.assertRaises(NetworkFailure):
                    await _exchange(
                        _target("https://example.org/"), "93.184.216.34", "GET", True, 1024
                    )

    def test_options_are_frozen_and_bounds_are_validated(self):
        base = OnlineOptions()
        for changes in (
            {"online": 1},
            {"online_jobs": 0},
            {"online_max_requests": 1001},
            {"online_timeout_seconds": 0},
            {"online_provider_interval_seconds": float("nan")},
            {"online_replication_urls": ["https://example.org/"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(PreparationError):
                replace(base, **changes)
        with self.assertRaises(AttributeError):
            attribute = "online"
            setattr(base, attribute, True)


if __name__ == "__main__":
    unittest.main()
