"""Explicit, bounded online reference checks; no rewriting or implicit networking.

This module owns the checks themselves and their findings: DOI resolution, metadata
agreement, missing-DOI and published-version candidates, correction and retraction
notices, and the health of reference and source links. It decides what each outcome of a
request means for a researcher (``passed``, ``failed`` or ``inconclusive``), what to do
next, and how the work is ordered and budgeted. The pieces it uses are one module each:
the inputs (``online_inputs``), the Crossref API (``crossref``), the comparison
(``online_comparison``), the client (``online_http_session``) and its wire format
(``online_http_wire``), the address policy (``public_url``) and name resolution
(``pinned_dns``).

Nothing leaves the machine unless the settings allow it: ``OnlineOptions.online`` is off
by default, and each check is a separate switch. What a check shares is stated in the
``online.sharing`` finding, and a request that fails is advisory, never a verified match.
"""

from __future__ import annotations

import contextlib
import difflib
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

from .analysed_source import AnalysedSource
from .cancellation import bounded_map, run_in_thread
from .crossref import (
    SEARCH_ROWS,
    CrossrefCandidate,
    CrossrefReplyError,
    CrossrefWork,
    NoticeReply,
    doi_url,
    notices_url,
    parse_notice_reply,
    parse_search_reply,
    parse_work_reply,
    search_url,
    work_url,
)
from .findings import EvidenceKind, Finding, Severity, Status
from .online_comparison import MetadataComparison, compare_work, normalized_text
from .online_http_session import Fetch, OnlineClient, Transport
from .online_inputs import OnlineInputs, Reference, collect_inputs
from .online_options import FetchOutcome, OnlineOptions, option_maximum
from .pinned_dns import Resolver
from .resource_budget import ResourceBudget

_MAX_LISTED_DIFFERENCES = 2
_MAX_LISTED_ITEMS = 3
_MAX_QUOTED_CHARS = 80
_MAX_TITLE_CHARACTERS = 2048
# Memory held while the entries and links to check are read from the project.
_INPUT_COLLECTION_MEMORY_MB = 128
# Types of Crossref record that can be the published version of a preprint.
_PUBLISHED_TYPES = frozenset({"journal-article", "proceedings-article", "book-chapter"})
_PREPRINT_KINDS = frozenset({"unpublished", "preprint"})
_Reply = TypeVar("_Reply")


# --- Findings about one entry or link ----------------------------------------------


def _reference_finding(
    rule: str,
    reference: Reference,
    message: str,
    *,
    status: Status = "inconclusive",
    severity: Severity = "warning",
    evidence: EvidenceKind = EvidenceKind.DIRECT,
    incomplete: bool = False,
    suggestion: str | None = None,
    **details: object,
) -> Finding:
    return Finding(
        rule,
        message,
        severity=severity,
        status=status,
        path=reference.path,
        line=reference.line,
        evidence_kind=evidence,
        incomplete=incomplete,
        suggestion=suggestion,
        details={"citation_key": reference.key, **details},
    )


def _input_unavailable(rule: str, reference: Reference, message: str) -> Finding:
    """The finding for an entry whose input cannot be used; no request was made for it."""
    return _reference_finding(rule, reference, message, outcome="input_unavailable")


def _fetch_details(result: Fetch) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "url": result.url,
        "http_status": result.response.status if result.response else None,
        "redirects": list(result.redirects),
        "reason": result.message or None,
        "host": result.host,
        "elapsed_seconds": round(result.elapsed_seconds, 3) if result.host else None,
        "error_code": result.error_code,
    }


