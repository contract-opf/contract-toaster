#!/usr/bin/env python3
"""
Gate for the BACKEND half of issue #70 ("Run again", prefilled from the stored
review) -- 2026-09-05 diagnostic finding G11, action B11.

## What "the backend half" turned out to be: a contract to HOLD, not to widen

The ticket originally asked for `original_filename` to be added to
`get_review_detail`'s projection. **The owner ruling recorded on issue #70 on
2026-09-13 reverses that**, and this file is the executable form of the ruling:

    "no CSP change, no bucket CORS, no presign, no new backend route, and no
     change to `get_review_detail`'s projection (`original_filename` stays
     unprojected -- #58 deliberately left it that way)."

The ruling chose option (c) of three -- prefill the SETTINGS and ask for the
document again -- over widening `connect-src` plus adding S3/MinIO bucket CORS
(a), and over a new same-origin route that streams the input bytes (b). "Run
again" therefore needs nothing from this route that it did not already have,
and `original_filename` has no consumer here at all.

Keeping it unprojected is a data-classification decision, not a detail.
`reviews._create_review_row` classifies that attribute **Confidential, not
Internal**, in its own words: a contract filename routinely names the
counterparty ("Mutual NDA - <them>.docx" is the ordinary case), which is why
retention clears it on purge alongside the substance fields. Adding it to a
route the SPA reads on every poll would put a counterparty's name on a surface
that has never carried one -- and would do it for a feature that, after the
ruling, does not use the value.

So what is asserted here is the CONTRACT "Run again" actually stands on:

  1. The four settings the prefill restores -- `playbook_id`, `notes_mode`,
     `markup_intensity`, `toaster_guidance` -- plus `has_input`, are all
     returned by `GET /api/reviews/{id}` for the owner. Nothing had to be
     added: every one is already on the projection. A prefill built on a field
     the route does not serve would be silently empty in production.
  2. A review submitted at the defaults projects those settings as `None`
     rather than as back-filled values -- the prefill must be able to tell
     "ran at the default" from "ran at a setting", and both from a row that
     predates the field.
  3. `original_filename` is recorded ON THE ROW and is NOT in the response.
     Both halves matter: the row assertion is what stops this from being a
     vacuous "absent because nothing wrote it" pass, and the response
     assertion is the ruling.
  4. No storage key (`upload_s3_key` / `output_s3_key`) reaches the response
     either -- availability travels as a boolean, which is the same discipline
     and the reason `has_input` exists at all.

Every row here is written by the REAL producer: `POST /api/reviews` through the
real router, real multipart parsing, the real gauntlet. Nothing is hand-seeded,
so a shape asserted here is a shape production can reach.

Offline: in-memory DynamoDB fake + moto S3 via `tests/test_review_api_84.py`'s
fixture; no network, no model.

Run standalone: `.venv/bin/python tests/test_review_detail_prefill_fields_70.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Cross-test-file import, the established convention here (see
# tests/test_review_routes_markup_intensity_54.py): the #84 fixture mounts the
# REAL router with an in-memory DynamoDB fake and moto S3, and its module-level
# env-var setdefaults happen once, on first import. `ReviewApiTestBase` carries
# no `test_*` methods, so importing it adds no tests to this module's suite.
from test_review_api_84 import PLAYBOOK_ID, ReviewApiTestBase, _valid_docx_bytes  # noqa: E402, I001

import src.reviews as reviews  # noqa: E402

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: A filename of the shape the classification note in `_create_review_row`
#: describes -- a counterparty name in the document name. Deliberately a
#: synthetic party: the point is the SHAPE, and this repo's own
#: `tests/lint-counterparty-names.py` exists to keep real ones off the public
#: surface.
UPLOAD_FILENAME = "Mutual NDA - Synthetic Counterparty Ltd.docx"

GUIDANCE = "Cap liability at 12 months of fees; leave the IP clause alone."


class ReviewDetailPrefillFieldsTest(ReviewApiTestBase):
    """The `GET /api/reviews/{id}` contract that "Run again" stands on."""

    def _post(
        self,
        owner: str,
        *,
        notes_mode: str | None = None,
        markup_intensity: str | None = None,
        toaster_guidance: str | None = None,
        body_text: str = "Hello",
    ):
        self._authenticate_as(owner)
        data: dict[str, str] = {"playbook_id": PLAYBOOK_ID}
        if notes_mode is not None:
            data["notes_mode"] = notes_mode
        if markup_intensity is not None:
            data["markup_intensity"] = markup_intensity
        if toaster_guidance is not None:
            data["toaster_guidance"] = toaster_guidance
        return self.client.post(
            "/api/reviews",
            files={"file": (UPLOAD_FILENAME, _valid_docx_bytes(body_text), DOCX_MIME)},
            data=data,
        )

    def _row(self, review_id: str) -> dict:
        return self._reviews_table().get_item(Key={"review_id": review_id}).get("Item") or {}

    def _detail(self, owner: str, review_id: str) -> dict:
        self._authenticate_as(owner)
        resp = self.client.get(f"/api/reviews/{review_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # -- 1. the settings the prefill reads ---------------------------------

    def test_every_setting_run_again_restores_is_projected(self):
        owner = "owner-prefill-settings"
        resp = self._post(
            owner,
            notes_mode="none",
            markup_intensity="heavy",
            toaster_guidance=GUIDANCE,
            body_text="settings document",
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        review_id = resp.json()["review_id"]

        detail = self._detail(owner, review_id)

        # The four the Review tab sets its controls from...
        self.assertEqual(detail["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(detail["notes_mode"], "none")
        self.assertEqual(detail["markup_intensity"], "heavy")
        self.assertEqual(detail["toaster_guidance"], GUIDANCE)
        # ...and the availability pointer, which is a boolean and not a key.
        self.assertIs(detail["has_input"], True)

    def test_defaults_project_as_none_rather_than_back_filled_values(self):
        """"Ran at the default" must stay distinguishable from "ran at a
        setting". The row records neither `notes_mode=external` nor
        `markup_intensity=medium` (both are the defaults and are deliberately
        absent), so the projection answers None -- and `fromMarkupIntensity`
        on the SPA side turns that back into Medium, which is the setting that
        sends no field at all."""
        owner = "owner-prefill-defaults"
        resp = self._post(owner, body_text="default document")
        self.assertEqual(resp.status_code, 202, resp.text)
        review_id = resp.json()["review_id"]

        detail = self._detail(owner, review_id)

        self.assertEqual(detail["playbook_id"], PLAYBOOK_ID)
        self.assertIsNone(detail["notes_mode"])
        self.assertIsNone(detail["markup_intensity"])
        self.assertIsNone(detail["toaster_guidance"])
        self.assertIs(detail["has_input"], True)

    def test_the_defaults_really_are_absent_from_the_row(self):
        """Not vacuous: the None above is a faithful projection of a row that
        carries nothing, not a reader that dropped a value on the floor."""
        owner = "owner-prefill-row-defaults"
        review_id = self._post(owner, body_text="row default document").json()["review_id"]
        row = self._row(review_id)
        self.assertTrue(row, "review row was not written")
        self.assertNotIn("notes_mode", row)
        self.assertNotIn("markup_intensity", row)
        self.assertEqual(reviews.DEFAULT_NOTES_MODE, "external")
        self.assertEqual(reviews.DEFAULT_MARKUP_INTENSITY, "medium")

    # -- 2. the ruling -----------------------------------------------------

    def test_original_filename_is_on_the_row_and_never_on_the_route(self):
        """Owner ruling, issue #70 (2026-09-13): `original_filename` stays
        unprojected. "Run again" restores settings and the reviewer chooses
        the document again, so the field has no consumer here -- and the
        attribute is classified Confidential precisely because a contract
        filename routinely names the counterparty."""
        owner = "owner-prefill-filename"
        review_id = self._post(owner, body_text="filename document").json()["review_id"]

        # The row DOES carry it (issue #518 -- it names the redline on
        # download). Asserting this first is what makes the negative below
        # mean something: the field is absent from the response because the
        # projection omits it, not because nothing ever wrote it.
        self.assertEqual(self._row(review_id).get("original_filename"), UPLOAD_FILENAME)

        detail = self._detail(owner, review_id)
        self.assertNotIn("original_filename", detail)
        # And it is not smuggled in under another name either.
        self.assertNotIn(UPLOAD_FILENAME, str(detail))
        self.assertNotIn("Synthetic Counterparty", str(detail))

    def test_no_storage_key_reaches_the_response(self):
        """Availability is a boolean; storage layout stays server-side. Same
        discipline `has_input` itself exists to serve."""
        owner = "owner-prefill-keys"
        review_id = self._post(owner, body_text="keys document").json()["review_id"]

        row = self._row(review_id)
        self.assertTrue(row.get("upload_s3_key"), "the submit should have recorded an upload key")

        detail = self._detail(owner, review_id)
        self.assertNotIn("upload_s3_key", detail)
        self.assertNotIn("output_s3_key", detail)
        self.assertNotIn(str(row["upload_s3_key"]), str(detail))


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(
            "\nPASS: GET /api/reviews/{id} serves every setting 'Run again' restores, "
            "projects the defaults as None, and keeps original_filename and the storage "
            "keys off the route (issue #70, owner ruling 2026-09-13)."
        )
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
