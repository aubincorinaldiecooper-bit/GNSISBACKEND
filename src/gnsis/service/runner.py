"""In-container entrypoint for the Docker sandbox.

When sandboxing is enabled, the worker does **not** run the engine in its own
process. Instead it launches an ephemeral container that runs this module against
the mounted workspace. The engine's phase events are streamed to a JSONL file and
the final result to a JSON file (both inside the mounted workspace) so the host
can replay them into Postgres — preserving per-phase checkpointing even though the
risky work (model-written edits, test execution) happened in isolation.

Run as: ``python -m gnsis.service.runner --workspace /work --engine claude \
    --instruction-file /work/.gnsis-instruction.txt \
    --events /work/.gnsis-events.jsonl --result /work/.gnsis-result.json``
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Any

from ..engines import get_engine
from ..orchestration.engine import PhaseSink, Workspace


class _JsonlSink(PhaseSink):
    """Records phase events to a JSONL file for the host to replay."""

    def __init__(self, path: str) -> None:
        self._path = path
        open(self._path, "w").close()

    def _emit(self, event: dict) -> None:
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def begin_phase(self, phase: str) -> None:
        self._emit({"type": "begin_phase", "phase": phase})

    def checkpoint(self, phase: str, content: Any) -> None:
        self._emit({"type": "checkpoint", "phase": phase, "content": content})

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        self._emit({"type": "log", "message": message, "level": level, "data": data})


def main() -> int:
    parser = argparse.ArgumentParser(description="GNSIS sandbox engine runner")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--engine", default="claude")
    parser.add_argument("--repo", default="")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    with open(args.instruction_file, "r", encoding="utf-8") as handle:
        instruction = handle.read()

    workspace = Workspace(
        path=args.workspace, repo=args.repo, base_branch=args.base_branch
    )
    sink = _JsonlSink(args.events)
    engine = get_engine(args.engine)

    try:
        result = engine.generate(instruction, workspace, sink)
        payload = {"ok": True, "result": dataclasses.asdict(result)}
    except Exception as exc:  # noqa: BLE001 - report failure to the host
        payload = {"ok": False, "error": str(exc)}

    with open(args.result, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
