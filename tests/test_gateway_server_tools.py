"""Governed server tools on the executor gateway.

Unit tests for the *pure* server-tool logic and end-to-end tests through the
authenticated ``/internal/model/v1/chat/completions`` route that verify:

* Primary agent calls receive the exact ``openrouter:web_search`` and
  ``openrouter:advisor`` tools; the Advisor is fixed to the run's pinned model
  and has its own nested Web Search.
* Condenser calls do NOT receive any server tools — they are plain model calls.
* A client-supplied ``openrouter:*`` tool is rejected with a structured error.
* The primary's OpenHands function tools survive untouched.
* The user prompt / message content cannot alter the server-tool configuration.
* The Advisor tool's ``forward_transcript`` is false, ``max_tool_calls`` is 4,
  ``max_completion_tokens`` is 4096; the Advisor instructions describe a
  concise senior architect / code reviewer.

Uses deterministic fake OpenRouter responses — no real model call.
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


ALLOWED = "anthropic/claude-opus-4.8"
ALLOWED_ADVISOR = "openai/gpt-5.4"


def _prepare():
    fresh_sqlite_env()
    os.environ["GNSIS_RUN_ALLOWED_MODELS"] = f"{ALLOWED},{ALLOWED_ADVISOR}"
    os.environ["OPENROUTER_API_KEY"] = "sk-test-key"
    os.environ["GNSIS_RUN_MAX_MODEL_CALLS"] = "10"
    os.environ["GNSIS_RUN_MAX_INPUT_TOKENS"] = "500000"
    os.environ["GNSIS_RUN_MAX_OUTPUT_TOKENS"] = "100000"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class ServerToolInjectionUnitTests(unittest.TestCase):
    """Cover the pure-function seams — no HTTP, no store."""

    def setUp(self):
        _prepare()

    def _run(self, primary=ALLOWED, advisor=ALLOWED_ADVISOR):
        from gnsis.service.executor.models import (
            Budgets, ExecutionRunRecord, Usage,
        )
        return ExecutionRunRecord(
            id="exec_x", job_id="job_x", workspace_id=None, repository_id=None,
            provider="github_actions", base_branch="main", base_sha="b" * 40,
            executor_owner="o", executor_repository="r", executor_repository_id=1,
            executor_workflow="execute.yml", executor_ref="main",
            trusted_workflow_sha="0" * 40, workflow_run_id=None, workflow_run_attempt=None,
            workflow_run_url=None, status="pending", nonce_consumed=False,
            token_hashed=True, token_revoked=False, token_expired=False,
            source_downloaded=False, patch_sha256=None, artifact_hashes={},
            budgets=Budgets(10, 500000, 100000, 3.0), usage=Usage(),
            cancellation_requested=False, failure_category=None,
            security_validation=None,
            primary_model=primary, advisor_model=advisor,
        )

    def test_client_openrouter_web_search_is_rejected(self):
        from gnsis.service.executor.gateway import (
            GatewayError, _reject_client_openrouter_tools,
        )
        with self.assertRaises(GatewayError) as cm:
            _reject_client_openrouter_tools([
                {"type": "openrouter:web_search", "engine": "auto"},
            ])
        self.assertEqual(cm.exception.status, 400)

    def test_client_openrouter_advisor_is_rejected(self):
        from gnsis.service.executor.gateway import (
            GatewayError, _reject_client_openrouter_tools,
        )
        with self.assertRaises(GatewayError):
            _reject_client_openrouter_tools([
                {"type": "openrouter:advisor", "model": "attacker/model"},
            ])

    def test_ordinary_function_tools_pass_through(self):
        from gnsis.service.executor.gateway import _reject_client_openrouter_tools
        # OpenHands's function tools carry the coding actions — they must be
        # allowed. The guard reserves only the openrouter: prefix.
        _reject_client_openrouter_tools([
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "str_replace_editor"}},
        ])

    def test_call_purpose_defaults_to_primary(self):
        from gnsis.service.executor.gateway import _resolve_call_purpose
        self.assertEqual(_resolve_call_purpose({}), "primary")
        self.assertEqual(_resolve_call_purpose({"metadata": {}}), "primary")

    def test_call_purpose_condenser_is_recognised(self):
        from gnsis.service.executor.gateway import _resolve_call_purpose
        self.assertEqual(
            _resolve_call_purpose({"metadata": {"call_purpose": "condenser"}}),
            "condenser",
        )

    def test_unknown_purpose_falls_back_to_primary(self):
        from gnsis.service.executor.gateway import _resolve_call_purpose
        # A malicious prompt trying to skip the tools by inventing a purpose
        # doesn't get to pick "no-tools" — the fallback is the safe path.
        self.assertEqual(
            _resolve_call_purpose({"metadata": {"call_purpose": "nefarious"}}),
            "primary",
        )

    def test_primary_call_appends_web_search_and_advisor(self):
        from gnsis.service.executor.gateway import _inject_server_tools
        from gnsis.service.settings import get_settings

        payload: Dict[str, Any] = {
            "tools": [{"type": "function", "function": {"name": "bash"}}],
        }
        _inject_server_tools(payload, self._run(), get_settings())
        types = [t["type"] for t in payload["tools"]]
        self.assertEqual(
            types, ["function", "openrouter:web_search", "openrouter:advisor"]
        )

        # Web Search config matches the sprint decision.
        ws = payload["tools"][1]
        self.assertEqual(ws["engine"], "auto")
        self.assertEqual(ws["max_results"], 5)
        self.assertEqual(ws["max_total_results"], 10)
        self.assertNotIn("search_context_size", ws)

        # Advisor is pinned to the run's advisor_model with the expected config.
        advisor = payload["tools"][2]
        self.assertEqual(advisor["model"], ALLOWED_ADVISOR)
        self.assertEqual(advisor["forward_transcript"], False)
        self.assertEqual(advisor["max_tool_calls"], 4)
        self.assertEqual(advisor["max_completion_tokens"], 4096)
        self.assertIn("senior software architect", advisor["instructions"].lower())
        self.assertIn("code review", advisor["instructions"].lower())
        # Advisor has its OWN nested Web Search with the same config as primary.
        self.assertEqual(len(advisor["tools"]), 1)
        nested = advisor["tools"][0]
        self.assertEqual(nested["type"], "openrouter:web_search")
        self.assertEqual(nested["engine"], "auto")
        self.assertEqual(nested["max_results"], 5)
        self.assertEqual(nested["max_total_results"], 10)

    def test_primary_call_preserves_client_function_tools_untouched(self):
        from gnsis.service.executor.gateway import _inject_server_tools
        from gnsis.service.settings import get_settings

        client_tools = [
            {"type": "function", "function": {"name": "bash", "parameters": {"cmd": "x"}}},
            {"type": "function", "function": {"name": "str_replace_editor"}},
            {"type": "function", "function": {"name": "task_tracker"}},
        ]
        payload = {"tools": [dict(t) for t in client_tools]}
        _inject_server_tools(payload, self._run(), get_settings())
        # The client tools appear FIRST, byte-identical.
        self.assertEqual(payload["tools"][: len(client_tools)], client_tools)

    def test_advisor_is_never_read_from_the_request_body(self):
        """The primary cannot swap the Advisor by putting one in metadata."""
        from gnsis.service.executor.gateway import _pick_advisor_model
        from gnsis.service.settings import get_settings

        run = self._run(advisor=ALLOWED_ADVISOR)
        picked = _pick_advisor_model(run, get_settings())
        self.assertEqual(picked, ALLOWED_ADVISOR)
        # A "different-advisor" hint in the body has no effect — the function
        # only reads from the pinned run record.

    def test_advisor_not_in_allowlist_is_ignored(self):
        """A stale/removed pinned Advisor falls back rather than escaping the allowlist."""
        from gnsis.service.executor.gateway import _pick_advisor_model
        from gnsis.service.settings import get_settings

        run = self._run(advisor="removed/model", primary=ALLOWED)
        picked = _pick_advisor_model(run, get_settings())
        # Falls back to the primary (which IS in the allowlist).
        self.assertEqual(picked, ALLOWED)

    def test_advisor_omitted_when_run_has_no_advisor_or_primary(self):
        """Historical rows with no models cause the Advisor tool to be skipped."""
        from gnsis.service.executor.gateway import _pick_advisor_model
        from gnsis.service.settings import get_settings

        run = self._run(primary=None, advisor=None)
        self.assertIsNone(_pick_advisor_model(run, get_settings()))


class GatewayEndToEndTests(unittest.TestCase):
    """End-to-end through handle_chat_completion with a fake OpenRouter."""

    def setUp(self):
        _prepare()
        from gnsis.orchestration.models import JobSpec
        from gnsis.service.executor.models import Budgets
        from gnsis.service.executor.store import ExecutionStore
        from gnsis.service.executor.tokens import hash_secret
        from gnsis.service.repository import PostgresJobStore

        self.job = PostgresJobStore().create_job(
            JobSpec(repo="cust/repo", instruction="do it",
                    base_branch="main", engine="gnsis")
        )
        self.exec_store = ExecutionStore()
        self.run = self.exec_store.create_run(
            job_id=self.job.id, workspace_id=None, repository_id=None,
            base_branch="main", base_sha="b" * 40,
            dispatch_nonce_hash=hash_secret("n"),
            executor_owner="o", executor_repository="r", executor_repository_id=1,
            executor_workflow="execute.yml", executor_ref="main",
            trusted_workflow_sha="0" * 40,
            budgets=Budgets(50, 500000, 100000, 3.0),
            primary_model=ALLOWED, advisor_model=ALLOWED_ADVISOR,
        )
        # Advance the run to a token-active state so reserve_model_call passes.
        self.exec_store.mark_dispatched(
            self.run.id, workflow_run_id=1, workflow_run_attempt=1, workflow_run_url="http://x"
        )
        from datetime import datetime, timedelta, timezone
        self.exec_store.bind_token(
            self.run.id, token_hash="h" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        self.run = self.exec_store.get_run(self.run.id)

    def _fake_upstream_captures(self):
        seen: List[Dict[str, Any]] = []

        def fake(_settings, payload):
            seen.append(payload)
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

        return seen, fake

    def _settings(self):
        from gnsis.service.settings import get_settings
        return get_settings()

    def test_primary_call_ships_web_search_and_advisor_to_upstream(self):
        from gnsis.service.executor.gateway import handle_chat_completion

        seen, fake = self._fake_upstream_captures()
        status, _data = handle_chat_completion(
            self._settings(), self.exec_store, self.run,
            body={
                "model": ALLOWED,
                "messages": [{"role": "user", "content": "make it work"}],
                "tools": [{"type": "function", "function": {"name": "bash"}}],
            },
            upstream=fake,
        )
        self.assertEqual(status, 200)
        payload = seen[0]
        types = [t["type"] for t in payload["tools"]]
        self.assertIn("openrouter:web_search", types)
        self.assertIn("openrouter:advisor", types)
        advisor = next(t for t in payload["tools"] if t["type"] == "openrouter:advisor")
        self.assertEqual(advisor["model"], ALLOWED_ADVISOR)

    def test_condenser_call_gets_no_server_tools(self):
        from gnsis.service.executor.gateway import handle_chat_completion

        seen, fake = self._fake_upstream_captures()
        handle_chat_completion(
            self._settings(), self.exec_store, self.run,
            body={
                "model": ALLOWED,
                "messages": [{"role": "system", "content": "compact"}],
                "metadata": {"call_purpose": "condenser"},
            },
            upstream=fake,
        )
        payload = seen[0]
        types = [t.get("type") for t in (payload.get("tools") or [])]
        self.assertNotIn("openrouter:web_search", types)
        self.assertNotIn("openrouter:advisor", types)

    def test_client_openrouter_tool_gets_400(self):
        from gnsis.service.executor.gateway import (
            GatewayError, handle_chat_completion,
        )

        _seen, fake = self._fake_upstream_captures()
        with self.assertRaises(GatewayError) as cm:
            handle_chat_completion(
                self._settings(), self.exec_store, self.run,
                body={
                    "model": ALLOWED,
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "openrouter:advisor", "model": "attacker/model"}],
                },
                upstream=fake,
            )
        self.assertEqual(cm.exception.status, 400)

    def test_run_without_advisor_still_works_and_omits_advisor_tool(self):
        """A historical run with no advisor recorded runs without the Advisor tool."""
        from gnsis.service.executor.gateway import handle_chat_completion
        from gnsis.service.executor.models import Budgets
        from gnsis.orchestration.models import JobSpec
        from gnsis.service.repository import PostgresJobStore
        from gnsis.service.executor.tokens import hash_secret

        # A fresh run pinned with no advisor at all.
        legacy_job = PostgresJobStore().create_job(
            JobSpec(repo="c/r", instruction="i", base_branch="main", engine="gnsis")
        )
        legacy = self.exec_store.create_run(
            job_id=legacy_job.id, workspace_id=None, repository_id=None,
            base_branch="main", base_sha="b" * 40,
            dispatch_nonce_hash=hash_secret("legacy-n"),
            executor_owner="o", executor_repository="r", executor_repository_id=1,
            executor_workflow="execute.yml", executor_ref="main",
            trusted_workflow_sha="0" * 40,
            budgets=Budgets(3, 500000, 100000, 3.0),
            primary_model=None, advisor_model=None,
        )
        self.exec_store.mark_dispatched(
            legacy.id, workflow_run_id=1, workflow_run_attempt=1, workflow_run_url="http://x"
        )
        from datetime import datetime, timedelta, timezone
        self.exec_store.bind_token(
            legacy.id, token_hash="l" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        legacy = self.exec_store.get_run(legacy.id)
        seen, fake = self._fake_upstream_captures()
        status, _ = handle_chat_completion(
            self._settings(), self.exec_store, legacy,
            body={"model": ALLOWED, "messages": [{"role": "user", "content": "hi"}]},
            upstream=fake,
        )
        self.assertEqual(status, 200)
        types = [t["type"] for t in seen[0].get("tools", [])]
        # Web Search is still injected (primary purpose default); Advisor is not.
        self.assertIn("openrouter:web_search", types)
        self.assertNotIn("openrouter:advisor", types)


if __name__ == "__main__":
    unittest.main()
