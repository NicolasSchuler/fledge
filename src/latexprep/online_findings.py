"""How the outcome of an online request reads in a report: findings, reasons and next steps.

This module owns the wording of the online checks: the finding about one entry or link,
how a request ended in words (with the reason the client gave), the next step a
researcher can take when a request decided nothing, and the findings that describe the
whole run (what is shared, which checks were skipped, how complete the inputs were and
how many requests were made). It decides no verdict and makes no request; the checks in
``online_checks`` choose the rule, the status and the evidence of each finding.
"""

from __future__ import annotations

from typing import Any

from .findings import EvidenceKind, Finding, Severity, Status
from .online_http_session import Fetch, OnlineClient
from .online_inputs import Reference
from .online_options import FetchOutcome, option_maximum

_MAX_QUOTED_CHARS = 80


# --- Findings about one entry or link ----------------------------------------------


def reference_finding(
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
    """Build a finding about one entry or link, located where it was read.

    Args:
        rule: The rule of the finding.
        reference: The entry or link; its path and line locate the finding.
        message: The message of the finding.
        status: The status of the finding.
        severity: The severity of the finding.
        evidence: The kind of evidence behind the finding.
        incomplete: Whether the evidence is incomplete.
        suggestion: What to do next, or ``None``.
        **details: Further details, after the entry's ``citation_key``.

    Returns:
        The finding.
    """
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


def input_unavailable(rule: str, reference: Reference, message: str) -> Finding:
    """Report an entry whose input cannot be used; no request was made for it."""
    return reference_finding(rule, reference, message, outcome="input_unavailable")


def fetch_details(result: Fetch) -> dict[str, Any]:
    """Describe how a fetch ended, as finding details."""
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
    """Name a fetch outcome in words."""
    return _OUTCOME_LABELS.get(outcome) or outcome.replace("_", " ")


def fetch_summary(subject: str, result: Fetch) -> str:
    """Summarize a fetch: ``subject``, the outcome in words and the reason when one is known.

    Args:
        subject: What was fetched, which starts the summary.
        result: How the fetch ended.

    Returns:
        The summary, with the reason in parentheses.
    """
    text = f"{subject}: {_outcome_label(result.outcome)}"
    return f"{text} ({result.message.rstrip('.')})" if result.message else text


def outcome_suggestion(result: Fetch) -> str | None:
    """Suggest what to do about an outcome that is not a verdict on the link or record itself."""
    if result.outcome is FetchOutcome.UNSAFE_TARGET:
        return (
            f"{result.message or 'This address cannot be requested'}. Correct the address if "
            "it is a typo; otherwise it cannot be checked online."
        )
    return _NEXT_STEPS.get(result.outcome)


# --- Crossref lookups and quoted values -----------------------------------------------


def lookup_unavailable(rule: str, reference: Reference, fetched: Fetch) -> Finding:
    """Report a Crossref lookup that gave no usable reply, with a suggestion.

    Args:
        rule: The rule of the finding.
        reference: The entry that was looked up.
        fetched: The failed fetch.

    Returns:
        An inconclusive warning; no metadata conclusion is drawn.
    """
    suggestion = outcome_suggestion(fetched)
    if fetched.outcome in {FetchOutcome.NOT_FOUND, FetchOutcome.GONE}:
        suggestion = (
            "Check the DOI. Crossref does not hold DOIs registered by other agencies, such as "
            "DataCite (used by Zenodo and arXiv)."
        )
    return reference_finding(
        rule,
        reference,
        f"{fetch_summary('Crossref lookup', fetched)}; no metadata conclusion is available.",
        suggestion=suggestion,
        provider="Crossref",
        **fetch_details(fetched),
    )


def quoted(values: list[str], *, others: bool = False) -> str:
    """Quote values for a message, shortened to ``_MAX_QUOTED_CHARS`` characters.

    Args:
        values: The values, joined with commas.
        others: Whether to add ``and others``, for a truncated author list.

    Returns:
        The quoted text.
    """
    text = ", ".join(values) if len(values) > 1 else (values[0] if values else "")
    if others:
        text += " and others"
    if len(text) > _MAX_QUOTED_CHARS:
        text = text[: _MAX_QUOTED_CHARS - 1] + "…"
    return repr(text)


# --- Findings about the whole run -------------------------------------------------------


def offline_findings(enabled: list[str]) -> list[Finding]:
    """Report each selected check as skipped because network permission was not given."""
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


def sharing_finding(enabled: list[str]) -> Finding:
    """State what the selected checks share with which services."""
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


def coverage_findings(
    enabled: list[str], findings: list[Finding], gaps: list[str]
) -> list[Finding]:
    """Say for each selected check that its input was incomplete, or that it had nothing to check.

    Args:
        enabled: The selected checks.
        findings: The findings of the checks so far.
        gaps: The reasons some inputs were not read.

    Returns:
        One inconclusive finding per check when inputs are missing, else one skipped
        finding per check that produced no finding.
    """
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


def request_summary(client: OnlineClient) -> Finding:
    """Report how many requests were made, against the budget."""
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
