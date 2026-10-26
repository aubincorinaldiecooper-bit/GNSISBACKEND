from __future__ import annotations

import contextlib
import json
import types

import pytest

pytest.importorskip("acp")

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    PermissionOption,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
)

from gnsis_runtime.providers import builtin_provider_registry  # noqa: E402
from gnsis_runtime.providers.deepseek import (  # noqa: E402
    DeepSeekHarnessSettings,
    DeepSeekHarnessWorkerProvider,
    acp_patch,
)


def _chunk(text):
    return AgentMessageChunk(
        session_update="agent_message_chunk",
        content=TextContentBlock(type="text", text=text),
    )


class _Conn:
    def __init__(self, client, script, stop="end_turn"):
        self.client, self.script, self.stop = client, script, stop
        self.calls = []

    async def initialize(self, protocol_version):
        self.calls.append("initialize")

    async def new_session(self, cwd, mcp_servers):
        self.calls.append(("new_session", cwd))
        return types.SimpleNamespace(session_id="s1")

    async def prompt(self, session_id, prompt):
        self.calls.append(("prompt", prompt[0].text))
        for step in self.script:
            await step(self.client, session_id)
        return types.SimpleNamespace(stop_reason=self.stop)

    async def close_session(self, session_id):
        self.calls.append(("close_session", session_id))

    async def cancel(self, session_id):
        self.calls.append(("cancel", session_id))


def _provider(tmp_path, monkeypatch, script, stop="end_turn"):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    seen = {}

    @contextlib.asynccontextmanager
    async def spawn(client, command, *args, cwd, env):
        seen["argv"] = (command, *args)
        conn = _Conn(client, script, stop)
        seen["conn"] = conn
        yield conn, None

    provider = DeepSeekHarnessWorkerProvider(
        DeepSeekHarnessSettings(model="deepseek/deepseek-v4-pro"),
        workspace=tmp_path,
        runtime_dir=tmp_path / "rt",
        spawn=spawn,
    )
    return provider, seen


def _request(instruction="Sum the counts in fruit.csv"):
    return types.SimpleNamespace(
        project_id="p1", owner_id="o", task_id="t", run_id="r", generation=1,
        instruction=instruction,
        context_plan=types.SimpleNamespace(brief=""),
        source_turn=None,
    )


async def _run(provider):
    project = await provider.open_project(types.SimpleNamespace(project_id="p1"))
    run = await project.start(_request(), None)
    events = [event async for event in run.events()]
    await provider.close()
    return events


def test_registered_and_patch_routes_openrouter():
    factory = builtin_provider_registry().configure("deepseek", {"model": "x/y"})
    assert factory.provider_name == "deepseek-harness"
    patch = json.loads(acp_patch(factory.settings))
    assert patch[0]["config"]["providers"] == {"openrouter": {"apiKeyEnv": "OPENROUTER_API_KEY"}}
    assert patch[1]["config"] == {"provider": "openrouter", "model": "x/y"}


@pytest.mark.asyncio
async def test_answer_is_text_after_the_last_tool_call(tmp_path, monkeypatch):
    async def narrate(client, sid):
        await client.session_update(sid, _chunk("Let me read the file."))

    async def tool(client, sid):
        await client.session_update(
            sid, ToolCallStart(session_update="tool_call", tool_call_id="c1", title="read")
        )

    async def answer(client, sid):
        await client.session_update(sid, _chunk("The total is 8."))

    provider, seen = _provider(tmp_path, monkeypatch, [narrate, tool, answer])
    events = await _run(provider)

    assert seen["argv"][1:4] == ("--profile", "acp", "--patch")
    assert [e.payload.summary for e in events if e.type == "update"][-1] == "Using read."
    assert events[-1].type == "done"
    assert events[-1].payload.status == "completed"
    assert events[-1].payload.result == "The total is 8."
    assert ("close_session", "s1") in seen["conn"].calls


@pytest.mark.asyncio
async def test_permission_requests_are_refused(tmp_path, monkeypatch):
    outcomes = []

    async def ask(client, sid):
        outcomes.append(
            await client.request_permission(
                sid,
                ToolCallUpdate(tool_call_id="c1", title="Run rm -rf build"),
                [
                    PermissionOption(option_id="yes", name="Allow", kind="allow_once"),
                    PermissionOption(option_id="no", name="Reject", kind="reject_once"),
                ],
            )
        )
        await client.session_update(sid, _chunk("I couldn't run that."))

    provider, _ = _provider(tmp_path, monkeypatch, [ask])
    events = await _run(provider)

    assert outcomes[0].outcome.option_id == "no"
    assert any("refused" in e.payload.summary for e in events if e.type == "update")


@pytest.mark.asyncio
async def test_missing_key_fails_the_run_not_silently(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch, [])
    monkeypatch.delenv("OPENROUTER_API_KEY")
    events = await _run(provider)
    assert events[-1].payload.status == "failed"
    assert "OPENROUTER_API_KEY" in events[-1].payload.result
