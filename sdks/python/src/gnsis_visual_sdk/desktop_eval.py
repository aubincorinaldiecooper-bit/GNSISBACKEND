"""Drive the whole Smaller GNSIS service on a live desktop screen.

This is the desktop counterpart of the browser host evaluation: an X11 screen
capture stands in for the desktop host's live screen stream, the same visual
session/task/decision/attempt calls run over the same service, the same planner
seam (`--planner service|openrouter`) decides each bounded action, and the host
side executes it on the real desktop with `xdotool`. Recording one JSON line
per scenario keeps the desktop numbers directly comparable to the browser
planner runs.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab

from .browser_host import PlannerObservation
from .client import VisualClient, VisualSession
from .errors import VisualServiceError
from .model_planner import OpenRouterPlanner
from .planner_eval import Scenario, load_scenarios
from .stream import FrameStream

DESKTOP_ACTIONS = ("click", "type", "scroll", "navigate", "back", "wait", "done")
SCROLL_CLICKS = 10
WAIT_SECONDS = 0.5


def capture_frame(max_edge: int = 1280) -> tuple[Screen, bytes]:
    shot = ImageGrab.grab()
    screen = Screen(shot.width, shot.height)
    ratio = min(1.0, max_edge / max(screen.width, screen.height))
    if ratio < 1.0:
        shot = shot.resize(
            (round(screen.width * ratio), round(screen.height * ratio)),
            Image.BILINEAR,
        )
    buffer = io.BytesIO()
    shot.convert("RGB").save(buffer, format="JPEG", quality=82)
    return screen, buffer.getvalue()


@dataclass(frozen=True, slots=True)
class Screen:
    width: int
    height: int


def _run_xdotool(*args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["xdotool", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"xdotool exited {proc.returncode}"
    return True, proc.stdout.strip()


def execute_action(
    decision: dict[str, Any],
    screen: Screen,
    *,
    browser_url_prefix: str | None,
) -> dict[str, Any]:
    action = str(decision.get("action", ""))
    if action == "click":
        target = decision.get("target") or {}
        x = round(float(target.get("x", 0)) * screen.width)
        y = round(float(target.get("y", 0)) * screen.height)
        ok, out = _run_xdotool("mousemove", str(x), str(y), "click", "1")
        return {"success": ok, "message": out or f"clicked ({x}, {y})"}
    if action == "type":
        target = decision.get("target")
        if isinstance(target, dict):
            x = round(float(target.get("x", 0)) * screen.width)
            y = round(float(target.get("y", 0)) * screen.height)
            ok, out = _run_xdotool("mousemove", str(x), str(y), "click", "1")
            if not ok:
                return {"success": False, "message": out}
        ok, out = _run_xdotool("type", "--delay", "40", str(decision.get("text", "")))
        return {"success": ok, "message": out or "typed"}
    if action == "scroll":
        button = "5" if decision.get("direction") == "down" else "4"
        ok, out = _run_xdotool(
            "mousemove",
            str(screen.width // 2),
            str(screen.height // 2),
            "click",
            "--repeat",
            str(SCROLL_CLICKS),
            button,
        )
        return {
            "success": ok,
            "message": out or f"scrolled {decision.get('direction')}",
        }
    if action == "navigate":
        url = str(decision.get("url", ""))
        if browser_url_prefix and not url.startswith(browser_url_prefix):
            return {
                "success": False,
                "message": f"url outside allowed origin {browser_url_prefix}",
            }
        ok, out = _run_xdotool(
            "key",
            "ctrl+l",
        )
        if not ok:
            return {"success": False, "message": out}
        ok, out = _run_xdotool("type", "--delay", "20", url)
        if not ok:
            return {"success": False, "message": out}
        ok, out = _run_xdotool("key", "Return")
        return {"success": ok, "message": out or f"navigated {url}"}
    if action == "back":
        ok, out = _run_xdotool("key", "alt+Left")
        return {"success": ok, "message": out or "back"}
    if action == "wait":
        time.sleep(WAIT_SECONDS)
        return {"success": True, "message": "waited"}
    if action == "done":
        return {"success": True, "done": True, "message": "Task is complete."}
    return {"success": False, "message": f"unsupported desktop action {action!r}"}


async def run_scenario(
    scenario: Scenario,
    *,
    base_url: str,
    host_token: str,
    planner_name: str,
    model: str | None,
    api_key: str | None,
    browser_url_prefix: str | None,
) -> dict[str, Any]:
    planner = None
    if planner_name == "openrouter":
        if not model or not api_key:
            raise ValueError("the openrouter planner needs a model and an API key")
        planner = OpenRouterPlanner(model, api_key)

    record: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "task": scenario.task,
        "planner": planner_name,
        "model": model,
        "max_steps": scenario.max_steps,
        "allowed_actions": list(scenario.allowed_actions),
        "host": "x11+xdotool",
        "started_at": time.time(),
    }
    started = time.monotonic()

    host = VisualClient(base_url, host_token)
    planner_client: VisualClient | None = None
    stream: FrameStream | None = None
    session: VisualSession | None = None
    trace: list[dict[str, Any]] = []
    try:
        session = await asyncio.to_thread(host.create_session)
        planner_client = VisualClient(base_url, session.planner_token)
        await asyncio.to_thread(
            planner_client.set_task,
            session.session_id,
            scenario.task,
            scenario.allowed_actions,
        )
        stream = await FrameStream.connect(base_url, session)

        for step in range(1, scenario.max_steps + 1):
            screen, image = await asyncio.to_thread(capture_frame)
            await stream.send_frame(
                f"frame-{step}",
                int(time.time() * 1000),
                image,
                encoding="jpeg",
                video_source="screen",
                metadata={
                    "source_kind": "desktop_live_screen",
                    "source_width": screen.width,
                    "source_height": screen.height,
                },
            )
            decide_started = time.monotonic()
            response = await asyncio.to_thread(
                planner_client.decide, session.session_id
            )
            decide_ms = int((time.monotonic() - decide_started) * 1000)
            decision_id = str(response["decision_id"])
            service_decision = response["decision"]

            executed = service_decision
            plan_trace: dict[str, Any] | None = None
            if planner is not None:
                planned = await planner.plan(
                    PlannerObservation(
                        step=step,
                        task=scenario.task,
                        allowed_actions=scenario.allowed_actions,
                        viewport=(screen.width, screen.height),
                        service_decision=dict(service_decision),
                        history=tuple(trace),
                    )
                )
                plan_trace = dict(planned.get("trace") or {})
                executed = {
                    key: value
                    for key, value in planned.items()
                    if key != "trace" and value is not None
                }
            action = str(executed.get("action", ""))
            if action not in scenario.allowed_actions or action not in DESKTOP_ACTIONS:
                raise VisualServiceError(
                    f"planner returned disallowed action {action!r}"
                )

            exec_started = time.monotonic()
            result = await asyncio.to_thread(
                execute_action, executed, screen, browser_url_prefix=browser_url_prefix
            )
            exec_ms = int((time.monotonic() - exec_started) * 1000)
            await asyncio.to_thread(
                host.record_attempt, session.session_id, decision_id
            )
            entry: dict[str, Any] = {
                "step": step,
                "decision_id": decision_id,
                "service_decision": service_decision,
                "executed_decision": executed,
                "service_decide_latency_ms": decide_ms,
                "host_latency_ms": exec_ms,
                "success": result.get("success") is True,
                "done": result.get("done") is True,
                "message": str(result.get("message", "")),
            }
            if plan_trace is not None:
                entry["planner"] = plan_trace
            trace.append(entry)
            record["trace"] = [dict(item) for item in trace]

            if result.get("done") is True:
                record["success"] = True
                record["message"] = str(result.get("message", "Task is complete."))
                record["steps"] = step
                break
        else:
            record["success"] = False
            record["steps"] = scenario.max_steps
            record["message"] = "scenario exceeded the step limit"
    except Exception as exc:
        record["success"] = False
        record["message"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "failed"
        record["trace"] = [dict(item) for item in trace]
    finally:
        if stream is not None:
            await stream.close()
        if session is not None:
            try:
                await asyncio.to_thread(host.close_session, session.session_id)
            except Exception as exc:  # cleanup failure is reported, not hidden
                record["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        host.close()
        if planner_client is not None:
            planner_client.close()
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
    record.setdefault("steps", len(trace))
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
                browser_url_prefix=args.browser_url_prefix,
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
    parser.add_argument(
        "--browser-url-prefix",
        default=None,
        help="when set, navigate actions must stay under this origin",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(run_all(_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
