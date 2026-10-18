"""Crossref request URLs and the typed records read from its replies."""

from __future__ import annotations

import json
import unittest
from typing import Any

from latexprep.crossref import (
    NOTICE_ROWS,
    SEARCH_ROWS,
    CrossrefReplyError,
    doi_url,
    notices_url,
    parse_notice_reply,
    parse_search_reply,
    parse_work_reply,
    search_url,
    work_url,
)

from .online_support import crossref_work


def reply(message: Any, status: str = "ok") -> bytes:
    return json.dumps({"status": status, "message": message}).encode()


def refused(parse, body: bytes, *args: Any) -> CrossrefReplyError:
    try:
        parse(body, *args)
    except CrossrefReplyError as error:
        return error
    raise AssertionError("the reply was accepted")


class UrlTests(unittest.TestCase):
    def test_every_url_is_built_from_the_doi_or_title_and_escapes_it(self):
        self.assertEqual(doi_url("10.1000/a b"), "https://doi.org/10.1000/a%20b")
        self.assertEqual(work_url("10.1000/a/b"), "https://api.crossref.org/works/10.1000%2Fa%2Fb")
        self.assertEqual(
            search_url("A & B"),
            f"https://api.crossref.org/works?query.bibliographic=A+%26+B&rows={SEARCH_ROWS}",
        )
        self.assertEqual(
            notices_url("10.1000/x"),
            f"https://api.crossref.org/works?filter=updates%3A10.1000%2Fx&rows={NOTICE_ROWS}",
        )


class WorkReplyTests(unittest.TestCase):
    def test_the_comparable_fields_of_a_record_are_typed(self):
        work = parse_work_reply(
            reply(
                crossref_work(
                    issued={"date-parts": [[2024, 5]]},
                    published={"date-parts": [[2023]]},
                    **{"container-title": ["Journal A"]},
                )
            )
        )
        self.assertEqual(work.doi, "10.1234/example")
        self.assertEqual(work.titles, ["A reference title"])
        self.assertEqual(work.authors, ["Smith"])
        self.assertEqual(work.years, ["2023", "2024"])
        self.assertEqual(work.venues, ["Journal A"])
        self.assertEqual(work.published_versions, [])

    def test_unusable_optional_fields_become_empty_not_errors(self):
        work = parse_work_reply(
            reply(
                crossref_work(
                    title=["x" * 2049],
                    author=[{"family": "Smith"}] * 201,
                    issued={"date-parts": [["2024"]]},
                )
            )
        )
        self.assertEqual((work.titles, work.authors, work.years), ([], [], []))

    def test_a_reply_that_is_not_a_record_is_refused(self):
        for body in (
            b"not json",
            b"[]",
            reply(crossref_work(), status="failed"),
            reply([]),
            reply({"title": ["No DOI"]}),
            reply(crossref_work(DOI="broken")),
        ):
            with self.subTest(body=body[:30]):
                error = refused(parse_work_reply, body)
                self.assertEqual(error.reason, "Crossref returned malformed metadata")
                self.assertEqual(error.outcome, "malformed")

    def test_declared_published_versions_are_listed_with_who_asserted_them(self):
        relation = {
            "is-preprint-of": [
                {"id-type": "doi", "id": "10.1234/published", "asserted-by": "subject"},
                {"id-type": "doi", "id": "10.1234/other", "asserted-by": "stranger"},
            ]
        }
        versions = parse_work_reply(reply(crossref_work(relation=relation))).published_versions
        assert versions is not None
        self.assertEqual(
            [(item.doi, item.asserted_by) for item in versions],
            [("10.1234/published", "subject"), ("10.1234/other", None)],
        )

    def test_a_relation_list_that_cannot_be_read_is_none_not_empty(self):
        broken = (
            {"is-preprint-of": "broken"},
            {"is-preprint-of": [{"id-type": "doi", "id": "10.1234/published"}] * 21},
            {"is-preprint-of": [{"id-type": "doi", "id": "no"}]},
            {"is-preprint-of": [{"id-type": "isbn", "id": "10.1234/x"}]},
        )
        for relation in broken:
            with self.subTest(relation=relation):
                work = parse_work_reply(reply(crossref_work(relation=relation)))
                self.assertIsNone(work.published_versions)
        for relation in ({}, {"is-preprint-of": []}, "text"):
            work = parse_work_reply(reply(crossref_work(relation=relation)))
            self.assertEqual(work.published_versions, [])


