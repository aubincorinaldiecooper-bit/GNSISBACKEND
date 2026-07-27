from types import SimpleNamespace

from gnsis.service.executor.events import record_lifecycle_event, safe_payload
from gnsis.service.executor.reconcile import classify_external_failure


class _Store:
    def __init__(self):
        self.calls = []

    def record_event(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return len(self.calls) == 1


def _run(**overrides):
    values = dict(
        id="er_1", job_id="job_1", workflow_run_attempt=1,
        status="dispatched", source_downloaded=False, token_hashed=False,
        patch_sha256=None, failure_category=None,
        usage=SimpleNamespace(model_calls=0),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_safe_event_payload_is_redacted_bounded_and_idempotency_is_stable():
    payload = safe_payload({
        "Authorization": "Bearer should-not-survive",
        "nonce": "should-not-survive",
        "message": "Bearer abcdefghijklmnop " + "x" * 5000,
        "technical": {"request_id": "req_1", "api_key": "sk_secretvalue123456"},
    })
    assert "Authorization" not in payload and "nonce" not in payload
    assert "Bearer abc" not in payload["message"]
    assert len(payload["message"]) <= 4000
    assert payload["technical"] == {"request_id": "req_1"}

    store = _Store()
    run = _run()
    record_lifecycle_event(store, run, "agent_progress", payload, identity="same")
    record_lifecycle_event(store, run, "agent_progress", payload, identity="same")
    assert store.calls[0][1]["idempotency_key"] == store.calls[1][1]["idempotency_key"]


def test_external_failure_before_oidc_does_not_claim_execution():
    result = classify_external_failure(_run())
    assert result["stage"] == "executor_authentication"
    assert result["execution_started"] is False
    assert result["model_called"] is False


def test_external_failure_after_source_and_validation_are_distinct():
    execution = classify_external_failure(_run(status="running", source_downloaded=True))
    assert execution["stage"] == "execution"
    assert execution["execution_started"] is True

    validation = classify_external_failure(_run(status="validating", patch_sha256="abc"))
    assert validation["stage"] == "output_validation"
    assert validation["execution_started"] is True