_OUTCOME_LABELS = {
    FetchOutcome.HOST_UNRESOLVED: "host name could not be resolved",
    FetchOutcome.TLS_FAILURE: "secure connection failed",
}
_NO_PROXY_NOTE = (
    "This tool connects directly and does not use HTTP proxy settings, so a network "
    "that only allows proxied access blocks these checks."
)
_REFUSED_NEXT_STEP = (
    "The server refused an anonymous request. Open the address in a browser to confirm "
    "that it works."
)
_UNUSABLE_NEXT_STEP = (
    "The provider's reply was not usable; rerun later and compare the entry by hand."
)
_NEXT_STEPS: dict[FetchOutcome, str] = {
    FetchOutcome.RATE_LIMITED: (
        "The provider asked for fewer requests. Wait a few minutes and rerun, or raise "
        "online_provider_interval_seconds to space the requests further apart."
    ),
    FetchOutcome.REQUEST_LIMIT: (
        "The request budget for this run was used up before this entry. Raise "
        f"online_max_requests (at most {option_maximum('online_max_requests')}) in "
        "[online_checks] and rerun."
    ),
    FetchOutcome.TIMEOUT: (
        "The provider did not answer in time. Rerun later or raise online_timeout_seconds "
        f"(at most {option_maximum('online_timeout_seconds')}) in [online_checks]. "
        f"{_NO_PROXY_NOTE}"
    ),
    FetchOutcome.TLS_FAILURE: (
        "If every host fails this way, check that this Python can find your system's CA "
        "certificates (python.org builds on macOS ship an 'Install Certificates.command'); "
        "if only this host fails, its certificate is invalid and the site owner must fix it."
    ),
    FetchOutcome.HOST_UNRESOLVED: (
        "Check the spelling of the host name and your network connection, then rerun."
    ),
    FetchOutcome.UNAVAILABLE: (
        f"Rerun later; the provider or the network may be down. {_NO_PROXY_NOTE}"
    ),
    FetchOutcome.ACCESS_DENIED: _REFUSED_NEXT_STEP,
    FetchOutcome.AUTHENTICATION_REQUIRED: _REFUSED_NEXT_STEP,
    FetchOutcome.REDIRECT_LIMIT: (
        "Open the address in a browser. If its redirect chain is legitimately long, raise "
        f"online_max_redirects (at most {option_maximum('online_max_redirects')})."
    ),
    FetchOutcome.REDIRECT_LOOP: "The address redirects back to itself; correct it.",
    FetchOutcome.RESPONSE_LIMIT: (
        "The reply was larger than allowed. Raise online_max_response_bytes "
        f"(at most {option_maximum('online_max_response_bytes')})."
    ),
    FetchOutcome.MALFORMED: _UNUSABLE_NEXT_STEP,
    FetchOutcome.IDENTIFIER_MISMATCH: _UNUSABLE_NEXT_STEP,
}


def _outcome_label(outcome: FetchOutcome) -> str:
    return _OUTCOME_LABELS.get(outcome) or outcome.replace("_", " ")


def _fetch_summary(subject: str, result: Fetch) -> str:
    """``subject``, the outcome in words and, in parentheses, the reason when one is known."""
    text = f"{subject}: {_outcome_label(result.outcome)}"
    return f"{text} ({result.message.rstrip('.')})" if result.message else text


def _outcome_suggestion(result: Fetch) -> str | None:
    """What to do about an outcome that is not a verdict on the link or record itself."""
    if result.outcome is FetchOutcome.UNSAFE_TARGET:
        return (
            f"{result.message or 'This address cannot be requested'}. Correct the address if "
            "it is a typo; otherwise it cannot be checked online."
        )
    return _NEXT_STEPS.get(result.outcome)


# Link availability is decided only by these outcomes; any other is inconclusive.
_LINK_STATUS: dict[FetchOutcome, Status] = {
    FetchOutcome.AVAILABLE: "passed",
    FetchOutcome.NOT_FOUND: "failed",
    FetchOutcome.GONE: "failed",
}


async def _health(client: OnlineClient, reference: Reference, rule: str, url: str) -> Finding:
    result = await client.fetch(url)
    status = _LINK_STATUS.get(result.outcome, "inconclusive")
    return _reference_finding(
        rule,
        reference,
        f"{_fetch_summary('URL check', result)}. "
        "Availability does not establish identity or reproducibility.",
        status=status,
        severity="info" if status == "passed" else "warning",
        suggestion=_outcome_suggestion(result) if status == "inconclusive" else None,
        **_fetch_details(result),
    )