class SearchReplyTests(unittest.TestCase):
    def test_candidates_keep_their_type_authors_and_years(self):
        candidates = parse_search_reply(
            reply({"items": [crossref_work(), {**crossref_work(), "author": "x"}]})
        )
        first, second = candidates
        self.assertEqual(
            (first.doi, first.work_type, first.authors),
            ("10.1234/example", "journal-article", ["Smith"]),
        )
        self.assertIsNone(second.authors)

    def test_more_results_than_asked_for_are_refused(self):
        error = refused(parse_search_reply, reply({"items": [crossref_work()] * (SEARCH_ROWS + 1)}))
        self.assertEqual(
            error.reason, f"Crossref returned more than the {SEARCH_ROWS} results asked for"
        )

    def test_a_result_without_doi_or_title_is_refused(self):
        for item in ({"title": ["No DOI"]}, crossref_work(DOI="broken"), crossref_work(title=[])):
            with self.subTest(item=item):
                error = refused(parse_search_reply, reply({"items": [item]}))
                self.assertIn("without a usable DOI or title", error.reason)

    def test_an_item_list_that_is_not_a_list_of_records_is_malformed(self):
        for message in ({"items": "x"}, {"items": [1]}, {"items": [{}] * 21}, {}):
            with self.subTest(message=message):
                self.assertEqual(refused(parse_search_reply, reply(message)).outcome, "malformed")


def notice(doi: str, kind: str, target: str = "10.1234/example") -> dict[str, Any]:
    return {"DOI": doi, "update-to": [{"DOI": target, "type": kind}]}


class NoticeReplyTests(unittest.TestCase):
    def test_notices_name_the_kind_and_the_notice_doi(self):
        body = reply(
            {"items": [notice("10.1000/a", "retraction"), notice("10.1000/b", "correction")]}
        )
        result = parse_notice_reply(body, "10.1234/EXAMPLE")
        self.assertEqual(
            [(item.kind, item.notice_doi) for item in result.notices],
            [("retraction", "10.1000/a"), ("correction", "10.1000/b")],
        )
        self.assertFalse(result.truncated)

    def test_more_results_at_crossref_than_returned_are_flagged(self):
        result = parse_notice_reply(
            reply({"items": [notice("10.1000/a", "retraction")], "total-results": 5}),
            "10.1234/example",
        )
        self.assertTrue(result.truncated)

    def test_an_update_record_for_another_doi_is_an_identifier_mismatch(self):
        body = reply({"items": [notice("10.1000/a", "retraction", target="10.9999/other")]})
        error = refused(parse_notice_reply, body, "10.1234/example")
        self.assertEqual(error.outcome, "identifier_mismatch")

    def test_more_notices_than_asked_for_exceed_the_response_limit(self):
        updates = [{"DOI": "10.1234/example", "type": "correction"}] * (NOTICE_ROWS // 2 + 1)
        items = [{"DOI": f"10.1000/n{index}", "update-to": updates} for index in range(2)]
        error = refused(parse_notice_reply, reply({"items": items}), "10.1234/example")
        self.assertEqual(error.outcome, "response_limit")

    def test_unreadable_update_records_are_refused(self):
        bad = (
            {"DOI": "broken", "update-to": []},
            {"DOI": "10.1000/a"},
            {"DOI": "10.1000/a", "update-to": ["x"]},
            {"DOI": "10.1000/a", "update-to": [{"DOI": "10.1234/example", "type": "x" * 129}]},
            {"DOI": "10.1000/a", "update-to": [{"DOI": "10.1234/example", "type": "1bad"}]},
        )
        for item in bad:
            with self.subTest(item=item):
                error = refused(parse_notice_reply, reply({"items": [item]}), "10.1234/example")
                self.assertEqual(error.outcome, "malformed")


if __name__ == "__main__":
    unittest.main()
