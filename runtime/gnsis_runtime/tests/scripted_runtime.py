"""The real runtime, serving a scripted model, for end-to-end runs of the
desktop's action path (desktop/scripts/e2e-actions.ts).

Everything between the socket and the model is the production code: host-tool
negotiation, call ids, the coordinator, deadlines, the session timeline. Only
the model is scripted: on the first second of audio it asks for one action
(`--call`), and once the desktop's answer has been fed back to it, it speaks
a sentence built from that answer — so the words that come out prove the
answer reached the same model session.

    python runtime/gnsis_runtime/tests/scripted_runtime.py \\
        --port 18765 --media-dir /tmp/gnsis-e2e \\
        --call '{"name": "files", "arguments": {"action": "move", "path": "report.pdf", "to": "Projects"}}'

Not a test module (pytest collects test_*.py only), and not shipped.
"""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).resolve().parents[2] / "configs" / "gnsis-host-tools.json"


def _event(**fields: Any) -> Any:
    from mcpmft.infer.realtime import DuplexStepEvent

    base = dict(
        index=1,
        is_listen=False,
        text="",
        end_of_turn=False,
        current_time=1,
        audio_waveform=None,
        metrics={},
    )
    base.update(fields)
    return DuplexStepEvent(**base)


class EchoingModel:
    """Asks for one action, then says what the computer answered."""

    def __init__(self, call: dict[str, Any], offered: tuple[str, ...]) -> None:
        self.call = call
        self.offered = offered
        self.stage = "start"
        self.awaiting_response = False
        self.responses: list[Any] = []
        self.closed = False
        self.screen_frames = None
        self._lock = threading.Lock()

    def feed_pcm16(self, _data: bytes, *, unit_capture_start_ms=None) -> tuple:
        with self._lock:
            if self.stage == "start":
                self.stage = "asked"
                if self.call["name"] not in self.offered:
                    # What the model core does with a tool it was not shown.
                    return (
                        _event(
                            is_tool_call=True,
                            tool_error=f"tool {self.call['name']!r} is not available",
                            tool_response_expected=True,
                        ),
                    )
                self.awaiting_response = True
                return (
                    _event(
                        is_tool_call=True,
                        tool_calls=[self.call],
                        tool_generation_complete=True,
                        tool_parse_valid=True,
                        tool_schema_valid=True,
                        tool_response_expected=True,
                    ),
                )
            if self.stage == "asked" and self.responses:
                self.stage = "spoke"
                answer = self.responses[-1]
                said = answer.get("message") if isinstance(answer, dict) else str(answer)
                status = answer.get("status") if isinstance(answer, dict) else "?"
                return (
                    _event(
                        index=2,
                        current_time=2,
                        text=f"[{status}] The computer said: {said}",
                        end_of_turn=True,
                    ),
                )
            return ()

    def feed_tool_response(self, response: Any) -> None:
        with self._lock:
            if not self.awaiting_response:
                raise RuntimeError("there is no pending tool call for this response")
            self.responses.append(response)
            self.awaiting_response = False

    def feed_runtime_event(self, _event: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no worker deliveries in this run")

    def take_native_input_receipt(self, _event: Any) -> None:
        return None

    def set_summary_needed_callback(self, _callback: Any) -> None:
        return None

    def set_task_slate(self, _slate: str) -> bool:
        return False

    def talker_state(self) -> dict[str, Any]:
        return {"generation_id": 0, "drained": True, "turn_ended": True}

    def interrupt_output(self) -> None:
        return None

    def acknowledge_playback(self, output_id: str, *, phase: str, chunks_played: int) -> None:
        return None

    def close(self, *, drain_speech: bool = False) -> None:
        self.closed = True

    def flush_pending(self, *, unit_capture_start_ms=None) -> tuple:
        return ()

    def should_continue_draining(self, _steps: int) -> bool:
        return False

    def should_stop_after(self, _event: Any) -> bool:
        return True

    def wait_for_speech(self) -> None:
        return None

    def poll_output(self, _timeout: float) -> None:
        return None

    def drain_outputs(self) -> tuple:
        return ()

    def enqueue_screen_frame(self, _frame: Any) -> None:
        return None

    def set_media_mode(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Params:
    chunk_ms = 1000
    generate_audio = False
    sliding_window_mode = "context_no_previous"
    context_max_units = 64
    context_previous_max_tokens = 0
    decode_mode = "sampling"
    speak_text_tokens_per_unit = 4


class _Bundle:
    class model:  # noqa: N801
        vpm = object()
        resampler = object()


def _stand_in_asr(text: str) -> str:
    """A speech-to-text upstream that hears `text` in any non-empty audio.

    The runtime's own /api/asr/transcribe route forwards to it exactly as it
    forwards to the real transcriber, so everything from the desktop to the
    route and back is the production path.
    """

    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._answer({"status": "ok"})

        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length") or 0)
            audio = self.rfile.read(size)
            self._answer({"text": text if audio else "", "segments": []})

        def log_message(self, *_args: Any) -> None:
            return None

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--call", required=True, help="the one tool call the model makes, as JSON")
    parser.add_argument("--tool-timeout", type=float, default=60.0)
    parser.add_argument(
        "--asr-text",
        help="serve /api/asr/transcribe from a stand-in speech-to-text that hears this",
    )
    args = parser.parse_args()
    call = json.loads(args.call)
    asr_base_url = _stand_in_asr(args.asr_text) if args.asr_text else None

    import uvicorn

    from gnsis_runtime import online_duplex
    from gnsis_runtime.gateway import GNSISGateway, ProviderRegistry
    from gnsis_runtime.host_tools import load_host_tool_catalog
    from gnsis_runtime.supervision import TaskLedger

    def build_session(_runtime: Any, *, host_tools: tuple[str, ...] = (), **_kwargs: Any) -> EchoingModel:
        return EchoingModel(call, tuple(host_tools))

    online_duplex._build_session = build_session
    online_duplex._prepare_static_prefix = lambda _runtime: None
    app = online_duplex.create_online_duplex_app(
        _Bundle(),
        params=_Params(),
        gateway_factory=lambda _sid: GNSISGateway(
            coordinator=None,
            providers=ProviderRegistry(()),
            ledger=TaskLedger(":memory:"),
            mode="lean",
        ),
        provider_name=None,
        settings=online_duplex.OnlineDuplexSettings(
            host_tool_catalog=load_host_tool_catalog(CATALOG),
            reconnect_grace_sec=5.0,
            external_tool_timeout_sec=args.tool_timeout,
            asr_base_url=asr_base_url,
        ),
        media_dir=args.media_dir,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
