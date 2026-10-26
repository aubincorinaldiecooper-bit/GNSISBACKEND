"""GNSIS WorkerProvider for DeepSeek Harness, driven over ACP.

One `dsh --profile acp` process serves every run of a provider: each run is
its own ACP session in the project workspace. Models come from OpenRouter
through DeepSeek Harness's pi-ai route; `model` is any OpenRouter model id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ..contracts import new_id
from ..coordination import (
    BackendCapabilities,
    DonePayload,
    ProjectRecord,
    UpdatePayload,
    WorkerEvent,
    WorkerMessage,
    WorkerRequest,
)
from ..gateway import WorkerControl
from .registry import ProviderBuildContext, ProviderRegistration


@dataclass(frozen=True)
class DeepSeekHarnessSettings:
    model: str = "deepseek/deepseek-v4-flash"
    command: str = "dsh"
    route: str = "openrouter"
    api_key_env: str = "OPENROUTER_API_KEY"
    startup_timeout_sec: float = 60.0
    run_timeout_sec: float = 900.0


DEEPSEEK_CAPABILITIES = BackendCapabilities(
    steering="none",
    side_queries="none",
    terminal_side_queries="none",
    interactions=False,
    blocking_granularity="run",
    authority_enforcement="gateway",
    structured_events="native",
    trusted_risk_signals=False,
    session_resume=False,
    modalities=frozenset({"text"}),
    max_parallel_projects=4,
    context_provisioning="push_bounded",
    session="stateless",
    worker_tools=frozenset(),
)

# Each run gets a fresh session; consequential steps the agent asks
# permission for are refused, because no person is answering its prompts.
_REFUSED = "refused: background jobs cannot take steps that need permission"


def acp_patch(settings: DeepSeekHarnessSettings) -> str:
    """The `--patch` overlay that points DeepSeek Harness at the model route."""

    return json.dumps(
        [
            {
                "id": "llm-pi-ai",
                "config": {
                    "providers": {
                        settings.route: {"apiKeyEnv": settings.api_key_env}
                    }
                },
            },
            {
                "id": "acp",
                "config": {"provider": settings.route, "model": settings.model},
            },
        ],
        indent=2,
    )


class _Client:
    """ACP client side: routes agent updates to the run that owns the session."""

    def __init__(self) -> None:
        self.runs: dict[str, "DeepSeekRun"] = {}

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        run = self.runs.get(session_id)
        if run is not None:
            await run.on_update(update)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **_: Any
    ) -> Any:
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        run = self.runs.get(session_id)
        if run is not None:
            await run.on_refused(getattr(tool_call, "title", None) or "a step")
        for option in options:
            if str(getattr(option, "kind", "")).startswith("reject"):
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=option.option_id)
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    def on_connect(self, _agent: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Filesystem and terminal requests: DeepSeek Harness uses its own tools.
        async def unsupported(*_: Any, **__: Any) -> Any:
            raise RuntimeError(f"GNSIS does not serve ACP {name}")

        return unsupported


class DeepSeekRun:
    def __init__(
        self,
        request: WorkerRequest,
        provider: "DeepSeekHarnessWorkerProvider",
        workspace: Path,
    ) -> None:
        self.request = request
        self.provider = provider
        self.workspace = workspace
        self.session_id = ""
        self._queue: asyncio.Queue[WorkerEvent | None] = asyncio.Queue()
        self._seq = 0
        self._terminal = False
        self._text: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._drive(), name=f"deepseek-{self.request.run_id}"
        )

    def _event(self, payload: UpdatePayload | DonePayload) -> WorkerEvent:
        self._seq += 1
        return WorkerEvent(
            event_id=new_id("event"),
            owner_id=self.request.owner_id,
            task_id=self.request.task_id,
            project_id=self.request.project_id,
            run_id=self.request.run_id,
            generation=self.request.generation,
            seq=self._seq,
            type="done" if isinstance(payload, DonePayload) else "update",
            payload=payload,
        )

    async def _publish(self, payload: UpdatePayload | DonePayload) -> None:
        if self._terminal:
            return
        if isinstance(payload, DonePayload):
            self._terminal = True
        await self._queue.put(self._event(payload))

    async def on_update(self, update: Any) -> None:
        kind = getattr(update, "session_update", "")
        if kind == "agent_message_chunk":
            text = getattr(getattr(update, "content", None), "text", None)
            if text:
                self._text.append(text)
        elif kind == "tool_call":
            # Text before a tool call is narration; the answer is what follows the last one.
            self._text.clear()
            title = getattr(update, "title", None) or "a tool"
            await self._publish(
                UpdatePayload(kind="activity", summary=f"Using {title}.")
            )

    async def on_refused(self, title: str) -> None:
        await self._publish(
            UpdatePayload(kind="activity", summary=f"{title}: {_REFUSED}.")
        )

    def _prompt(self) -> str:
        pieces = [f"TASK:\n{self.request.instruction}"]
        brief = getattr(self.request.context_plan, "brief", "")
        if brief:
            pieces.extend(["", "GNSIS CONTEXT:", brief])
        source_turn = self.request.source_turn
        text = getattr(source_turn, "final_asr", "") if source_turn else ""
        if text:
            pieces.extend(["", "TRUSTED USER TURN:", text])
        pieces.extend(
            [
                "",
                "Your final reply is spoken to the person in a casual conversation:"
                " keep it to a few plain sentences.",
            ]
        )
        return "\n".join(pieces)

    async def _drive(self) -> None:
        from acp import text_block

        settings = self.provider.settings
        try:
            conn = await self.provider.connection()
            session = await conn.new_session(cwd=str(self.workspace), mcp_servers=[])
            self.session_id = session.session_id
            self.provider.client.runs[self.session_id] = self
            await self._publish(
                UpdatePayload(
                    kind="activity",
                    summary=f"DeepSeek Harness accepted the task ({settings.model}).",
                )
            )
            result = await asyncio.wait_for(
                conn.prompt(
                    session_id=self.session_id, prompt=[text_block(self._prompt())]
                ),
                timeout=settings.run_timeout_sec,
            )
            answer = "".join(self._text).strip()
            stop = getattr(result, "stop_reason", "")
            if stop == "cancelled":
                await self._publish(DonePayload(status="cancelled", result=answer))
            elif stop == "end_turn" and answer:
                await self._publish(DonePayload(status="completed", result=answer))
            else:
                await self._publish(
                    DonePayload(
                        status="partial" if answer else "failed",
                        result=answer or f"DeepSeek Harness stopped: {stop or 'no reply'}",
                    )
                )
        except asyncio.CancelledError:
            await self._publish(DonePayload(status="cancelled", result="Task cancelled."))
            raise
        except Exception as exc:
            await self._publish(
                DonePayload(
                    status="failed",
                    result=f"DeepSeek Harness failure: {type(exc).__name__}: {exc}",
                )
            )
        finally:
            await self._close_session()

    async def _close_session(self) -> None:
        if not self.session_id:
            return
        self.provider.client.runs.pop(self.session_id, None)
        conn = self.provider.live_connection
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close_session(session_id=self.session_id)

    async def events(self) -> AsyncIterator[WorkerEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
            if event.type == "done":
                return

    async def send(self, message: WorkerMessage) -> bool:
        return False

    async def cancel(self, request_id: str) -> bool:
        if self._terminal:
            return True
        conn = self.provider.live_connection
        if self.session_id and conn is not None:
            await conn.cancel(session_id=self.session_id)
            return False
        if self._task is not None:
            self._task.cancel()
        return False

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._queue.put(None)


class DeepSeekProject:
    def __init__(
        self,
        project: ProjectRecord,
        provider: "DeepSeekHarnessWorkerProvider",
    ) -> None:
        self.project = project
        self.provider = provider
        self._runs: set[DeepSeekRun] = set()
        self._closed = False

    async def start(self, request: WorkerRequest, control: WorkerControl) -> DeepSeekRun:
        if self._closed:
            raise RuntimeError("DeepSeek Harness project is closed")
        if request.project_id != self.project.project_id:
            raise ValueError("WorkerRequest project mismatch")
        run = DeepSeekRun(request, self.provider, self.provider.workspace)
        self._runs.add(run)
        await run.start()
        return run

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for run in tuple(self._runs):
            await run.close()
        self._runs.clear()


class DeepSeekHarnessWorkerProvider:
    name = "deepseek-harness"
    capabilities = DEEPSEEK_CAPABILITIES

    def __init__(
        self,
        settings: DeepSeekHarnessSettings,
        *,
        workspace: Path,
        runtime_dir: Path,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.runtime_dir = runtime_dir
        self.client = _Client()
        self.live_connection: Any = None
        self._spawn = spawn
        self._stack = contextlib.AsyncExitStack()
        self._lock = asyncio.Lock()
        self._projects: dict[str, DeepSeekProject] = {}
        self._closed = False

    def _command(self) -> str:
        path = shutil.which(self.settings.command)
        if path is None:
            raise RuntimeError(
                f"DeepSeek Harness is not installed: {self.settings.command!r} not on PATH"
            )
        return path

    async def connection(self) -> Any:
        async with self._lock:
            if self._closed:
                raise RuntimeError("DeepSeek Harness provider is closed")
            if self.live_connection is not None:
                return self.live_connection
            if not os.environ.get(self.settings.api_key_env):
                raise RuntimeError(f"Missing environment variable {self.settings.api_key_env}")
            spawn = self._spawn
            if spawn is None:
                from acp import spawn_agent_process as spawn
            from acp import PROTOCOL_VERSION

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            patch = self.runtime_dir / "deepseek-harness.patch.yml"
            patch.write_text(acp_patch(self.settings) + "\n", encoding="utf-8")
            conn, _process = await self._stack.enter_async_context(
                spawn(
                    self.client,
                    self._command(),
                    "--profile",
                    "acp",
                    "--patch",
                    str(patch),
                    cwd=str(self.workspace),
                    env=dict(os.environ),
                )
            )
            await asyncio.wait_for(
                conn.initialize(protocol_version=PROTOCOL_VERSION),
                timeout=self.settings.startup_timeout_sec,
            )
            self.live_connection = conn
            return conn

    async def warmup(self) -> None:
        await self.connection()

    async def open_project(self, project: ProjectRecord) -> DeepSeekProject:
        if self._closed:
            raise RuntimeError("DeepSeek Harness provider is closed")
        existing = self._projects.get(project.project_id)
        if existing is not None:
            return existing
        created = DeepSeekProject(project, self)
        self._projects[project.project_id] = created
        return created

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for project in tuple(self._projects.values()):
            await project.close()
        self._projects.clear()
        self.live_connection = None
        await self._stack.aclose()


def _build_deepseek_provider(
    context: ProviderBuildContext,
    settings: DeepSeekHarnessSettings,
) -> DeepSeekHarnessWorkerProvider:
    return DeepSeekHarnessWorkerProvider(
        settings, workspace=context.workspace, runtime_dir=context.runtime_dir
    )


DEEPSEEK_PROVIDER_REGISTRATION = ProviderRegistration(
    key="deepseek",
    provider_name="deepseek-harness",
    settings_type=DeepSeekHarnessSettings,
    build=_build_deepseek_provider,
)