# --- Crossref replies ---------------------------------------------------------------


def _unusable_reply(
    fetched: Fetch, reason: str, outcome: FetchOutcome = FetchOutcome.MALFORMED
) -> Fetch:
    """``fetched`` re-labelled as a reply that cannot be used, with the reason."""
    return replace(fetched, outcome=outcome, message=reason)


async def _crossref_reply(
    client: OnlineClient, url: str, parse: Callable[[bytes], _Reply]
) -> tuple[_Reply | None, Fetch]:
    """Fetch ``url`` and parse the reply; None with the failed fetch when it cannot be used."""
    fetched = await client.fetch(url, want_body=True)
    if fetched.outcome != FetchOutcome.AVAILABLE or fetched.response is None:
        return None, fetched
    try:
        return parse(fetched.response.body), fetched
    except CrossrefReplyError as error:
        return None, _unusable_reply(fetched, error.reason, error.outcome)


def _unavailable(rule: str, reference: Reference, fetched: Fetch) -> Finding:
    suggestion = _outcome_suggestion(fetched)
    if fetched.outcome in {FetchOutcome.NOT_FOUND, FetchOutcome.GONE}:
        suggestion = (
            "Check the DOI. Crossref does not hold DOIs registered by other agencies, such as "
            "DataCite (used by Zenodo and arXiv)."
        )
    return _reference_finding(
        rule,
        reference,
        f"{_fetch_summary('Crossref lookup', fetched)}; no metadata conclusion is available.",
        suggestion=suggestion,
        provider="Crossref",
        **_fetch_details(fetched),
    )


async def _crossref_work(
    client: OnlineClient, reference: Reference
) -> tuple[CrossrefWork | None, Fetch]:
    """The Crossref record of the reference's DOI, or None with the failed fetch."""
    if not reference.doi:
        return None, Fetch(FetchOutcome.INPUT_UNAVAILABLE, "")
    work, fetched = await _crossref_reply(client, work_url(reference.doi), parse_work_reply)
    if work is not None and work.doi.casefold() != reference.doi.casefold():
        return None, _unusable_reply(
            fetched,
            "Crossref returned the record of a different DOI",
            FetchOutcome.IDENTIFIER_MISMATCH,
        )
    return work, fetched


# --- DOI resolution and metadata agreement ------------------------------------------


async def _check_doi_resolution(client: OnlineClient, reference: Reference) -> Finding:
    if reference.doi:
        return await _health(client, reference, "online.doi_resolution", doi_url(reference.doi))
    return _input_unavailable(
        "online.doi_resolution",
        reference,
        "DOI is invalid, ambiguous or nonliteral; no request was made.",
    )


def _quoted(values: list[str], *, others: bool = False) -> str:
    text = ", ".join(values) if len(values) > 1 else (values[0] if values else "")
    if others:
        text += " and others"
    if len(text) > _MAX_QUOTED_CHARS:
        text = text[: _MAX_QUOTED_CHARS - 1] + "…"
    return repr(text)


def _difference(name: str, comparison: MetadataComparison) -> str:
    row = comparison.fields[name]
    return (
        f"{name}: the entry has {_quoted(row.local, others=row.local_truncated)}"
        f" but Crossref has {_quoted(row.remote)}"
    )


def _differences(comparison: MetadataComparison) -> str:
    """The first differing fields with the entry's and Crossref's value of each."""
    fields = comparison.mismatched
    listed = [_difference(name, comparison) for name in fields[:_MAX_LISTED_DIFFERENCES]]
    if len(fields) > _MAX_LISTED_DIFFERENCES:
        listed.append(f"and {len(fields) - _MAX_LISTED_DIFFERENCES} more field(s)")
    return "; ".join(listed)


_METADATA_STATUS: dict[str, Status] = {
    "mismatch": "failed",
    "incomplete": "inconclusive",
    "agreement": "passed",
}


