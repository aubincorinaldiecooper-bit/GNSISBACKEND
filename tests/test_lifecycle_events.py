from types import SimpleNamespace

from gnsis.service.executor.events import record_lifecycle_event, safe_payload
from gnsis.service.activity import EventType, _append_persisted_events
from gnsis.service.executor.callbacks import handle_failed
from gnsis.service.executor.failures import classify_failure


class _Store:
    def __init__(self):
        self.calls = []

    def record_event(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return len(self.calls) == 1

    def set_status(self, *args, **kwargs):
        pass

    def revoke_token(self, *args, **kwargs):
        pass


def _run(**overrides):
    values = dict(
        id="er_1", job_id="job_1", workflow_run_attempt=1,
        workflow_run_id=None,
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


def test_lifecycle_event_never_breaks_execution_for_missing_run_evidence():
    store = _Store()

    assert record_lifecycle_event(store, None, "agent_progress", {"message": "started"}) is False
    assert store.calls == []


def test_external_failure_before_oidc_does_not_claim_execution():
    result = classify_failure(_run())
    assert result["stage"] == "executor_authentication"
    assert result["execution_started"] is False
    assert result["model_called"] is False


def test_external_failure_after_source_and_validation_are_distinct():
    execution = classify_failure(_run(status="running", source_downloaded=True))
    assert execution["stage"] == "execution"
    assert execution["execution_started"] is True

    validation = classify_failure(_run(status="validating", patch_sha256="abc"))
    assert validation["stage"] == "output_validation"
    assert validation["execution_started"] is True


def test_persisted_singular_events_are_canonical_and_deduplicated():
    raw = [
        {"kind": "dispatch_started", "created_at": "1", "payload": {"source": "ledger"}},
        {"kind": "dispatch_started", "created_at": "2", "payload": {"source": "duplicate"}},
        {"kind": "workflow_dispatched", "created_at": "3", "payload": {}},
        {"kind": "executor_failure_received", "created_at": "4", "payload": {}},
        {"kind": "run_failed", "created_at": "5", "payload": {}},
        {"kind": "preflight_blocked", "created_at": "6", "payload": {}},
        {"kind": "awaiting_approval", "created_at": "7", "payload": {}},
    ]

    projected = []
    persisted = _append_persisted_events(projected, raw)

    for event_type in (
        EventType.RUN_DISPATCH_STARTED, EventType.WORKFLOW_DISPATCHED,
        EventType.RUN_FAILED, EventType.RUN_BLOCKED, EventType.AWAITING_APPROVAL,
    ):
        assert [event["type"] for event in projected].count(event_type) == 1
        assert event_type in persisted
    assert projected[0]["payload"] == {"source": "ledger"}


def test_failure_classifier_uses_callback_category_and_persisted_evidence():
    source_loading = classify_failure(_run(token_hashed=True))
    assert source_loading["stage"] == "source_loading"
    assert source_loading["execution_started"] is False

    validation = classify_failure(_run(), failure_category="validation_failed")
    assert validation["stage"] == "output_validation"
    assert validation["execution_started"] is True


def test_executor_failure_callback_uses_evidence_based_stage():
    store = _Store()
    job_store = SimpleNamespace(get_job=lambda _job_id: None)
    run = _run(status="authenticated", token_hashed=True)

    result = handle_failed(None, job_store, store, run, {"reason": "stopped"})

    assert result["status"] == "failed"
    payload = store.calls[0][1]["payload"]
    assert payload["stage"] == "source_loading"
    assert payload["execution_started"] is False
