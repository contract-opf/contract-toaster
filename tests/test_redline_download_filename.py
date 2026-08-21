#!/usr/bin/env python3
"""
Gate for issue #518: the redline downloads under a name that identifies it.

## What was wrong

Every downloaded redline was named `out.docx`. The presign passed only
`Bucket` and `Key`, so the browser derived the name from the S3 key, which is
hardcoded as `outputs/{review_id}/out.docx`.

A lawyer running three reviews in an afternoon ends up with `out.docx`,
`out (1).docx`, `out (2).docx` and nothing tying any of them to the agreement
they came from. The redline is the product's entire deliverable, and it
arrived anonymous.

## The part that needs real care

The filename is COUNTERPARTY-SUPPLIED and it goes into an HTTP response
header. That is a header-injection sink, so most of this file is about what
`content_disposition_for` refuses:

  - CR and LF, which would let an uploader append their own headers
  - quotes, which would escape the quoted-string form
  - path separators and traversal, so the browser cannot be steered
  - non-ASCII, which must be RFC 5987-encoded rather than emitted raw
  - unbounded length

The fallback matters as much as the happy path: a review whose original name
is missing, empty, or entirely stripped by sanitisation must still download
under something a human can tell apart, never an empty `filename=""`.

Offline, pure string logic. No AWS, no network.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import src.download as download  # noqa: E402

REVIEW_ID = "73ea3ba9-fe5f-4ef3-bb5c-edcb28bee7bd"


class TestTheNameIdentifiesTheDocument(unittest.TestCase):
    def test_an_ordinary_upload_keeps_its_name_and_gains_a_redline_marker(self):
        header = download.content_disposition_for("Mutual NDA - Acme.docx", REVIEW_ID)
        self.assertIn("Mutual NDA - Acme-redline.docx", header)
        self.assertTrue(header.startswith("attachment; "))

    def test_the_extension_is_not_duplicated(self):
        header = download.content_disposition_for("Agreement.docx", REVIEW_ID)
        self.assertIn("Agreement-redline.docx", header)
        self.assertNotIn(".docx.docx", header)

    def test_an_upload_with_no_extension_still_gets_one(self):
        header = download.content_disposition_for("Agreement", REVIEW_ID)
        self.assertIn("Agreement-redline.docx", header)


class TestItCannotInjectAHeader(unittest.TestCase):
    """The filename is counterparty-supplied and lands in a response header."""

    def test_crlf_cannot_append_a_header(self):
        header = download.content_disposition_for(
            'a.docx\r\nX-Evil: yes\r\nContent-Type: text/html', REVIEW_ID
        )
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("X-Evil", header)

    def test_a_quote_cannot_escape_the_quoted_string(self):
        header = download.content_disposition_for('a".docx', REVIEW_ID)
        # Exactly two quotes: the ones this function opened and closed.
        self.assertEqual(header.count('"'), 2)

    def test_path_separators_and_traversal_are_stripped(self):
        for hostile in ("../../etc/passwd.docx", "..\\..\\windows\\system32.docx", "/abs/path.docx"):
            header = download.content_disposition_for(hostile, REVIEW_ID)
            self.assertNotIn("/", header.split("filename=")[1])
            self.assertNotIn("\\", header.split("filename=")[1])
            self.assertNotIn("..", header)

    def test_control_characters_are_stripped(self):
        header = download.content_disposition_for("a\x00b\x1fc.docx", REVIEW_ID)
        self.assertTrue(all(ord(ch) >= 0x20 for ch in header))

    def test_non_ascii_is_rfc5987_encoded_and_never_emitted_raw(self):
        header = download.content_disposition_for("Vertrag – Müller.docx", REVIEW_ID)
        # The ASCII `filename=` parameter must stay ASCII-only for any client
        # that ignores `filename*`; the real name rides in the encoded one.
        self.assertTrue(header.isascii())
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("M%C3%BCller", header)

    def test_a_semicolon_cannot_start_a_new_parameter(self):
        header = download.content_disposition_for('a.docx"; evil=1', REVIEW_ID)
        self.assertNotIn("evil=1", header.split("filename*=")[0])

    def test_an_absurd_length_is_capped(self):
        header = download.content_disposition_for("A" * 5000 + ".docx", REVIEW_ID)
        self.assertLess(len(header), 600)
        self.assertIn("-redline.docx", header)


class TestTheFallbackIsAlwaysUsable(unittest.TestCase):
    def test_no_recorded_filename_falls_back_to_the_review_id(self):
        for missing in (None, "", "   "):
            header = download.content_disposition_for(missing, REVIEW_ID)
            self.assertIn(f"redline-{REVIEW_ID[:8]}.docx", header)

    def test_a_name_sanitised_down_to_nothing_falls_back_too(self):
        """A filename made ENTIRELY of stripped characters must not produce an
        empty `filename=""` -- the browser would then invent one, which is the
        anonymous-download bug coming back through the side door."""
        header = download.content_disposition_for("../../..", REVIEW_ID)
        self.assertIn(f"redline-{REVIEW_ID[:8]}.docx", header)

    def test_the_fallback_is_still_a_valid_header(self):
        header = download.content_disposition_for(None, REVIEW_ID)
        self.assertTrue(header.startswith("attachment; filename="))
        self.assertEqual(header.count('"'), 2)


class TestItIsActuallyWiredIntoThePresign(unittest.TestCase):
    """A sanitiser nothing calls leaves every download named out.docx."""

    def test_the_presign_passes_a_content_disposition(self):
        source = (BACKEND_ROOT / "src" / "download.py").read_text()
        self.assertIn("ResponseContentDisposition", source)

    def test_the_original_filename_is_recorded_at_submission(self):
        """It cannot be used at download time unless it was stored at upload
        time -- the row had no such field at all."""
        self.assertIn("original_filename", (BACKEND_ROOT / "src" / "reviews.py").read_text())

    def test_the_filename_is_purged_as_confidential_substance(self):
        """It routinely names the counterparty, so it is Confidential, not
        Internal -- retention has to clear it with the rest of the substance
        fields or the purge leaves the counterparty's name behind."""
        self.assertIn("original_filename", (BACKEND_ROOT / "src" / "retention.py").read_text())
        self.assertIn(
            "original_filename",
            (REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py").read_text(),
        )


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: the redline downloads under a safe, identifying name (issue #518).")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