def _metadata_message(key: str, comparison: MetadataComparison) -> str:
    if comparison.outcome == "mismatch":
        return (
            f"Entry {key!r} differs from its Crossref record ({_differences(comparison)}). "
            "Check which version is right before editing the entry."
        )
    if comparison.outcome == "incomplete":
        return (
            f"Entry {key!r}: the comparison with Crossref is incomplete because "
            f"{', '.join(comparison.unmeasured)} cannot be compared."
        )
    return f"Entry {key!r}: supported literal metadata agrees with Crossref."


def _metadata_finding(reference: Reference, work: CrossrefWork, source: str) -> Finding:
    comparison = compare_work(reference, work)
    status = _METADATA_STATUS[comparison.outcome]
    return _reference_finding(
        "online.metadata",
        reference,
        _metadata_message(reference.key, comparison),
        status=status,
        severity="info" if status == "passed" else "warning",
        evidence=EvidenceKind.HEURISTIC,
        # A field without comparable values on both sides is unmeasured.
        incomplete=bool(comparison.unmeasured),
        provider="Crossref",
        source_url=source,
        outcome=comparison.outcome,
        comparisons=comparison.as_details(),
        unmeasured_fields=comparison.unmeasured,
        scope=(
            "Normalized literal title, author family names in order, publication year and venue; "
            "no identity proof"
        ),
    )


def _check_metadata(reference: Reference, work: CrossrefWork | None, fetched: Fetch) -> Finding:
    if work is not None:
        return _metadata_finding(reference, work, fetched.url)
    if reference.doi:
        return _unavailable("online.metadata", reference, fetched)
    return _input_unavailable(
        "online.metadata", reference, "A supported literal DOI is required for metadata comparison."
    )


# --- Candidate searches: missing DOIs and published versions --------------------------


class _Ranked(NamedTuple):
    candidate: CrossrefCandidate
    similarity: float


class _Search(NamedTuple):
    ranked: list[_Ranked]
    fetched: Fetch


def _candidate_row(ranked: _Ranked) -> dict[str, Any]:
    candidate = ranked.candidate
    return {
        "doi": candidate.doi,
        "url": doi_url(candidate.doi),
        "title": candidate.titles[0],
        "authors": candidate.authors,
        "years": candidate.years,
        "title_similarity": ranked.similarity,
    }


def _candidate_list(ranked: list[_Ranked]) -> str:
    listed = [
        f"{item.candidate.doi} ({_quoted([item.candidate.titles[0]])}, "
        f"title match {item.similarity:.0%})"
        for item in ranked[:_MAX_LISTED_ITEMS]
    ]
    if len(ranked) > _MAX_LISTED_ITEMS:
        listed.append(f"and {len(ranked) - _MAX_LISTED_ITEMS} more")
    return "; ".join(listed)


async def _search(
    client: OnlineClient, reference: Reference, rule: str, *, only_types: frozenset[str] | None
) -> _Search | Finding:
    """Search Crossref for the entry's title: the ranked candidates, or the finding saying why not.

    A candidate is dropped when ``only_types`` is given and its type is not among them, or
    when it is the entry's own DOI.
    """
    title = reference.values.get("title")
    if not title or len(title) > _MAX_TITLE_CHARACTERS:
        return _input_unavailable(
            rule,
            reference,
            f"Entry {reference.key!r}: a literal title of at most {_MAX_TITLE_CHARACTERS} "
            "characters is required for the candidate search.",
        )
    results, fetched = await _crossref_reply(client, search_url(title), parse_search_reply)
    if results is None:
        return _unavailable(rule, reference, fetched)
    ranked = []
    for candidate in results:
        if only_types is not None and candidate.work_type not in only_types:
            continue
        if reference.doi and candidate.doi.casefold() == reference.doi.casefold():
            continue
        if candidate.authors is None:
            reason = "Crossref returned a result with unusable author names"
            return _unavailable(rule, reference, _unusable_reply(fetched, reason))
        similarity = difflib.SequenceMatcher(
            None, normalized_text(title), normalized_text(candidate.titles[0])
        ).ratio()
        ranked.append(_Ranked(candidate, round(similarity, 3)))
    ranked.sort(key=lambda item: (-item.similarity, item.candidate.doi))
    return _Search(ranked, fetched)


