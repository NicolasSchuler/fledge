"""Which URLs and addresses the online checks may request, and how a URL is shown."""

from __future__ import annotations

import unittest

from latexprep.online_options import NetworkFailure
from latexprep.public_url import parse_public_url, redact_url, require_public_ip


class PublicAddressTests(unittest.TestCase):
    def test_private_local_reserved_and_transition_addresses_are_refused(self):
        for address in (
            "10.0.0.1",
            "192.168.1.1",
            "100.64.0.1",
            "198.18.0.1",
            "224.0.0.1",
            "0.0.0.0",
            "127.0.0.1",
            "::1",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
            "192.0.0.9",
            "192.88.99.2",
            "2001:20::1",
            "3fff::1",
            "5f00::1",
        ):
            with self.subTest(address=address), self.assertRaises(NetworkFailure) as raised:
                require_public_ip(address)
            self.assertEqual(raised.exception.outcome, "unsafe_target")

    def test_public_addresses_are_returned_normalized(self):
        self.assertEqual(require_public_ip("93.184.216.34"), "93.184.216.34")
        self.assertEqual(
            require_public_ip("2606:2800:0220:0001:0248:1893:25c8:1946"),
            "2606:2800:220:1:248:1893:25c8:1946",
        )

    def test_text_that_is_not_an_address_is_refused(self):
        with self.assertRaises(NetworkFailure) as raised:
            require_public_ip("example.org")
        self.assertEqual(str(raised.exception), "DNS returned an invalid IP address")


class ParsePublicUrlTests(unittest.TestCase):
    def test_a_public_url_is_normalized_for_the_request(self):
        target = parse_public_url("HTTPS://Example.ORG./a b?q=1#frag".replace(" ", "%20"))
        self.assertEqual(
            (target.scheme, target.host, target.port, target.path),
            ("https", "example.org", 443, "/a%20b?q=1"),
        )
        self.assertEqual(parse_public_url("http://example.org").path, "/")
        self.assertEqual(parse_public_url("http://example.org").port, 80)

    def test_an_internationalized_host_is_sent_as_ascii(self):
        self.assertEqual(
            parse_public_url("https://bücher.example.org/").host, "xn--bcher-kva.example.org"
        )

    def test_each_refusal_says_why(self):
        reasons = {
            "https://user:secret@example.org/": "Credential-bearing URLs are never requested",
            "https://example.org/?access_token=1": "Credential-bearing query parameters",
            "http://metadata.internal/": "Reserved or local DNS names are forbidden",
            "http://printer.local/": "Reserved or local DNS names are forbidden",
            "https://example.org:8443/": "standard ports",
            "ftp://example.org/": "standard ports",
            "https:///nohost": "standard ports",
            "https://single/": "standard ports",
            "https://exa_mple.org/": "standard ports",
            "http://127.0.0.1/": "Private, local, reserved or multicast",
            "http://[::1]/": "Private, local, reserved or multicast",
            "https://example.org/a\r\nb": "unsupported whitespace, controls or escapes",
            "https://example.org/a\\b": "unsupported whitespace, controls or escapes",
            "https://example.org/" + "a" * 9000: "unsupported whitespace, controls or escapes",
        }
        for url, reason in reasons.items():
            with self.subTest(url=url[:40]), self.assertRaises(NetworkFailure) as raised:
                parse_public_url(url)
            self.assertEqual(raised.exception.outcome, "unsafe_target")
            self.assertIn(reason, str(raised.exception))


class RedactUrlTests(unittest.TestCase):
    def test_credentials_fragment_and_secret_values_are_removed(self):
        redacted = redact_url("https://user:SECRET@example.org:8443/p?id=1&token=SECRET&a=b#frag")
        self.assertEqual(redacted, "https://example.org:8443/p?id=1&token=%5Bredacted%5D&a=b")
        self.assertNotIn("SECRET", redacted)

    def test_a_url_that_cannot_be_parsed_is_not_echoed(self):
        self.assertEqual(redact_url("http://[bad/"), "[invalid or credential-bearing URL]")


if __name__ == "__main__":
    unittest.main()
