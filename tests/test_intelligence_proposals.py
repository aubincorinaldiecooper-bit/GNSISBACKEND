"""Deterministic repository-intelligence proposals.

``propose_intelligence_for_run`` derives 0, 1 or multiple candidate lessons
from a run's own evidence (the agent's outcome summary, captured from
receipt.json) — never from the task instruction, never an LLM call. Same
evidence always yields the same proposals, so a reviewer's decision is
reproducible and auditable.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.service.codememory import MemoryKind  # noqa: E402
from gnsis.service.intelligence_lifecycle import propose_intelligence_for_run  # noqa: E402


def _run(outcome_summary):
    return SimpleNamespace(outcome_summary=outcome_summary)


class ProposeIntelligenceForRunTests(unittest.TestCase):
    def test_no_summary_proposes_zero(self):
        self.assertEqual(propose_intelligence_for_run(_run(None)), [])
        self.assertEqual(propose_intelligence_for_run(_run("")), [])
        self.assertEqual(propose_intelligence_for_run(_run("   ")), [])

    def test_missing_outcome_summary_attribute_proposes_zero(self):
        self.assertEqual(propose_intelligence_for_run(SimpleNamespace()), [])

    def test_generic_short_summary_proposes_zero(self):
        self.assertEqual(propose_intelligence_for_run(_run("Done. Task completed.")), [])

    def test_one_substantive_sentence_proposes_one_item(self):
        items = propose_intelligence_for_run(
            _run("Added input validation to the login handler to reject empty passwords.")
        )
        self.assertEqual(len(items), 1)
        self.assertIn("input validation", items[0].content)
        self.assertTrue(items[0].item_key)

    def test_multiple_sentences_propose_multiple_items(self):
        summary = (
            "Refactored the payment retry logic to keep idempotency keys stable. "
            "Added pytest coverage for the retry-exhaustion path. "
            "Documented the new backoff convention in the service README."
        )
        items = propose_intelligence_for_run(_run(summary))
        self.assertGreaterEqual(len(items), 2)

    def test_deterministic_across_repeated_calls(self):
        summary = (
            "Refactored the payment retry logic to keep idempotency keys stable. "
            "Added pytest coverage for the retry-exhaustion path."
        )
        first = propose_intelligence_for_run(_run(summary))
        second = propose_intelligence_for_run(_run(summary))
        self.assertEqual([i.item_key for i in first], [i.item_key for i in second])
        self.assertEqual([i.content for i in first], [i.content for i in second])
        self.assertEqual([i.kind for i in first], [i.kind for i in second])

    def test_duplicate_sentences_are_deduplicated(self):
        summary = (
            "Added retry backoff with stable idempotency keys. "
            "Added retry backoff with stable idempotency keys."
        )
        items = propose_intelligence_for_run(_run(summary))
        self.assertEqual(len(items), 1)

    def test_proposal_count_is_bounded(self):
        sentences = [
            f"Improved subsystem number {i} to handle edge case number {i} safely."
            for i in range(20)
        ]
        items = propose_intelligence_for_run(_run(" ".join(sentences)))
        self.assertLessEqual(len(items), 5)

    def test_kind_classification_is_keyword_based_and_deterministic(self):
        security = propose_intelligence_for_run(
            _run("Sanitized the search query to prevent SQL injection in the reporting endpoint.")
        )
        self.assertEqual(security[0].kind, MemoryKind.SECURITY_CONSTRAINT)

        testing = propose_intelligence_for_run(
            _run("Added pytest regression coverage for the checkout discount calculation.")
        )
        self.assertEqual(testing[0].kind, MemoryKind.TESTING_CONSTRAINT)

        fallback = propose_intelligence_for_run(
            _run("Renamed the billing summary export to match the new dashboard field names.")
        )
        self.assertEqual(fallback[0].kind, MemoryKind.ACCEPTED_CHANGE)

    def test_short_filler_sentences_within_a_longer_summary_are_skipped(self):
        summary = "Done. Ok. Added structured logging to the checkout webhook handler for easier debugging."
        items = propose_intelligence_for_run(_run(summary))
        self.assertEqual(len(items), 1)
        self.assertIn("structured logging", items[0].content)


if __name__ == "__main__":
    unittest.main()
