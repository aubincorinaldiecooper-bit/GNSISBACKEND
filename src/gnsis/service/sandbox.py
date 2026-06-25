"""Docker sandboxing for the risky part of a job.

The worker executes model-written edits and runs the project's tests. That is
untrusted code. :class:`DockerEngine` runs the real engine inside an **ephemeral,
resource-limited, non-root container** that can only see the job's workspace, so a
hostile or buggy change can't touch the worker, its secrets, or other jobs.

It is itself a :class:`~gnsis.orchestration.engine.PatchEngine`, so it slots into
the existing seam: the pipeline calls ``generate`` exactly as before, and this
class delegates into a container, then **replays the phase events back into the
pipeline's sink** so per-phase checkpointing to Postgres still happens.

Notes
-----
* The container needs network egress to reach the model API (Anthropic), so the
  network is restricted, not severed. Lock egress down further with an allowlist
  proxy if you need to.
* Docker-in-Docker is **not available on Railway's standard runtime.** Use this
  when the worker runs on a Docker-capable host; on Railway the default
  ``none`` sandbox relies on the worker's own ephemeral container for isolation
  (acceptable for your-own-repo dogfooding).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, List, Optional

from ..orchestration.engine import PhaseSink, Workspace
from ..orchestration.models import EngineResult

# Bookkeeping lives in its own mount, NEVER inside the repo worktree (/work) —
# otherwise the instruction/events/result files would be picked up by the inner
# engine's `git add -AN` diff and leak into the patch and the published PR.
_GNSIS_DIR = "/gnsis"  # container mount for bookkeeping
_INSTRUCTION = "instruction.txt"
_EVENTS = "events.jsonl"
_RESULT = "result.json"


class DockerEngine:
    """Runs an inner engine inside an isolated container."""

    name = "docker"

    def __init__(
        self,
        inner_engine: str,
        image: str,
        network: str = "bridge",
        memory: str = "2g",
        cpus: str = "2",
        timeout_seconds: int = 1800,
        pass_env: Optional[List[str]] = None,
    ) -> None:
        self.inner_engine = inner_engine
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.timeout_seconds = timeout_seconds
        self.pass_env = pass_env or ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]

    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        if workspace is None:
            raise ValueError("DockerEngine requires a workspace to mount")

        book = self._book_dir(workspace)
        os.makedirs(book, exist_ok=True)
        self._write(book, _INSTRUCTION, instruction)
        cmd = self._docker_command(workspace, book)
        sink.log(f"running engine in sandbox: {self.image}", level="info")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout_seconds
            )
            # Replay phase events the container recorded, so checkpoints land in
            # Postgres exactly as for an in-process run.
            self._replay_events(book, sink)
            result = self._read_result(book)
        finally:
            self._cleanup(book)

        if result is None:
            raise RuntimeError(
                f"sandbox produced no result (exit {proc.returncode}): "
                f"{proc.stderr.strip()[-2000:]}"
            )
        if not result.get("ok"):
            raise RuntimeError(f"sandbox engine failed: {result.get('error')}")
        return _engine_result_from_dict(result["result"])

    # -- helpers ----------------------------------------------------------
    def _book_dir(self, workspace: Workspace) -> str:
        """Host dir for bookkeeping — a sibling of the worktree, never inside it."""
        return os.path.abspath(workspace.path).rstrip(os.sep) + ".gnsisbox"

    def _docker_command(self, workspace: Workspace, book: str) -> List[str]:
        cmd = [
            "docker", "run", "--rm",
            "--network", self.network,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", "512",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{os.path.abspath(workspace.path)}:/work",
            "-v", f"{os.path.abspath(book)}:{_GNSIS_DIR}",
            "-w", "/work",
        ]
        for var in self.pass_env:
            if os.environ.get(var):
                cmd += ["-e", var]
        # Override the image entrypoint explicitly: Dockerfile.sandbox sets
        # ENTRYPOINT to the runner, and `docker run IMAGE CMD` *appends* CMD to
        # the entrypoint — without this, the runner would be invoked twice and
        # argparse would reject the extra positionals. `--entrypoint python`
        # makes the invocation correct for any gnsis-installed image.
        cmd += [
            "--entrypoint", "python",
            self.image,
            "-m", "gnsis.service.runner",
            "--workspace", "/work",
            "--engine", self.inner_engine,
            "--repo", workspace.repo,
            "--base-branch", workspace.base_branch,
            "--instruction-file", f"{_GNSIS_DIR}/{_INSTRUCTION}",
            "--events", f"{_GNSIS_DIR}/{_EVENTS}",
            "--result", f"{_GNSIS_DIR}/{_RESULT}",
        ]
        return cmd

    def _replay_events(self, book: str, sink: PhaseSink) -> None:
        events_path = os.path.join(book, _EVENTS)
        if not os.path.exists(events_path):
            return
        with open(events_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                kind = event.get("type")
                if kind == "begin_phase":
                    sink.begin_phase(event["phase"])
                elif kind == "checkpoint":
                    sink.checkpoint(event["phase"], event["content"])
                elif kind == "log":
                    sink.log(
                        event["message"],
                        level=event.get("level", "info"),
                        **event.get("data", {}),
                    )

    def _read_result(self, book: str) -> Optional[dict]:
        result_path = os.path.join(book, _RESULT)
        if not os.path.exists(result_path):
            return None
        with open(result_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, path: str, name: str, content: str) -> None:
        with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def _cleanup(self, book: str) -> None:
        shutil.rmtree(book, ignore_errors=True)


def _engine_result_from_dict(data: dict) -> EngineResult:
    return EngineResult(
        plan=data.get("plan", ""),
        patch=data.get("patch", ""),
        tests=data.get("tests", ""),
        summary=data.get("summary", ""),
        files_changed=list(data.get("files_changed", [])),
        success=bool(data.get("success", True)),
        detail=dict(data.get("detail", {})),
    )