def _candidates_finding(
    rule: str, reference: Reference, search: _Search, status: Status, text: str
) -> Finding:
    ranked = search.ranked
    outcome = "ambiguous_candidates" if len(ranked) > 1 else "candidate" if ranked else "no_match"
    return _reference_finding(
        rule,
        reference,
        text,
        status=status,
        severity="warning" if ranked else "info",
        evidence=EvidenceKind.HEURISTIC,
        outcome=outcome,
        provider="Crossref",
        source_url=search.fetched.url,
        candidates=[_candidate_row(item) for item in ranked],
        search_limit=SEARCH_ROWS,
        identity_confirmed=False,
    )


def _no_candidate_text(key: str) -> str:
    return (
        f"Entry {key!r}: Crossref returned no candidate in the bounded search; "
        "this does not establish absence."
    )


async def _check_missing_doi(client: OnlineClient, reference: Reference) -> Finding:
    rule = "online.missing_doi"
    search = await _search(client, reference, rule, only_types=None)
    if isinstance(search, Finding):
        return search
    ranked, key = search.ranked, reference.key
    if not ranked:
        return _candidates_finding(rule, reference, search, "passed", _no_candidate_text(key))
    text = (
        f"Entry {key!r} has no DOI; Crossref suggests {_candidate_list(ranked)}. Check each "
        "candidate before adding a DOI; no bibliography was changed."
    )
    status: Status = "inconclusive" if len(ranked) > 1 else "failed"
    return _candidates_finding(rule, reference, search, status, text)


async def _published_candidates(client: OnlineClient, reference: Reference) -> Finding:
    rule = "online.published_version"
    search = await _search(client, reference, rule, only_types=_PUBLISHED_TYPES)
    if isinstance(search, Finding):
        return search
    ranked, key = search.ranked, reference.key
    if not ranked:
        return _candidates_finding(rule, reference, search, "passed", _no_candidate_text(key))
    # A published-version candidate is never more than a lead to review.
    text = (
        f"Entry {key!r} may have a published version: {_candidate_list(ranked)}. Title "
        "similarity does not establish identity, so check each candidate."
    )
    return _candidates_finding(rule, reference, search, "inconclusive", text)


async def _check_published_version(
    client: OnlineClient, reference: Reference, work: CrossrefWork | None, fetched: Fetch
) -> Finding:
    rule = "online.published_version"
    versions = work.published_versions if work else []
    if versions is None:
        reason = "Crossref returned a relation list that could not be read"
        return _unavailable(rule, reference, _unusable_reply(fetched, reason))
    if versions:
        listed = ", ".join(version.doi for version in versions[:_MAX_LISTED_ITEMS])
        return _reference_finding(
            rule,
            reference,
            f"Entry {reference.key!r}: Crossref declares a published version ({listed}). "
            "Review it before changing the citation.",
            status="failed",
            provider="Crossref",
            source_url=fetched.url,
            outcome="declared_relation",
            relations=[
                {
                    "doi": version.doi,
                    "url": doi_url(version.doi),
                    "asserted_by": version.asserted_by,
                }
                for version in versions
            ],
        )
    if reference.doi and work is None:
        return _unavailable(rule, reference, fetched)
    return await _published_candidates(client, reference)


# --- Correction and retraction notices ----------------------------------------------


def _notice_list(reply: NoticeReply) -> str:
    notices = reply.notices
    listed = [f"{item.kind} notice {item.notice_doi}" for item in notices[:_MAX_LISTED_ITEMS]]
    if len(notices) > _MAX_LISTED_ITEMS:
        listed.append(f"and {len(notices) - _MAX_LISTED_ITEMS} more")
    return "; ".join(listed)


