"""The online checks end to end, with the network replaced by a fake transport and resolver.

Each check is run through ``check_online_references`` against a small project. How the
client behaves (redirects, budget, caching) is in ``test_online_client``, the wire format
in ``test_online_http_wire`` and the choice of inputs in ``test_online_inputs``.
"""

from __future__ import annotations

import asyncio
import errno
import ssl
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep import __version__
from latexprep.cancellation import AnalysisCancelled, cancellation_point
from latexprep.errors import PreparationError
from latexprep.online_checks import check_online_references
from latexprep.online_http_wire import Response
from latexprep.online_inputs import OnlineInputs
from latexprep.online_options import OnlineOptions
from latexprep.resource_budget import ResourceBudget

from .online_support import (
    PUBLIC_ADDRESS,
    FakeTransport,
    options,
    public_resolver,
)
from .online_support import crossref_reply as document
from .online_support import crossref_work as metadata


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

    def reference(self, extra="doi={10.1234/example}", kind="article", author="Smith, Ada"):
        self.bib.write_text(
            f"@{kind}{{key, title={{A reference title}}, author={{{author}}}, "
            f"year={{2024}}, journal={{Example Journal}}, {extra}}}\n"
        )

    async def run_check(self, selected, answer, rule, code):
        original = self.bib.read_bytes() if self.bib.exists() else None
        transport = FakeTransport(answer)
        findings = await check_online_references(
            self.root,
            "main.tex",
            options=selected,
            resolver=public_resolver,
            transport=transport,
        )
        found = next(item for item in findings if item.rule == rule)
        self.assertEqual(found.code, code)
        self.assertTrue(found.details["outcome"])
        for request in transport.requests:
            self.assertEqual(request.address, PUBLIC_ADDRESS)
        if original is not None:
            self.assertEqual(original, self.bib.read_bytes())
        return found, findings, transport.requests

    async def test_offline_default_and_unselected_online_make_zero_dns_or_http_calls(self):
        self.reference()
        no_dns = AsyncMock(side_effect=AssertionError("no DNS"))
        no_http = AsyncMock(side_effect=AssertionError("no HTTP"))
        with (
            patch("latexprep.online_checks.collect_inputs", side_effect=AssertionError("no scan")),
            patch("socket.getaddrinfo", side_effect=AssertionError("no ambient DNS")),
        ):
            seams = {"resolver": no_dns, "transport": no_http}
            self.assertEqual(await check_online_references(self.root, **seams), [])
            self.assertEqual(
                await check_online_references(self.root, options=options(), **seams), []
            )
            findings = await check_online_references(
                self.root, options=OnlineOptions(online_metadata=True), **seams
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

    async def test_contact_email_reaches_only_the_user_agent_header_of_crossref_requests(self):
        self.reference()
        plain = f"fledge/{__version__} (explicit reference checks)"
        _, _, calls = await self.run_check(
            options(online_doi_resolution=True), Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(calls[0].user_agent, plain)
        selected = options(
            online_doi_resolution=True,
            online_metadata=True,
            online_contact_email="paper@example.org",
        )
        _, findings, calls = await self.run_check(
            selected,
            lambda request: (
                Response(200) if request.target.host == "doi.org" else document(metadata())
            ),
            "online.doi_resolution",
            "NET001",
        )
        agents = {call.target.host: call.user_agent for call in calls}
        self.assertEqual(
            agents["api.crossref.org"], f"fledge/{__version__} (mailto:paper@example.org)"
        )
        # The contact address stays out of requests to every other host.
        self.assertEqual(agents["doi.org"], plain)
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
                self.assertEqual(calls[0].target.host, "doi.org")
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
                self.assertEqual(finding.evidence_kind, "heuristic")
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
                self.assertIn("query.bibliographic", calls[0].target.path)
                self.assertEqual(finding.evidence_kind, "heuristic")
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

        def no_match(request):
            path = request.target.path
            return document({"items": []}) if "?" in path else document(metadata())

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
            self.assertIn("updates%3A10.1234", calls[0].target.path)
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
            # A HEAD refusal or miss is asked again with GET before it is reported.
            self.assertEqual(
                [call.method for call in calls], ["HEAD"] if status == 200 else ["HEAD", "GET"]
            )
            self.assertIn("reproducibility", finding.message)

    async def test_author_comparison_ignores_and_others_and_keeps_compound_names(self):
        def authors(*families):
            return metadata(author=[{"family": family} for family in families])

        smith_jones_brown = authors("Smith", "Jones", "Brown")
        for author, work, expected in (
            # "and others" says the list is cut short: only the listed names are compared.
            ("Smith, Ada and Jones, Bo and others", smith_jones_brown, "passed"),
            ("Smith, Ada and Brown, Cy and others", smith_jones_brown, "failed"),
            ("Smith, Ada and Jones, Bo", smith_jones_brown, "failed"),
            (
                "Smith, A. and Jones, B. and Brown, C. and Green, D. and others",
                smith_jones_brown,
                "failed",
            ),
            # Lowercase particles belong to the family name, in either BibTeX name form.
            ("Jan van der Berg", authors("van der Berg"), "passed"),
            ("van der Berg, Jan", authors("van der Berg"), "passed"),
            ("Berg, Jan van der", authors("Berg"), "passed"),
            ("Jan van der Berg", authors("Berg"), "passed"),
            ("{van der Berg}, Jan", authors("Berg"), "passed"),
            ("Jan van der Berg", authors("Bergmann"), "failed"),
        ):
            with self.subTest(author=author, expected=expected):
                self.reference(author=author)
                finding, _, _ = await self.run_check(
                    options(online_metadata=True), document(work), "online.metadata", "NET002"
                )
                row = finding.details["comparisons"]["authors"]
                self.assertEqual(row["matches"], expected == "passed")

    async def test_metadata_finding_names_the_entry_and_the_first_differences(self):
        self.reference(author="Smith, Ada and Jones, Bo and others")
        work = metadata(
            title=["Different title"],
            author=[{"family": "Smith"}, {"family": "Brown"}],
            issued={"date-parts": [[2023]]},
        )
        finding, _, _ = await self.run_check(
            options(online_metadata=True), document(work), "online.metadata", "NET002"
        )
        self.assertIn("Entry 'key' differs from its Crossref record", finding.message)
        self.assertIn(
            "title: the entry has 'A reference title' but Crossref has 'Different title'",
            finding.message,
        )
        self.assertIn(
            "authors: the entry has 'Smith, Jones and others' but Crossref has 'Smith, Brown'",
            finding.message,
        )
        self.assertIn("and 1 more field(s)", finding.message)
        self.assertEqual(finding.details["comparisons"]["authors"]["local_truncated"], True)

        finding, _, _ = await self.run_check(
            options(online_metadata=True),
            document(metadata(author=None)),
            "online.metadata",
            "NET002",
        )
        self.assertIn("Entry 'key': the comparison with Crossref is incomplete", finding.message)
        self.assertIn("authors cannot be compared", finding.message)

    async def test_candidate_and_notice_findings_name_the_entry_and_the_leads(self):
        self.reference("url={https://example.org/paper}")
        finding, _, _ = await self.run_check(
            options(online_missing_doi=True),
            document({"items": [metadata()]}),
            "online.missing_doi",
            "NET003",
        )
        self.assertIn("Entry 'key' has no DOI; Crossref suggests 10.1234/example", finding.message)
        self.assertIn("'A reference title'", finding.message)
        self.assertIn("title match 100%", finding.message)

        self.reference("doi={10.1234/example}")
        notice = metadata(
            DOI="10.1234/notice",
            **{"update-to": [{"DOI": "10.1234/example", "type": "retraction"}]},
        )
        finding, _, _ = await self.run_check(
            options(online_notices=True),
            document({"items": [notice], "total-results": 1}),
            "online.notices",
            "NET005",
        )
        self.assertIn(
            "Entry 'key': Crossref reports retraction notice 10.1234/notice.", finding.message
        )

        self.reference("doi={10.1234/example}, archiveprefix={arXiv}", kind="unpublished")
        relation = {"is-preprint-of": [{"id-type": "doi", "id": "10.1234/published"}]}
        finding, _, _ = await self.run_check(
            options(online_published_versions=True),
            document(metadata(relation=relation)),
            "online.published_version",
            "NET004",
        )
        self.assertIn("published version (10.1234/published)", finding.message)

    async def test_unsafe_url_finding_states_the_exact_reason_and_the_fix(self):
        for url, reason in (
            (
                "https://example.org:8080/data",
                "Only public HTTP/HTTPS URLs on their standard ports are supported",
            ),
            (
                "https://example.org/data?session=1",
                "Credential-bearing query parameters are never requested",
            ),
        ):
            with self.subTest(url=url):
                self.reference(f"url={{{url}}}")
                finding, _, calls = await self.run_check(
                    options(online_reference_links=True),
                    Response(200),
                    "online.reference_link",
                    "NET006",
                )
                self.assertFalse(calls)
                self.assertIn(f"URL check: unsafe target ({reason})", finding.message)
                self.assertEqual(finding.details["reason"], reason)
                self.assertTrue((finding.suggestion or "").startswith(reason))
                self.assertIn("typo", finding.suggestion or "")

    async def test_transient_outcomes_state_the_reason_and_one_concrete_next_step(self):
        self.reference("url={https://example.org/paper}")

        def raise_timeout(_request):
            return TimeoutError()

        def raise_certificate(_request):
            error = ssl.SSLCertVerificationError(1, "certificate verify failed")
            error.verify_message = "unable to get local issuer certificate"
            return error

        def raise_reset(_request):
            return ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

        for response, outcome, reason, advice in (
            (Response(429), "rate_limited", None, "Wait a few minutes and rerun"),
            (
                raise_timeout,
                "timeout",
                "No complete answer arrived within 10 seconds",
                "raise online_timeout_seconds (at most 60)",
            ),
            (
                raise_certificate,
                "tls_failure",
                "The server's certificate could not be verified "
                "(unable to get local issuer certificate)",
                "CA certificates",
            ),
            (
                raise_reset,
                "unavailable",
                "The network connection failed (ConnectionResetError: Connection reset by peer)",
                "does not use HTTP proxy settings",
            ),
            (Response(403), "access_denied", None, "Open the address in a browser"),
        ):
            with self.subTest(outcome=outcome):
                finding, _, _ = await self.run_check(
                    options(online_reference_links=True),
                    response,
                    "online.reference_link",
                    "NET006",
                )
                self.assertEqual(finding.details["outcome"], outcome)
                self.assertEqual(finding.details["reason"], reason)
                self.assertIn(advice, finding.suggestion or "")
                if reason:
                    self.assertIn(f"({reason})", finding.message)

    async def test_exhausted_request_budget_says_how_to_raise_it(self):
        self.bib.write_text(
            "@misc{one, url={https://example.org/one}}\n@misc{two, url={https://example.org/two}}\n"
        )
        _, findings, _ = await self.run_check(
            options(online_reference_links=True, online_max_requests=1),
            Response(200),
            "online.reference_link",
            "NET006",
        )
        (limited,) = [
            item
            for item in findings
            if item.rule == "online.reference_link" and item.details["outcome"] == "request_limit"
        ]
        self.assertIn("The budget of 1 online requests is used up", limited.message)
        self.assertIn("online_max_requests (at most 1000)", limited.suggestion or "")

    async def test_crossref_lookup_failure_gives_the_reason_and_a_next_step(self):
        self.reference()
        finding, _, _ = await self.run_check(
            options(online_metadata=True), Response(404), "online.metadata", "NET002"
        )
        self.assertIn("Crossref lookup: not found;", finding.message)
        self.assertIn("DataCite", finding.suggestion or "")
        finding, _, _ = await self.run_check(
            options(online_metadata=True),
            document(metadata(DOI="10.1234/other")),
            "online.metadata",
            "NET002",
        )
        self.assertIn("(Crossref returned the record of a different DOI)", finding.message)

    async def test_head_refusals_are_confirmed_with_get_before_a_link_is_reported(self):
        self.reference("url={https://example.org/paper}")
        for first, second, outcome in (
            (404, 200, "available"),
            (403, 200, "available"),
            (404, 404, "not_found"),
            (403, 403, "access_denied"),
        ):
            with self.subTest(first=first, second=second):
                methods = []

                def answer(target, first=first, second=second, methods=methods):
                    methods.append(len(methods))
                    return Response(first if len(methods) == 1 else second)

                finding, _, calls = await self.run_check(
                    options(online_reference_links=True),
                    answer,
                    "online.reference_link",
                    "NET006",
                )
                self.assertEqual([call.method for call in calls], ["HEAD", "GET"])
                self.assertEqual(finding.details["outcome"], outcome)

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

        def collect(*_args, **_options):
            entered.set()
            try:
                release.wait(3)
                try:
                    cancellation_point()
                except AnalysisCancelled:
                    return OnlineInputs([], [], [])
                return OnlineInputs([], [], [])
            finally:
                stopped.set()

        budget = ResourceBudget(1, 256)
        with patch("latexprep.online_checks.collect_inputs", collect):
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


if __name__ == "__main__":
    unittest.main()
