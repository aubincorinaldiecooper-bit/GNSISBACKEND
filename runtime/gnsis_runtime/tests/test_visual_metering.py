from __future__ import annotations

import json
import threading

import pytest

from gnsis_runtime.visual.metering import HttpUsageSink, UsageReport


class FakeService:
    def __init__(self) -> None:
        self.reports = []

    def collect_usage(self):
        reports, self.reports = self.reports, []
        return reports


def test_http_usage_sink_retries_identical_event_ids() -> None:
    service = FakeService()
    calls: list[bytes] = []
    failures = [OSError("temporary secret"), None]

    def post(payload, _headers):
        calls.append(payload)
        failure = failures.pop(0)
        if failure:
            raise failure

    sink = HttpUsageSink(
        "http://127.0.0.1:8791/internal/usage/visual",
        "secret",
        post=post,
    )
    sink._service = service
    service.reports = [
        UsageReport(
            workspace_id="workspace",
            virtual_key_id="key",
            project_id=None,
            environment_id=None,
            grant_id="grant",
            session_id="session",
            report_seq=1,
            frames_accepted=1,
            frame_bytes=10,
            decisions=1,
            decisions_act=1,
            decisions_abstain=0,
            attempts_recorded=0,
            inference_ms=1,
            session_ms=0,
            closed=False,
        )
    ]
    HttpUsageSink._flush(sink)
    assert sink.health()["failures"] == 1
    assert sink.health()["last_error"] == "OSError"
    assert "temporary secret" not in repr(sink.health())
    HttpUsageSink._flush(sink)

    assert len(calls) == 2
    assert json.loads(calls[0]) == json.loads(calls[1])
    assert json.loads(calls[0])["reports"][0]["event_id"] == "session:1"
    assert sink.health()["failures"] == 1


def test_http_usage_sink_rejects_non_loopback_plain_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpUsageSink("http://usage.example/internal/usage/visual", "secret")

    HttpUsageSink("http://localhost:8791/internal/usage/visual", "secret")


def _report(index: int) -> UsageReport:
    return UsageReport(
        workspace_id="workspace",
        virtual_key_id="key",
        project_id=None,
        environment_id=None,
        grant_id="grant",
        session_id=f"session-{index}",
        report_seq=1,
        frames_accepted=0,
        frame_bytes=0,
        decisions=0,
        decisions_act=0,
        decisions_abstain=0,
        attempts_recorded=0,
        inference_ms=0,
        session_ms=0,
        closed=False,
    )


def test_http_usage_sink_sends_reports_in_bounded_batches() -> None:
    service = FakeService()
    calls: list[bytes] = []
    sink = HttpUsageSink(
        "http://127.0.0.1:8791/internal/usage/visual",
        "secret",
        post=lambda payload, _headers: calls.append(payload),
    )
    sink._service = service
    service.reports = [_report(index) for index in range(1001)]

    HttpUsageSink._flush(sink)

    assert len(calls) == 3
    counts = [len(json.loads(call)["reports"]) for call in calls]
    assert counts == [500, 500, 1]
    assert sink.health()["pending"] == 0


def test_http_usage_sink_stop_retries_failed_reports() -> None:
    service = FakeService()
    attempts = [0]

    def post(_payload, _headers):
        attempts[0] += 1
        if attempts[0] == 1:
            raise OSError("unreachable")

    sink = HttpUsageSink(
        "http://127.0.0.1:8791/internal/usage/visual",
        "secret",
        post=post,
        interval_s=0.01,
    )
    sink._service = service
    sink._thread = threading.Thread(target=lambda: None)
    sink._thread.start()
    sink._thread.join()
    service.reports = [_report(1)]

    sink.stop()

    assert attempts[0] == 2
    assert sink.health()["pending"] == 0
    assert sink.health()["failures"] == 1