async def _check_notices(client: OnlineClient, reference: Reference) -> Finding:
    rule = "online.notices"
    doi = reference.doi
    if not doi:
        return _input_unavailable(
            rule, reference, "A supported literal DOI is required for notice lookup."
        )
    reply, fetched = await _crossref_reply(
        client, notices_url(doi), partial(parse_notice_reply, doi=doi)
    )
    if reply is None:
        return _unavailable(rule, reference, fetched)
    notices, truncated = reply.notices, reply.truncated
    message = (
        f"Entry {reference.key!r}: Crossref reports {_notice_list(reply)}. "
        "Read each notice before citing this work."
        if notices
        else f"Entry {reference.key!r}: no Crossref update notice was returned; "
        "absence outside this provider is unverified."
    )
    return _reference_finding(
        rule,
        reference,
        message,
        status="inconclusive" if truncated else "failed" if notices else "passed",
        severity="warning" if notices or truncated else "info",
        incomplete=truncated,
        provider="Crossref",
        source_url=fetched.url,
        outcome="notices" if notices else "no_notice_found",
        notices=[
            {
                "type": item.kind,
                "notice_doi": item.notice_doi,
                "notice_url": doi_url(item.notice_doi),
                "updated_doi": doi,
            }
            for item in notices
        ],
        truncated=truncated,
        coverage=(
            "Crossref records returned by the updates DOI filter; "
            "not an exhaustive retraction registry"
        ),
    )


# --- Links and the order of checks for one entry --------------------------------------


async def _check_reference_link(client: OnlineClient, reference: Reference) -> Finding:
    value = reference.values["url"]
    if value:
        return await _health(client, reference, "online.reference_link", value)
    return _input_unavailable(
        "online.reference_link",
        reference,
        "Reference URL is ambiguous or nonliteral; no request was made.",
    )


def _is_preprint(reference: Reference) -> bool:
    return (
        reference.kind in _PREPRINT_KINDS
        or (reference.values.get("archiveprefix") or "").casefold() == "arxiv"
        or "arxiv.org/" in (reference.values.get("url") or "").casefold()
        or "preprint" in (reference.values.get("note") or "").casefold()
    )


async def _check_reference(client: OnlineClient, reference: Reference) -> list[Finding]:
    """Run the selected checks for one entry, in a fixed order that spends requests."""
    options, result = client.options, []
    has_doi_field, preprint = "doi" in reference.values, _is_preprint(reference)
    if options.online_doi_resolution and has_doi_field:
        result.append(await _check_doi_resolution(client, reference))
    # One Crossref record serves both the metadata and the published-version checks.
    work, fetched = None, Fetch(FetchOutcome.INPUT_UNAVAILABLE, "")
    if options.online_metadata or (options.online_published_versions and preprint):
        work, fetched = await _crossref_work(client, reference)
    if options.online_metadata and has_doi_field:
        result.append(_check_metadata(reference, work, fetched))
    if options.online_missing_doi and not has_doi_field:
        result.append(await _check_missing_doi(client, reference))
    if options.online_published_versions and preprint:
        result.append(await _check_published_version(client, reference, work, fetched))
    if options.online_notices and has_doi_field:
        result.append(await _check_notices(client, reference))
    if options.online_reference_links and "url" in reference.values:
        result.append(await _check_reference_link(client, reference))
    return result


class _Job(NamedTuple):
    """One unit of work: an entry to check, or a source link to probe."""

    reference: Reference
    is_link: bool


async def _run_job(client: OnlineClient, job: _Job) -> list[Finding]:
    if job.is_link:
        url = str(job.reference.values["url"])
        return [await _health(client, job.reference, "online.replication_link", url)]
    return await _check_reference(client, job.reference)


# --- The run ----------------------------------------------------------------------------


def _offline_findings(enabled: list[str]) -> list[Finding]:
    """One skipped finding per selected check: network permission was not given."""
    return [
        Finding(
            rule,
            "Selected online check was not run: network permission is disabled.",
            severity="info",
            status="skipped",
            suggestion="Use --online to allow the selected requests, or deselect this check.",
            details={"outcome": "offline", "requests": 0},
        )
        for rule in enabled
    ]


