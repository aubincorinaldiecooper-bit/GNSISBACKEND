"""Contract + normalization tests for the realtime provider seam."""

from __future__ import annotations

import base64
import json
import urllib.error
from unittest.mock import patch

import pytest

from gnsis_runtime.providers.venus import (
    VenusRealtimeProvider,
    VenusRealtimeSession,
    VenusUnavailable,
    _normalize_step,
)
from gnsis_runtime.providers.thinker import _normalize_output
from gnsis_runtime.realtime_provider import (
    ProviderSessionConfig,
    RealtimeSession,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_urlopen(payloads):
    responses = list(payloads)

    def opener(req, timeout=None):
        return _FakeResponse(responses.pop(0))

    return opener


def test_venus_provider_validates_config():
    with pytest.raises(ValueError):
        VenusRealtimeProvider("not-a-url")
    with pytest.raises(ValueError):
        VenusRealtimeProvider("http://x", timeout_s=0)


@pytest.mark.asyncio
async def test_venus_open_session_and_push():
    provider = VenusRealtimeProvider("http://venus.test")
    opened = {"incarnation": 3}
    accepted = {"accepted": True}
    with patch("urllib.request.urlopen", _mock_urlopen([opened, accepted])):
        session = await provider.open_session(
            ProviderSessionConfig(session_id="s1")
        )
        assert isinstance(session, RealtimeSession)
        assert session.session_id == "s1"
        await session.push_audio(b"\x00\x01" * 160)


def test_venus_unavailable_maps_http_error():
    provider = VenusRealtimeProvider("http://venus.test")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "err", {}, None
        )

    with patch("urllib.request.urlopen", boom), pytest.raises(VenusUnavailable):
        import asyncio

        asyncio.run(provider.health())


def test_normalize_step_audio_event():
    pcm = base64.b64encode(b"\x01\x02" * 240).decode()
    event = _normalize_step(
        {
            "session_id": "s",
            "incarnation": 1,
            "generation_id": "g-9",
            "generation_epoch": 2,
            "step_seq": 5,
            "audio": {"data": pcm},
            "audio_chunk_seq": 5,
            "turn_finished": False,
            "finish_reason": None,
        }
    )
    assert event.kind == "audio"
    assert event.epoch == 2
    assert event.seq == 5
    assert event.correlation_id == "g-9"
    assert event.payload["pcm16"] == b"\x01\x02" * 240


def test_normalize_step_prefill_and_turn_events():
    prefill = _normalize_step({"work_id": "w1", "kv_position": 42})
    assert prefill.kind == "control"
    turn = _normalize_step(
        {"turn_finished": True, "finish_reason": "stop", "total_token_ids": [1, 2]}
    )
    assert turn.kind == "turn"
    assert turn.payload["token_count"] == 2


def test_thinker_normalize_output_variants():
    event = _normalize_output({"kind": "audio", "pcm": b"x"})
    assert event.kind == "audio"
    event = _normalize_output(type("O", (), {"kind": "text", "value": "hi"})())
    assert event.kind == "text"
    event = _normalize_output(type("O", (), {"kind": "weird", "value": 1})())
    assert event.kind == "control"


def test_venus_session_incarnation_travels(monkeypatch):
    provider = VenusRealtimeProvider("http://venus.test")
    session = VenusRealtimeSession(provider, session_id="s1", incarnation=7)
    calls = []

    def fake_request(method, path, body=None, query=""):
        calls.append((method, path, body, query))
        return {"ok": True}

    monkeypatch.setattr(provider, "_request", fake_request)
    import asyncio

    asyncio.run(session.push_audio(b"\x00" * 32, capture_ts_ms=100))
    assert calls[0][1] == "/sessions/s1/audio"
    assert calls[0][2]["session_id"] == "s1"
    assert calls[0][2]["incarnation"] == 7
    assert calls[0][2]["capture_ts_ms"] == 100

    asyncio.run(session.acknowledge_playback("u-1", chunks_played=4))
    assert calls[1][1] == "/sessions/s1/playback_ack"
    assert calls[1][2]["utterance_id"] == "u-1"

    asyncio.run(session.close())
    assert calls[2][0] == "DELETE"
    assert "incarnation=7" in calls[2][3]
