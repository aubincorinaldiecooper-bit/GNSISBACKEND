"""Run one scenario set through the visual service with interchangeable planners.

Every planner gets the same host, the same scenarios, the same legal actions,
the same step budget, and the same trace schema, so the only thing that varies
between runs is who chooses the next bounded action. Each scenario produces one
JSON line holding the host trace, timings, planner usage, and outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from .browser_host import (
    BROWSER_ACTIONS,
    BrowserHostConfig,
    BrowserHubConnector,
)
from .model_planner import OpenRouterPlanner


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    task: str
    max_steps: int = 8
    allowed_actions: tuple[str, ...] = BROWSER_ACTIONS

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Scenario:
        actions = raw.get("allowed_actions")
        return cls(
            scenario_id=str(raw["id"]),
            task=str(raw["task"]),
            max_steps=int(raw.get("max_steps", 8)),
            allowed_actions=tuple(actions) if actions else BROWSER_ACTIONS,
        )


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("scenario file must be a non-empty JSON array")
    return tuple(Scenario.from_mapping(item) for item in raw)


async def run_scenario(
    scenario: Scenario,
    *,
    base_url: str,
    host_token: str,
    planner_name: str,
    model: str | None,
    api_key: str | None,
    listen_host: str,
    listen_port: int,
    connect_timeout: float,
) -> dict[str, Any]:
    planner = None
    if planner_name == "openrouter":
        if not model or not api_key:
            raise ValueError("the openrouter planner needs a model and an API key")
        planner = OpenRouterPlanner(model, api_key)

    config = BrowserHostConfig(
        base_url=base_url,
        host_token=host_token,
        task=scenario.task,
        allowed_actions=scenario.allowed_actions,
        max_steps=scenario.max_steps,
        capture_fps=1,
    )
    connector = BrowserHubConnector(config, planner=planner)
    finished: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def handler(socket: ServerConnection) -> None:
        if finished.done():
            await socket.close(code=1013, reason="planner eval is already in use")
            return
        try:
            finished.set_result(await connector.run(socket))
        except Exception as exc:  # recorded as a scenario failure, not a crash
            finished.set_exception(exc)

    record: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "task": scenario.task,
        "planner": planner_name,
        "model": model,
        "max_steps": scenario.max_steps,
        "allowed_actions": list(scenario.allowed_actions),
        "started_at": time.time(),
    }
    started = time.monotonic()
    try:
        async with serve(handler, listen_host, listen_port):
            result = await asyncio.wait_for(finished, timeout=connect_timeout)
        record["success"] = result.success
        record["message"] = result.message
        record["steps"] = result.steps
        record["trace"] = [dict(entry) for entry in result.trace]
    except asyncio.TimeoutError:
        record["success"] = False
        record["message"] = "browser hub did not complete within the timeout"
        record["status"] = "blocked"
    except Exception as exc:
        record["success"] = False
        record["message"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "failed"
    finally:
        record["wall_clock_ms"] = int((time.monotonic() - started) * 1000)
        if planner is not None:
            record["planner_usage"] = {
                "requests": planner.usage.requests,
                "prompt_tokens": planner.usage.prompt_tokens,
                "completion_tokens": planner.usage.completion_tokens,
                "total_latency_ms": planner.usage.total_latency_ms,
                "errors": list(planner.usage.errors),
            }
            planner.close()
    record.setdefault("status", "measured")
    return record


async def run_all(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(Path(args.scenarios))
    api_key = os.environ.get("OPENROUTER_API_KEY")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with out.open("a", encoding="utf-8") as handle:
        for scenario in scenarios:
            record = await run_scenario(
                scenario,
                base_url=args.base_url,
                host_token=args.host_token,
                planner_name=args.planner,
                model=args.model,
                api_key=api_key,
                listen_host=args.listen_host,
                listen_port=args.listen_port,
                connect_timeout=args.timeout,
            )
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            print(
                json.dumps(
                    {
                        "scenario_id": record["scenario_id"],
                        "planner": record["planner"],
                        "model": record.get("model"),
                        "status": record["status"],
                        "success": record["success"],
                        "steps": record.get("steps"),
                        "message": record["message"],
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            if not record["success"]:
                failures += 1
    return 1 if failures == len(scenarios) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--host-token", required=True)
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--planner", choices=("service", "openrouter"), required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8766)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(run_all(_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