def _sharing_finding(enabled: list[str]) -> Finding:
    return Finding(
        "online.sharing",
        "Explicit online checks share DOI identifiers, URL destinations and bibliography titles "
        "with DOI/Crossref services and selected public destination hosts. "
        "No bibliography or manuscript file is uploaded.",
        severity="info",
        status="passed",
        details={
            "checks": enabled,
            "shared_fields": ["DOI", "URL", "bibliographic title"],
            "offline_default": True,
        },
    )


def _coverage_findings(
    enabled: list[str], findings: list[Finding], gaps: list[str]
) -> list[Finding]:
    """Per selected check: say that its input was incomplete, or that it had nothing to check."""
    extra: list[Finding] = []
    for rule in enabled:
        if gaps:
            extra.append(
                Finding(
                    rule,
                    "Online input coverage is incomplete; uninspected inputs were not requested.",
                    severity="warning",
                    status="inconclusive",
                    details={"outcome": "input_coverage_incomplete", "reasons": gaps},
                )
            )
        elif not any(finding.rule == rule for finding in findings):
            extra.append(
                Finding(
                    rule,
                    "No applicable literal input was found for this selected check.",
                    severity="info",
                    status="skipped",
                    details={"outcome": "not_applicable"},
                )
            )
    return extra


def _request_summary(client: OnlineClient) -> Finding:
    return Finding(
        "online.requests",
        "Bounded online requests completed; observations are time-specific.",
        severity="info",
        status="passed",
        details={
            "requests": client.requests,
            "maximum": client.options.online_max_requests,
            "provider": "Crossref/DOI and explicitly selected public URLs",
        },
    )


async def _collect(
    root: Path,
    main: str | None,
    options: OnlineOptions,
    budget: ResourceBudget | None,
    analysed: AnalysedSource | None,
) -> OnlineInputs:
    """Read the entries and links off the event loop, under a share of the job's budget."""
    reservation = (
        budget.lease("collect online reference inputs", memory_mb=_INPUT_COLLECTION_MEMORY_MB)
        if budget
        else contextlib.nullcontext()
    )
    async with reservation:
        return await run_in_thread(collect_inputs, root, main, options, analysed=analysed)


async def _run_checks(
    client: OnlineClient, inputs: OnlineInputs, budget: ResourceBudget | None
) -> list[Finding]:
    options = client.options
    jobs = [_Job(reference, False) for reference in inputs.references]
    jobs += [_Job(link, True) for link in inputs.links]
    limit = min(options.online_jobs, budget.cpu_slots if budget else options.online_jobs)
    collected = await bounded_map(jobs, partial(_run_job, client), limit=limit)
    return [finding for group in collected for finding in group]


_DEFAULT_OPTIONS = OnlineOptions()


async def check_online_references(
    root: Path,
    main: str | None = None,
    *,
    options: OnlineOptions = _DEFAULT_OPTIONS,
    budget: ResourceBudget | None = None,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Opt-in only: share DOI/URL identifiers and titles, never whole source files.

    Entries come from the bibliography resources the selected literal source graph
    declares, so spare copies outside the manuscript spend no request; a directory
    without any document root falls back to every local .bib file. Source links use
    the same selected graph. Transport availability and metadata agreement are
    separate checks. A request failure is advisory/inconclusive, never a fabricated
    metadata match, and the bounded request budget is reported with the findings.

    ``resolver`` and ``transport`` replace name resolution and the network exchange,
    so that tests run the checks without a network. ``analysed`` shares the source analysis
    of the same root and main file with other checks.
    """
    enabled = options.selected_rules
    if not options.online:
        return _offline_findings(enabled)
    if not enabled:
        return []
    inputs = await _collect(root, main, options, budget, analysed)
    client = OnlineClient(options, budget, resolver=resolver, transport=transport)
    findings = [_sharing_finding(enabled), *await _run_checks(client, inputs, budget)]
    findings.extend(_coverage_findings(enabled, findings, inputs.gaps))
    findings.append(_request_summary(client))
    return sorted(
        findings,
        key=lambda finding: (finding.path or "", finding.line or 0, finding.rule, finding.message),
    )
