from __future__ import annotations

import asyncio
import json
import ssl
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep import __version__
from latexprep.bibtex_syntax import BibFileReader
from latexprep.cancellation import AnalysisCancelled, cancellation_point
from latexprep.errors import PreparationError
from latexprep.online_checks import (
    _DNS_SCRIPT,
    NetworkFailure,
    OnlineOptions,
    Response,
    _Client,
    _collect,
    _ConnectFailure,
    _exchange,
    _public_ip,
    _resolve,
    _scan_bound,
    _target,
    check_online_references,
)
from latexprep.resource_budget import ResourceBudget


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

    def reference(self, extra="doi={10.1234/example}", kind="article", author="Smith, Ada"):
        self.bib.write_text(
            f"@{kind}{{key, title={{A reference title}}, author={{{author}}}, "
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

    async def test_contact_email_reaches_only_the_user_agent_header_of_crossref_requests(self):
        self.reference()
        plain = f"fledge/{__version__} (explicit reference checks)"
        _, _, calls = await self.run_check(
            options(online_doi_resolution=True), Response(200), "online.doi_resolution", "NET001"
        )
        self.assertEqual(calls[0][4], plain)
        selected = options(
            online_doi_resolution=True,
            online_metadata=True,
            online_contact_email="paper@example.org",
        )
        _, findings, calls = await self.run_check(
            selected,
            lambda target: Response(200) if target.host == "doi.org" else document(metadata()),
            "online.doi_resolution",
            "NET001",
        )
        agents = {call[0].host: call[4] for call in calls}
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
                self.assertIn("query.bibliographic", calls[0][0].path)
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
            # A HEAD refusal or miss is asked again with GET before it is reported.
            self.assertEqual(
                [call[2] for call in calls], ["HEAD"] if status == 200 else ["HEAD", "GET"]
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

        def raise_timeout(_target):
            raise TimeoutError

        def raise_certificate(_target):
            error = ssl.SSLCertVerificationError(1, "certificate verify failed")
            error.verify_message = "unable to get local issuer certificate"
            raise error

        def raise_reset(_target):
            raise ConnectionResetError(54, "Connection reset by peer")

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
                self.assertEqual([call[2] for call in calls], ["HEAD", "GET"])
                self.assertEqual(finding.details["outcome"], outcome)

    async def test_source_link_scan_reports_sorted_distinct_gaps_when_the_bound_is_hit(self):
        (self.root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\url{\\first}\n\\url{\\second}\n"
            "\\url{https://a.example.org/}\n\\url{https://b.example.org/}\n"
            "\\url{https://c.example.org/}\n\\end{document}\n"
        )
        with patch("latexprep.online_checks._MAX_REFERENCES", 2):
            _, links, gaps = _collect(self.root, "main.tex", options(online_replication_links=True))
        self.assertEqual(len(links), 2)
        self.assertIn("Source-link scan exceeded its 2-link bound", gaps)
        self.assertEqual(gaps, sorted(set(gaps)))

    async def test_input_limit_messages_are_built_from_the_limit_constants(self):
        self.reference()
        with patch("latexprep.online_checks._MAX_BIB_BYTES", 3 * 1024 * 1024):
            with patch("latexprep.online_checks._MAX_INPUT_BYTES", 5 * 1024 * 1024):
                self.assertEqual(
                    _scan_bound(), "online scan bound of 3 MiB per file and 5 MiB in total"
                )
        with patch("latexprep.online_checks._MAX_BIB_BYTES", 16):
            _, _, gaps = _collect(self.root, "main.tex", options(online_metadata=True))
            self.assertEqual(gaps, [f"references.bib exceeds the {_scan_bound()}"])
        self.bib.write_text("@misc{one, doi={10.1/a}}\n@misc{two, doi={10.1/b}}\n")
        with patch("latexprep.online_checks._MAX_REFERENCES", 1):
            _, _, gaps = _collect(self.root, "main.tex", options(online_metadata=True))
        self.assertEqual(gaps, ["Online bibliography scan exceeded its 1-entry bound"])

    async def test_unreadable_bibliographies_become_gaps_that_name_the_file_and_cause(self):
        self.bib.write_bytes(b"@misc{k, title={\xff}}")
        _, _, gaps = _collect(self.root, "main.tex", options(online_metadata=True))
        self.assertEqual(
            gaps, ["references.bib is not valid UTF-8 (first invalid byte at offset 16)"]
        )
        self.reference()
        with patch.object(
            BibFileReader, "read", side_effect=PermissionError(13, "Permission denied")
        ):
            _, _, gaps = _collect(self.root, "main.tex", options(online_metadata=True))
        self.assertEqual(gaps, ["references.bib could not be read (Permission denied)"])

    async def test_missing_input_directory_uses_the_shared_wording(self):
        self.reference()
        with self.assertRaises(PreparationError) as raised:
            _collect(self.bib, "main.tex", options(online_metadata=True))
        self.assertTrue(
            str(raised.exception).startswith("Online reference input must be a directory, but ")
        )

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
                except AnalysisCancelled:
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

    async def exchange_bytes(self, payload, *, method="GET", body=True, connect=None):
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
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
            patch.object(asyncio.get_running_loop(), "sock_connect", connect or AsyncMock()),
            patch(
                "latexprep.online_checks.asyncio.open_connection",
                AsyncMock(return_value=(reader, writer)),
            ),
        ):
            return await _exchange(
                _target("https://example.org/"), "93.184.216.34", method, body, 1024
            )

    async def test_interim_responses_are_skipped_and_never_taken_as_the_final_answer(self):
        response = await self.exchange_bytes(
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
                await self.exchange_bytes(payload, body=False)
            self.assertEqual(raised.exception.outcome, "malformed")

    async def test_failed_connection_is_reported_as_a_connect_failure(self):
        for failure in (OSError(111, "Connection refused"), TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(_ConnectFailure):
                    await self.exchange_bytes(b"", connect=AsyncMock(side_effect=failure))

    async def test_connection_failure_moves_on_to_the_next_validated_address(self):
        attempts = []
        v4, v6 = "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"

        async def exchange(target, address, *_args):
            attempts.append(address)
            if address == v4:
                raise _ConnectFailure
            return Response(200)

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=(v6, v4))),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            result = await _Client(options(), None).fetch("https://example.org/")
        self.assertEqual(result.outcome, "available")
        # IPv4 is tried first; its failed connection leaves the IPv6 address to answer.
        self.assertEqual(attempts, [v4, v6])

        attempts.clear()

        async def unreachable(target, address, *_args):
            attempts.append(address)
            raise _ConnectFailure

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=(v6, v4))),
            patch("latexprep.online_checks._exchange", unreachable),
        ):
            result = await _Client(options(), None).fetch("https://example.org/")
        self.assertEqual((result.outcome, attempts), ("unavailable", [v4, v6]))
        self.assertEqual(result.message, "Could not connect to any of the server's 2 addresses")

        attempts.clear()

        async def resets(target, address, *_args):
            attempts.append(address)
            raise ConnectionResetError(54, "Connection reset by peer")

        with (
            patch("latexprep.online_checks._resolve", AsyncMock(return_value=(v6, v4))),
            patch("latexprep.online_checks._exchange", resets),
        ):
            result = await _Client(options(), None).fetch("https://example.org/")
        # Only a failed connection justifies another address; a dropped request does not.
        self.assertEqual((result.outcome, attempts), ("unavailable", [v4]))

    async def test_connect_timeout_leaves_time_for_the_next_address(self):
        timeouts = []

        async def exchange(target, address, method, body, maximum, agent, connect_timeout):
            timeouts.append(connect_timeout)
            return Response(200)

        addresses = ("93.184.216.34", "93.184.216.35")
        with (
            patch(
                "latexprep.online_checks._resolve",
                AsyncMock(side_effect=[addresses, addresses[:1]]),
            ),
            patch("latexprep.online_checks._exchange", exchange),
        ):
            client = _Client(options(online_timeout_seconds=8), None)
            await client.fetch("https://example.org/")
            await client.fetch("https://other.example.org/")
        self.assertEqual(timeouts, [4.0, None])

    async def test_unresolvable_host_has_its_own_outcome(self):
        with patch("latexprep.online_checks._DNS_SCRIPT", "raise SystemExit(3)"):
            with self.assertRaises(NetworkFailure) as raised:
                await _resolve("no-such-host.example.org", 443)
            result = await _Client(options(), None).fetch("https://no-such-host.example.org/")
        self.assertEqual(raised.exception.outcome, "host_unresolved")
        self.assertEqual(result.outcome, "host_unresolved")
        self.assertEqual(
            result.message, "The host name no-such-host.example.org could not be resolved"
        )
        # Any other resolver failure stays a generic unavailable answer.
        with patch("latexprep.online_checks._DNS_SCRIPT", "raise SystemExit(2)"):
            with self.assertRaises(NetworkFailure) as raised:
                await _resolve("example.org", 443)
        self.assertEqual(raised.exception.outcome, "unavailable")

    async def test_resolver_script_exits_with_the_unresolved_status_for_a_bad_name(self):
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
        await asyncio.wait_for(process.communicate(_DNS_SCRIPT.encode()), 10)
        self.assertEqual(process.returncode, 3)

    async def test_https_to_http_redirect_is_refused_for_metadata_but_not_for_link_checks(self):
        downgrade = Response(302, (("location", "http://example.org/data"),))
        for body, outcome in ((True, "unsafe_target"), (False, "available")):
            with self.subTest(body=body):
                exchange = AsyncMock(side_effect=[downgrade, Response(200)])
                with (
                    patch(
                        "latexprep.online_checks._resolve",
                        AsyncMock(return_value=("93.184.216.34",)),
                    ),
                    patch("latexprep.online_checks._exchange", exchange),
                ):
                    result = await _Client(options(), None).fetch(
                        "https://api.crossref.org/works/10.1234%2Fexample", body=body
                    )
                self.assertEqual(result.outcome, outcome)
                if body:
                    self.assertEqual(exchange.await_count, 1)
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
