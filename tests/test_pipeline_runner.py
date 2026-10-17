#!/usr/bin/env python3
"""Issue #127 item 2: the composed system blocks are cached per active version.

`tests/test_opf_cache_127.py` pins item 1 -- the validated OPF document is
cached per active playbook version. This pins what rides on that cache: the
POSTURE / BINDING / DIGEST / MANIFEST / GUIDANCE / CONTEXT blocks
`review_knowledge.resolve_knowledge` composes from the document. Each cache
entry carries a `review_knowledge.KnowledgeMemo`, the bundle hands it to
`review_spine.run_review`, and the spine resolves through it:

  1. Two reviews on the same active version validate ONCE and compose ONCE;
     `_assemble_opf_system_blocks` receives the cached blocks on the second.
  2. A hit yields the lineage a miss yields -- including `prompt_omissions`
     -- and replays the same omission warning on stderr. Asserted on both
     the public fixture's shape and the real playbook's `posture: {}` /
     `floor: {}` shape, whose omissions differ.
  3. Activating a different version composes afresh (its own blocks, never
     the previous version's); rolling back hits again without composing.
  4. Any other composition input changing -- here the deployment's entity
     roster, which production resolves per review -- misses rather than
     serving blocks composed for different inputs.
  5. A hit hands the review its own copy of the document, so a mutation in
     one review cannot reach the next through the memo.

Only the object store and the playbook_versions read are faked (the same
fakes as item 1's tests); the real validator, the real runner seam and the
real spine run up to system-block assembly, where the test stops the review.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "backend", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import opf_canonicalize  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
from test_opf_cache_127 import BUCKET, PLAYBOOK_ID, _CountingS3, _doc, _stored  # noqa: E402

REAL_SHAPE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5-real-shape.opf.json"


class _Stop(Exception):
    """Raised once the spine has assembled its system blocks: everything this
    file asserts is decided by then, and nothing after it is under test."""


def _doc_at(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rehashed(doc: dict[str, Any]) -> dict[str, Any]:
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    return doc


class KnowledgeCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        pipeline_runner._clear_opf_cache()
        self.addCleanup(pipeline_runner._clear_opf_cache)
        env = mock.patch.dict(
            os.environ, {"PLAYBOOK_VERSIONS_TABLE": "pv-127", "UPLOADS_BUCKET": BUCKET}
        )
        env.start()
        self.addCleanup(env.stop)
        self.s3 = _CountingS3()
        self.active: dict[str, Any] | None = None
        active = mock.patch.object(
            pipeline_runner.playbook_versions,
            "get_active_version_record",
            side_effect=lambda _pid, _ddb: self.active,
        )
        active.start()
        self.addCleanup(active.stop)

        real_validate = pipeline_runner.playbook_upload._load_opf_from_bytes
        self.validations = 0

        def counting_validate(contents: bytes, *, suffix: str) -> dict[str, Any]:
            self.validations += 1
            return real_validate(contents, suffix=suffix)

        validator = mock.patch.object(
            pipeline_runner.playbook_upload, "_load_opf_from_bytes", side_effect=counting_validate
        )
        validator.start()
        self.addCleanup(validator.stop)

        real_compose = review_knowledge.opf_prompt.compose_opf_system_blocks
        self.compositions = 0

        def counting_compose(*args: Any, **kwargs: Any) -> list[str]:
            self.compositions += 1
            return real_compose(*args, **kwargs)

        composer = mock.patch.object(
            review_knowledge.opf_prompt, "compose_opf_system_blocks", side_effect=counting_compose
        )
        composer.start()
        self.addCleanup(composer.stop)

    def _review(self, entity_roster: tuple[str, ...] = ()) -> dict[str, Any]:
        """One review through the real runner load and the real spine, up to
        system-block assembly. Returns the knowledge the spine resolved, the
        blocks it assembled, the bundle's document and what went to stderr."""
        bundle = pipeline_runner._load_opf_bundle_if_active(PLAYBOOK_ID, object(), self.s3)
        assert bundle is not None
        bundle = pipeline_runner._bundle_with_openrouter_model_ids(bundle)
        seen: dict[str, Any] = {"opf": bundle["opf_bundle_v2"]["opf"]}
        real_assemble = review_spine._assemble_opf_system_blocks

        def capture(knowledge: Any, notes_mode: str = "external") -> list[dict[str, Any]]:
            seen["knowledge"] = knowledge
            seen["blocks"] = real_assemble(knowledge, notes_mode=notes_mode)
            raise _Stop

        stderr = io.StringIO()
        with (
            mock.patch.object(review_spine, "_assemble_opf_system_blocks", side_effect=capture),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(_Stop),
        ):
            review_spine.run_review(b"", bundle, object(), entity_roster=entity_roster)
        seen["stderr"] = stderr.getvalue()
        return seen

    def test_same_active_version_validates_and_composes_once(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        miss = self._review()
        hit = self._review()
        self.assertEqual(self.validations, 1)
        self.assertEqual(self.compositions, 1, "the second review must take the cached blocks")
        self.assertEqual(hit["blocks"], miss["blocks"])
        self.assertEqual(hit["knowledge"].blocks, miss["knowledge"].blocks)

    def test_hit_lineage_and_omissions_are_identical_to_a_miss(self) -> None:
        omission_records: dict[str, Any] = {}
        for name, doc in (("public", _doc()), ("real-shape", _rehashed(_doc_at(REAL_SHAPE)))):
            with self.subTest(fixture=name):
                pipeline_runner._clear_opf_cache()
                self.compositions = 0
                self.active = _stored(self.s3, doc, f"{name}-1.0.0")
                miss = self._review()
                hit = self._review()
                self.assertEqual(self.compositions, 1)
                lineage = miss["knowledge"].lineage_record()
                self.assertTrue(lineage["prompt_omissions"], "fixture must record an omission")
                self.assertEqual(hit["knowledge"].lineage_record(), lineage)
                self.assertTrue(miss["stderr"].strip(), "a miss reports its omissions")
                self.assertEqual(hit["stderr"], miss["stderr"], "a hit replays them")
                # And both equal a memo-less resolution of the same document.
                plain = review_knowledge.resolve_knowledge(
                    bundle_v2={"opf": doc, "overrides": None},
                    policy=None,
                    declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
                    accept_empty_posture=True,
                    entity_roster=(),
                )
                self.assertEqual(plain.lineage_record(), lineage)
                omission_records[name] = lineage["prompt_omissions"]
        self.assertNotEqual(
            omission_records["public"],
            omission_records["real-shape"],
            "the two fixtures must exercise different omission records",
        )

    def test_activation_composes_afresh_and_rollback_hits(self) -> None:
        changed = _doc()
        changed["perspective"]["party"] = "Synthetic Other Party"
        v1 = _stored(self.s3, _doc(), "1.0.0")
        v2 = _stored(self.s3, _rehashed(changed), "2.0.0")

        self.active = v1
        first = self._review()
        self.active = v2
        second = self._review()
        self.assertEqual(self.compositions, 2, "activating a new version must compose afresh")
        self.assertNotEqual(second["knowledge"].blocks, first["knowledge"].blocks)
        self.assertNotEqual(second["knowledge"].content_hash(), first["knowledge"].content_hash())

        self.active = v1
        rolled_back = self._review()
        self.assertEqual(self.compositions, 2, "rolling back to a composed version must hit")
        self.assertEqual(rolled_back["knowledge"].blocks, first["knowledge"].blocks)

    def test_same_content_reactivated_as_a_new_version_still_composes_afresh(self) -> None:
        # identity.version is outside the content hash: same content, new
        # row, new cache entry -- the memo belongs to the entry, not the hash.
        self.active = _stored(self.s3, _doc(), "1.0.0")
        self._review()
        self.active = _stored(self.s3, _doc("1.0.1"), "1.0.1")
        self._review()
        self.assertEqual(self.compositions, 2)

    def test_a_different_entity_roster_misses(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        without = self._review()
        with_roster = self._review(entity_roster=("Synthetic Holdings LLC",))
        self.assertEqual(self.compositions, 2)
        self.assertNotEqual(with_roster["knowledge"].blocks, without["knowledge"].blocks)
        self.assertIn("Synthetic Holdings LLC", "\n".join(with_roster["knowledge"].blocks))
        again = self._review()
        self.assertEqual(self.compositions, 2, "the first roster's blocks are still cached")
        self.assertEqual(again["knowledge"].blocks, without["knowledge"].blocks)

    def test_a_hit_binds_this_reviews_own_document(self) -> None:
        self.active = _stored(self.s3, _doc(), "1.0.0")
        first = self._review()
        self.assertIs(first["knowledge"].opf_doc, first["opf"])
        first["knowledge"].opf_doc["perspective"]["party"] = "MUTATED"
        second = self._review()
        self.assertEqual(self.compositions, 1)
        self.assertIs(second["knowledge"].opf_doc, second["opf"])
        self.assertNotEqual(second["knowledge"].opf_doc["perspective"]["party"], "MUTATED")

    def test_no_memo_without_a_cacheable_document(self) -> None:
        # Bytes that do not hash to their content-addressed key are never
        # cached (item 1), so they carry no memo and compose every review.
        row = _stored(self.s3, _doc(), "1.0.0")
        mismatched = f"playbooks/{PLAYBOOK_ID}/{'0' * 64}.json"
        self.s3.put(mismatched, opf_canonicalize.canonicalize(_doc()).encode("utf-8") + b"\n")
        self.active = {**row, "storage_key": mismatched}
        bundle = pipeline_runner._load_opf_bundle_if_active(PLAYBOOK_ID, object(), self.s3)
        assert bundle is not None
        self.assertIsNone(bundle["opf_bundle_v2"]["knowledge_memo"])
        self._review()
        self._review()
        self.assertEqual(self.compositions, 2)


class KnowledgeMemoKeyTest(unittest.TestCase):
    """The memo key covers every composition input, and refuses to key on a
    document with no verified content hash."""

    BASE: ClassVar[dict[str, Any]] = {
        "opf_doc": {"identity": {"content_hash": "sha256:" + "a" * 64}},
        "overrides": None,
        "policy": None,
        "declared_mode": review_knowledge.MODE_PLAYBOOK_DIGEST,
        "accept_stub_basis": False,
        "accept_empty_posture": True,
        "entity_roster": (),
    }

    def test_every_input_changes_the_key(self) -> None:
        base = review_knowledge.knowledge_memo_key(**self.BASE)
        self.assertIsNotNone(base)
        variants = {
            "opf_doc": {"identity": {"content_hash": "sha256:" + "b" * 64}},
            "overrides": {"posture": {"system_prompt": "x"}},
            "policy": {"rules": []},
            "declared_mode": review_knowledge.MODE_POLICY_ONLY,
            "accept_stub_basis": True,
            "accept_empty_posture": False,
            "entity_roster": ("Synthetic Holdings LLC",),
        }
        for field, value in variants.items():
            with self.subTest(field=field):
                key = review_knowledge.knowledge_memo_key(**{**self.BASE, field: value})
                self.assertIsNotNone(key)
                self.assertNotEqual(key, base)

    def test_no_key_without_a_content_hash_or_for_unserialisable_input(self) -> None:
        self.assertIsNone(review_knowledge.knowledge_memo_key(**{**self.BASE, "opf_doc": {}}))
        self.assertIsNone(
            review_knowledge.knowledge_memo_key(**{**self.BASE, "policy": {"rules": [object()]}})
        )

    def test_memo_is_bounded(self) -> None:
        memo = review_knowledge.KnowledgeMemo(max_entries=2)
        knowledge = review_knowledge.ReviewKnowledge(
            mode=review_knowledge.MODE_PLAYBOOK_DIGEST, opf_doc=None, overrides=None, policy=None
        )
        for key in ("a", "b", "c"):
            memo.put(key, knowledge, {})
        self.assertEqual(len(memo), 2)
        self.assertIsNone(memo.get("a"))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
