from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.parse import quote

from latexprep.redaction import LIMIT_REDACTION, REDACTED, redact_data


class RedactionTests(unittest.TestCase):
    def test_long_plain_tokens_and_marker_runs_remain_unchanged(self) -> None:
        value = {"text": "a" * 12000, "punctuation": "." * 12000, "markers": "?" * 12000}
        self.assertEqual(redact_data(value), value)

    def assert_hidden(self, result: object, *values: str) -> None:
        serialized = json.dumps(result)
        for value in values:
            self.assertNotIn(value, serialized)

    def test_clean_tree_retains_types_values_and_original(self) -> None:
        original = {
            "findings": [{"rule": "pdf.metadata", "details": {"Title": "A useful result"}}],
            "settings": {"scan_secrets": True, "online_max_requests": 20},
            "optional": None,
            "size": 10.5,
            "tuple": ("title", "author"),
            "url": "https://example.org/paper?q=two%20words",
        }
        result = redact_data(original)
        self.assertEqual(result, original)
        self.assertIsNot(result, original)
        self.assertIsNot(result["findings"], original["findings"])
        result["findings"][0]["details"]["Title"] = "Changed copy"
        self.assertEqual(original["findings"][0]["details"]["Title"], "A useful result")

    def test_standalone_signatures_redact_complete_long_values(self) -> None:
        tokens = (
            "AKIA" + "A1" * 8,
            "ghp_" + "B2" * 20,
            "github_pat_" + "C3" * 20,
            "sk-proj-" + "D4" * 200,
            "xoxb-" + "E5" * 20,
        )
        result = redact_data({"log": "\n".join(tokens), "suggestion": tokens[-1]})
        self.assert_hidden(result, *tokens, "D4" * 30)
        self.assertEqual(result["suggestion"], REDACTED)

    def test_sensitive_nested_keys_redact_repeats_across_earlier_fields(self) -> None:
        token = "PrivateCredentialValue7235"
        original = {
            "findings": [
                {
                    "message": "Observed " + token,
                    "details": {"Author": token},
                    "suggestion": "Review " + token,
                }
            ],
            "changes": [{"diff": "+" + token}],
            "settings": {"nested": {"api_key": token, "password": "short-pass"}},
        }
        result = redact_data(original)
        self.assert_hidden(result, token, "short-pass")
        self.assertEqual(result["settings"]["nested"]["api_key"], REDACTED)
        self.assertEqual(original["settings"]["nested"]["api_key"], token)

    def test_secret_assignments_and_bearer_values_include_quoted_spaces(self) -> None:
        result = redact_data(
            {
                "log": "password='correct horse battery'\nAPI_KEY=abcdef123456\n"
                "Authorization: Bearer bearer-value-8675",
                "metadata": "correct horse battery / abcdef123456 / bearer-value-8675",
            }
        )
        self.assert_hidden(result, "correct horse battery", "abcdef123456", "bearer-value-8675")

    def test_escaped_and_unclosed_secret_assignments_do_not_leak(self) -> None:
        secret = "EscapedCredential8765"
        result = redact_data({"log": r"password=\"" + secret + r"\"", "metadata": secret})
        self.assert_hidden(result, secret)
        self.assertEqual(result["metadata"], REDACTED)
        self.assertEqual(redact_data({"log": 'password="unclosed secret'}), LIMIT_REDACTION)

    def test_url_userinfo_and_query_values_are_redacted_and_reused_values_removed(self) -> None:
        password = "Secret/with:punctuation"
        token = "QueryCredential77394"
        result = redact_data(
            {
                "settings": {
                    "urls": [
                        "https://username:"
                        + quote(password, safe="")
                        + "@example.org/paper?access_token="
                        + token
                        + "&page=2"
                    ]
                },
                "message": password + " " + token + " username",
            }
        )
        self.assert_hidden(result, password, quote(password, safe=""), token, "username")
        self.assertIn("example.org", result["settings"]["urls"][0])
        self.assertIn("page=2", result["settings"]["urls"][0])

    def test_encoded_query_keys_and_values_and_encoded_repeat_text(self) -> None:
        token = "Encoded Token / 7235"
        encoded = quote(token, safe="")
        result = redact_data(
            {
                "settings": {"url": "https://example.org/?%74oken=" + encoded},
                "metadata": token,
                "log": encoded,
            }
        )
        self.assert_hidden(result, token, encoded, "7235")
        self.assertEqual(result["metadata"], REDACTED)

    def test_query_context_handles_short_auth_names_without_treating_bib_keys_as_secrets(
        self,
    ) -> None:
        result = redact_data(
            {
                "url": "https://example.org/?key=QueryCredential8945&auth=AuthCredential7845",
                "bibliography": {"key": "citation-key"},
                "log": "QueryCredential8945 AuthCredential7845",
            }
        )
        self.assert_hidden(result, "QueryCredential8945", "AuthCredential7845")
        self.assertEqual(result["bibliography"]["key"], "citation-key")

    def test_short_secret_does_not_replace_letters_inside_structural_mapping_keys(self) -> None:
        result = redact_data(
            {
                "password": "a",
                "findings": [{"message": "a", "path": "a.tex"}],
                "changes": [],
                "main": "main.tex",
            }
        )
        self.assertEqual(set(result), {"password", "findings", "changes", "main"})
        self.assertEqual(set(result["findings"][0]), {"message", "path"})
        self.assertEqual(result["findings"][0]["message"], REDACTED)
        self.assertEqual(redact_data({"token": "message", "message": "value"})[REDACTED], "value")

    def test_malformed_and_schemeless_credential_urls_are_sanitized(self) -> None:
        result = redact_data(
            {
                "urls": [
                    "https://reader:privatepass@[invalid?token=QuerySecret4825",
                    "reader:privatepass@example.org/path",
                    "example.org/?api_key=SecondSecret9284",
                ],
                "metadata": "privatepass QuerySecret4825 SecondSecret9284",
            }
        )
        self.assert_hidden(result, "reader", "privatepass", "QuerySecret4825", "SecondSecret9284")

    def test_private_key_blocks_and_truncated_blocks_hide_body_and_repeats(self) -> None:
        body = "MIIExamplePrivateBody9876543210+/="
        block = "-----BEGIN RSA PRIVATE KEY-----\n" + body + "\n-----END RSA PRIVATE KEY-----"
        result = redact_data({"source": block, "metadata": body})
        self.assertEqual(result["source"], REDACTED)
        self.assert_hidden(result, body, "BEGIN RSA PRIVATE KEY")
        result = redact_data({"diff": "+-----BEGIN PRIVATE KEY-----\n+" + body})
        self.assert_hidden(result, body, "BEGIN PRIVATE KEY")

    def test_sensitive_lists_and_encoded_mapping_keys(self) -> None:
        result = redact_data(
            {
                "nested": {"%61pi_key": ["FirstSecret9825", "SecondSecret9836"]},
                "other": "FirstSecret9825 SecondSecret9836",
            }
        )
        self.assert_hidden(result, "FirstSecret9825", "SecondSecret9836")
        self.assertEqual(result["nested"]["%61pi_key"], [REDACTED, REDACTED])

    def test_bounds_cycles_and_unsupported_values_fail_closed(self) -> None:
        with patch("latexprep.redaction.MAX_TEXT_CHARS", 8):
            self.assertEqual(redact_data({"log": "too much source text"}), LIMIT_REDACTION)
        with patch("latexprep.redaction.MAX_NODES", 2):
            self.assertEqual(redact_data([1, 2, 3]), LIMIT_REDACTION)
        with patch("latexprep.redaction.MAX_DEPTH", 1):
            self.assertEqual(redact_data([["value"]]), LIMIT_REDACTION)
        with patch("latexprep.redaction.MAX_SECRETS", 1):
            self.assertEqual(redact_data({"password": "alpha", "token": "beta"}), LIMIT_REDACTION)
        with patch("latexprep.redaction.MAX_REPLACEMENT_WORK", 1):
            self.assertEqual(redact_data({"password": "alpha"}), LIMIT_REDACTION)
        with patch("latexprep.redaction.MAX_DISCOVERIES", 1):
            self.assertEqual(redact_data("password=repeat\npassword=repeat"), LIMIT_REDACTION)
        deeply_encoded = "%61"
        for _ in range(4):
            deeply_encoded = quote(deeply_encoded, safe="")
        self.assertEqual(redact_data(deeply_encoded), LIMIT_REDACTION)
        cycle = []
        cycle.append(cycle)
        self.assertEqual(redact_data(cycle), LIMIT_REDACTION)

        class UnsafeRepresentation:
            def __repr__(self) -> str:
                raise AssertionError("Unsupported objects must not be stringified")

        self.assertEqual(redact_data(UnsafeRepresentation()), "[REDACTED: unsupported value]")

    def test_repeated_redaction_preserves_the_redacted_copy(self) -> None:
        result = redact_data({"password": "SecretValue87654", "log": "SecretValue87654"})
        self.assertEqual(redact_data(result), result)


if __name__ == "__main__":
    unittest.main()
