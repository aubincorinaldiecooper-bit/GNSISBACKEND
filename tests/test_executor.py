"""Core tests for the public-beta GitHub Actions execution path.

Deliberately focused (not exhaustive — deeper coverage is a follow-up): they
prove the security-critical spine works — provider enforcement, cryptographic
OIDC claim validation and its key rejections, single-use nonce + hashed token
binding, server-side completion validation, and gateway budget enforcement.

No network: an RS256 keypair stands in for GitHub's OIDC signer (JWKS served via
an injected fetcher) and SQLite stands in for Postgres.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

from _authkit import fresh_sqlite_env  # noqa: E402

OWNER = "aubincorinaldiecooper-bit"
REPO = "Gnsis-studio-"
AUDIENCE = "https://api.gnsis.studio"
ISSUER = "https://token.actions.githubusercontent.com"
TRUSTED_SHA = "a" * 40
FULL = f"{OWNER}/{REPO}"
WORKFLOW_REF = f"{FULL}/.github/workflows/execute.yml@refs/heads/main"


def _configure_env():
    fresh_sqlite_env()
    os.environ.update(
        {
            "GITHUB_APP_ID": "12345",
            "GITHUB_APP_PRIVATE_KEY": "key",
            "GITHUB_APP_SLUG": "gnsis-studio",
            "OPENROUTER_API_KEY": "sk-test",
            "GITHUB_WEBHOOK_SECRET": "whsec",
            "GNSIS_EXECUTION_PROVIDER": "github_actions",
            "GNSIS_PUBLIC_API_URL": "https://api.gnsis.test",
            "GNSIS_EXECUTOR_OWNER": OWNER,
            "GNSIS_EXECUTOR_REPO": REPO,
            "GNSIS_EXECUTOR_OIDC_AUDIENCE": AUDIENCE,
            "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA": TRUSTED_SHA,
            "GNSIS_RUN_MAX_MODEL_CALLS": "3",
            "GNSIS_RUN_MAX_OUTPUT_TOKENS": "1000",
            "GNSIS_RUN_MAX_COST_USD": "0.10",
        }
    )
    from gnsis.service import settings as settings_mod

    settings_mod._settings = None
    from gnsis.service.db import init_db

    init_db()


class _OidcSigner:
    def __init__(self, kid="oidc-key-1"):
        self.kid = kid
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.priv.public_key()))
        jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}

    def claims(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": f"repo:{FULL}:ref:refs/heads/main",
            "repository": FULL,
            "repository_id": "77",
            "repository_owner": OWNER,
            "repository_visibility": "private",
            "event_name": "workflow_dispatch",
            "ref": "refs/heads/main",
            "ref_type": "branch",
            "workflow_ref": WORKFLOW_REF,
            "job_workflow_sha": TRUSTED_SHA,
            "sha": TRUSTED_SHA,
            "run_id": "999",
            "run_attempt": "1",
            "runner_environment": "github-hosted",
            "iat": now - 5,
            "exp": now + 300,
        }
        claims.update(overrides)
        return claims

    def token(self, **overrides):
        return jwt.encode(self.claims(**overrides), self.priv, algorithm="RS256", headers={"kid": self.kid})


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        _configure_env()
        from gnsis.service.executor.oidc import GithubOidcVerifier
        from gnsis.service.executor import api as exec_api

        self.signer = _OidcSigner()
        exec_api.set_oidc_verifier(
            GithubOidcVerifier.default(audience=AUDIENCE, fetcher=lambda: self.signer.jwks)
        )
        from fastapi.testclient import TestClient
        from gnsis.service.api import app

        self.client = TestClient(app)
        self._make_job_and_run()

    def tearDown(self):
        from gnsis.service.executor import api as exec_api

        exec_api.set_oidc_verifier(None)

    def _make_job_and_run(self, nonce="nonce-abc"):
        from gnsis.orchestration.models import JobSpec
        from gnsis.service.repository import PostgresJobStore
        from gnsis.service.executor.models import Budgets
        from gnsis.service.executor.store import ExecutionStore
        from gnsis.service.executor.tokens import hash_secret

        self.nonce = nonce
        self.job = PostgresJobStore().create_job(
            JobSpec(repo="cust/repo", instruction="do it", base_branch="main", engine="gnsis")
        )
        self.exec_store = ExecutionStore()
        self.run = self.exec_store.create_run(
            job_id=self.job.id, workspace_id=None, repository_id=None,
            base_branch="main", base_sha="b" * 40, dispatch_nonce_hash=hash_secret(nonce),
            executor_owner=OWNER, executor_repository=REPO, executor_repository_id=77,
            executor_workflow="execute.yml", executor_ref="main", trusted_workflow_sha=TRUSTED_SHA,
            budgets=Budgets(3, 500000, 1000, 0.10),
        )
        self.exec_store.mark_dispatched(
            self.run.id, workflow_run_id=999, workflow_run_attempt=1, workflow_run_url="http://x"
        )

    def _exchange(self, token=None, nonce=None):
        return self.client.post(
            "/internal/executor/oidc/exchange",
            json={
                "job_id": self.job.id,
                "dispatch_nonce": nonce or self.nonce,
                "oidc_token": token if token is not None else self.signer.token(),
            },
        )


class OidcExchangeTests(ExecutorTestBase):
    def test_valid_exchange_issues_run_token(self):
        r = self._exchange()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["run_token"].startswith("gnsis_rt_"))
        self.assertIn("model_gateway_url", data)

    def test_nonce_is_single_use(self):
        self.assertEqual(self._exchange().status_code, 200)
        # Second exchange with the same (now consumed) nonce must fail.
        self.assertEqual(self._exchange().status_code, 401)

    def test_wrong_audience_rejected(self):
        r = self._exchange(token=self.signer.token(aud="https://evil.example"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_repository_rejected(self):
        r = self._exchange(token=self.signer.token(repository="attacker/evil"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_workflow_ref_rejected(self):
        r = self._exchange(token=self.signer.token(workflow_ref=f"{FULL}/.github/workflows/evil.yml@refs/heads/main"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_workflow_sha_rejected(self):
        r = self._exchange(token=self.signer.token(job_workflow_sha="f" * 40, sha="f" * 40))
        self.assertEqual(r.status_code, 401)

    def test_self_hosted_runner_rejected(self):
        r = self._exchange(token=self.signer.token(runner_environment="self-hosted"))
        self.assertEqual(r.status_code, 401)

    def test_public_repository_rejected(self):
        r = self._exchange(token=self.signer.token(repository_visibility="public"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_run_id_rejected(self):
        r = self._exchange(token=self.signer.token(run_id="123456"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_nonce_rejected(self):
        r = self._exchange(nonce="not-the-nonce")
        self.assertEqual(r.status_code, 401)

    def test_run_token_is_hashed_not_stored_plaintext(self):
        token = self._exchange().json()["run_token"]
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            row = s.query(orm.ExecutionRun).first()
            self.assertIsNotNone(row.token_hash)
            self.assertNotEqual(row.token_hash, token)  # stored as a hash


class RunTokenAndGatewayTests(ExecutorTestBase):
    def _authed(self):
        token = self._exchange().json()["run_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_spec_requires_valid_token(self):
        r = self.client.get(f"/internal/executor/runs/{self.job.id}/spec")
        self.assertEqual(r.status_code, 401)
        r2 = self.client.get(f"/internal/executor/runs/{self.job.id}/spec", headers=self._authed())
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["base_sha"], "b" * 40)
        self.assertNotIn("OPENROUTER_API_KEY", r2.text)

    def test_gateway_rejects_unlisted_model(self):
        r = self.client.post(
            "/internal/model/v1/chat/completions",
            headers=self._authed(),
            json={"model": "openai/gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(r.status_code, 403)

    def test_gateway_enforces_call_budget_and_records_usage(self):
        headers = self._authed()

        def fake_upstream(settings, payload):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        import gnsis.service.executor.gateway as gwmod

        orig = gwmod._default_upstream
        gwmod._default_upstream = fake_upstream
        try:
            body = {"model": "anthropic/claude-opus-4.8", "messages": [{"role": "user", "content": "hi"}]}
            oks = [self.client.post("/internal/model/v1/chat/completions", headers=headers, json=body).status_code for _ in range(3)]
            self.assertEqual(oks, [200, 200, 200])
            # 4th exceeds the call budget (max 3).
            r = self.client.post("/internal/model/v1/chat/completions", headers=headers, json=body)
            self.assertEqual(r.status_code, 402)
        finally:
            gwmod._default_upstream = orig

    def test_gateway_rejects_per_request_token_over_limit(self):
        r = self.client.post(
            "/internal/model/v1/chat/completions",
            headers=self._authed(),
            json={"model": "anthropic/claude-opus-4.8", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999},
        )
        self.assertEqual(r.status_code, 403)


class CompletionValidationTests(ExecutorTestBase):
    def _authed(self):
        return {"Authorization": f"Bearer {self._exchange().json()['run_token']}"}

    def _complete(self, patch, base_sha=None, headers=None):
        return self.client.post(
            f"/internal/executor/runs/{self.job.id}/complete",
            headers=headers or self._authed(),
            json={
                "run_id": 999, "run_attempt": 1,
                "base_sha": base_sha or ("b" * 40),
                "outputs": {"patch.diff": patch, "tests.json": json.dumps({"passed": 1})},
            },
        )

    def test_workflow_file_patch_rejected(self):
        patch = "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n--- a/.github/workflows/x.yml\n+++ b/.github/workflows/x.yml\n@@ -1 +1 @@\n-a\n+b\n"
        r = self._complete(patch)
        self.assertEqual(r.status_code, 422)

    def test_wrong_base_rejected(self):
        patch = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hi\n"
        r = self._complete(patch, base_sha="c" * 40)
        self.assertEqual(r.status_code, 409)

    def test_malformed_patch_rejected(self):
        r = self._complete("this is not a diff")
        self.assertEqual(r.status_code, 422)

    def test_happy_path_reaches_awaiting_approval(self):
        patch = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new\n"
        headers = self._authed()
        # Inject a real clean base checkout so no clone/network is needed.
        import tempfile, subprocess
        import gnsis.service.executor.basecheckout as bc

        base = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=base)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=base)
        subprocess.run(["git", "config", "user.name", "t"], cwd=base)
        with open(os.path.join(base, "x.txt"), "w") as fh:
            fh.write("old\n")
        subprocess.run(["git", "add", "-A"], cwd=base)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=base)
        orig = bc.materialize_base
        bc.materialize_base = lambda *a, **k: base
        try:
            r = self.client.post(
                f"/internal/executor/runs/{self.job.id}/complete",
                headers=headers,
                json={
                    "run_id": 999, "run_attempt": 1, "base_sha": "b" * 40,
                    "outputs": {"patch.diff": patch},
                },
            )
        finally:
            bc.materialize_base = orig
        self.assertEqual(r.status_code, 200, r.text)
        from gnsis.service.repository import PostgresJobStore

        self.assertEqual(PostgresJobStore().get_job(self.job.id).status, "awaiting_approval")


class ProviderEnforcementTests(unittest.TestCase):
    def test_missing_provider_blocks_job_creation(self):
        fresh_sqlite_env()
        # No execution provider configured.
        for k in ("GNSIS_EXECUTION_PROVIDER", "GNSIS_EXECUTOR_OWNER"):
            os.environ.pop(k, None)
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        s = settings_mod.get_settings()
        self.assertFalse(s.execution_provider_valid)
        self.assertTrue(s.missing_execution_vars())


if __name__ == "__main__":
    unittest.main()

class BeatAndSourceTests(ExecutorTestBase):
    def test_beat_role_does_not_require_api_only_secrets_and_tasks_registered(self):
        from gnsis.service.settings import get_settings
        from gnsis.service.tasks import celery_app
        s = get_settings()
        self.assertNotIn("OPENROUTER_API_KEY", s.missing_production_vars(role="beat"))
        self.assertNotIn("GITHUB_WEBHOOK_SECRET", s.missing_production_vars(role="beat"))
        self.assertIn("gnsis.reconcile_executions", celery_app.tasks)
        self.assertIn("gnsis.observe_customer_ci", celery_app.tasks)
        self.assertIn("observe-customer-ci", celery_app.conf.beat_schedule)

    def test_source_prepare_failure_does_not_claim(self):
        from gnsis.service.executor import source as srcmod
        from gnsis.service.settings import get_settings
        orig = srcmod._customer_installation_id
        srcmod._customer_installation_id = lambda run: None
        try:
            with self.assertRaises(srcmod.SourceError):
                srcmod.prepare_source(get_settings(), self.run, self.job.repo)
            self.assertFalse(self.exec_store.get_run(self.run.id).source_downloaded)
        finally:
            srcmod._customer_installation_id = orig

    def test_source_successful_single_use_prepare_then_claim(self):
        from gnsis.service.executor import source as srcmod
        from gnsis.service.settings import get_settings
        class Resp:
            status = 200
            headers = {"Content-Length": "3"}
            closed = False
            def __init__(self): self.parts = [b"abc", b""]
            def read(self, n): return self.parts.pop(0)
            def close(self): self.closed = True
        orig_inst = srcmod._customer_installation_id
        orig_gh = srcmod.ExecutorGitHub
        class GH:
            def __init__(self, app): pass
            def scoped_installation_token(self, *a, **k): return {"token": "secret-token"}
        srcmod._customer_installation_id = lambda run: 123
        srcmod.ExecutorGitHub = GH
        resp = Resp()
        try:
            prepared = srcmod.prepare_source(get_settings(), self.run, self.job.repo, open_archive=lambda *a: resp)
            self.assertTrue(self.exec_store.claim_source_download(self.run.id))
            self.assertEqual(b"".join(prepared.iter_bytes()), b"abc")
            self.assertTrue(resp.closed)
            self.assertFalse(self.exec_store.claim_source_download(self.run.id))
        finally:
            srcmod._customer_installation_id = orig_inst
            srcmod.ExecutorGitHub = orig_gh

    def test_source_oversized_content_length_does_not_claim_and_closes(self):
        from gnsis.service.executor import source as srcmod
        from gnsis.service.settings import get_settings
        class Resp:
            status = 200
            headers = {"Content-Length": str(10**12)}
            closed = False
            def read(self, n): return b"x"
            def close(self): self.closed = True
        orig_inst = srcmod._customer_installation_id
        orig_gh = srcmod.ExecutorGitHub
        class GH:
            def __init__(self, app): pass
            def scoped_installation_token(self, *a, **k): return {"token": "secret-token"}
        resp = Resp(); srcmod._customer_installation_id = lambda run: 123; srcmod.ExecutorGitHub = GH
        try:
            with self.assertRaises(srcmod.SourceError): srcmod.prepare_source(get_settings(), self.run, self.job.repo, open_archive=lambda *a: resp)
            self.assertTrue(resp.closed)
            self.assertFalse(self.exec_store.get_run(self.run.id).source_downloaded)
        finally:
            srcmod._customer_installation_id = orig_inst; srcmod.ExecutorGitHub = orig_gh

class PublishSafetyUnitTests(unittest.TestCase):
    def test_sanitize_removes_installation_tokens(self):
        from gnsis.service.executor.publish import _sanitize
        text = "https://x-access-token:ghs_SECRET123@github.com/o/r.git token ghs_SECRET123 Bearer ghs_SECRET123"
        clean = _sanitize(text)
        self.assertNotIn("ghs_SECRET123", clean)
        self.assertIn("***", clean)
