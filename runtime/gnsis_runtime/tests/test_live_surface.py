"""The /live phone surface, and what a server needs in order to boot.

A note on what half of these prove. The config tests run the real
`preflight_config` and `load_config`, so they hold actual behaviour. The client
tests read `live.js` as text and assert on what is in it, because this suite
has no browser: they will catch the protocol mistakes coming back, which is
what they are for, but they cannot tell you the page works. Only driving it in
a browser does that, and a real device does it properly.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from starlette.testclient import TestClient

from starlette.websockets import WebSocketDisconnect

from test_duplex_lifecycle import _drain_until, _jpeg, _settle, _stop, connect
from test_live_camera_privacy import _camera_header


def test_live_page_is_served(harness):
    h = harness()
    with TestClient(h.app) as client:
        page = client.get("/live")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        body = page.text
        # The statement, in the guideline's words.
        assert "Let it see what you see." in body
        # The page must not promise a working session before the server has
        # confirmed a frame: "Session ready" lives in the script, not the page.
        assert "Session ready" not in body


def test_both_doors_are_in_the_page(harness):
    """A QR is useless on the device you would scan it with.

    Which one is shown is a CSS decision about pointer type, so both have to be
    present in the markup; the test that they are is the only part a server
    test can hold.
    """

    h = harness()
    with TestClient(h.app) as client:
        body = client.get("/live").text
    assert "Scan with your phone" in body
    assert 'id="start"' in body
    # And neither door is locked: a desktop with a webcam can still opt in.
    assert 'id="useThis"' in body


def test_permission_is_asked_in_our_own_words_first(harness):
    """The sheet exists so the native prompt is never the first thing seen."""

    h = harness()
    with TestClient(h.app) as client:
        body = client.get("/live").text
        source = client.get("/assets/live.js").text
    assert 'id="permission"' in body
    assert "Allow camera and microphone" in body
    # Refusing must stay cheap, which means a way out that is not the browser's.
    assert 'id="cancel"' in body
    # Start must not reach for the camera itself: the native prompt belongs to
    # Allow, or the sheet is decoration over a prompt that already fired.
    start_handler = source.split("ui.start.addEventListener")[1].split("\n")[0]
    assert "ask" in start_handler
    assert "getUserMedia" not in start_handler


def test_the_view_is_blurred_while_permission_is_still_being_asked(harness):
    """Nothing behind the question stays sharp until the question is answered.

    Asserted on the stylesheet rather than on a render, because this suite has
    no browser. What it can hold is the part that silently rots: the plain
    property alone does nothing in Safari, which is most of the phones this
    page is for, so the prefix is the half that has to be here.
    """

    h = harness()
    with TestClient(h.app) as client:
        sheet = client.get("/assets/live.css").text.split(".sheet {")[1].split("}")[0]
    # Matched per declaration, not as a substring of the block: the prefixed
    # property contains the plain one, so `"backdrop-filter: blur(" in sheet`
    # passes with the plain property deleted. It did.
    declared = {
        line.strip().split(":")[0].strip()
        for line in sheet.splitlines()
        if ":" in line and not line.strip().startswith(("/*", "*"))
    }
    assert "backdrop-filter" in declared
    assert "-webkit-backdrop-filter" in declared


def test_the_headline_arrives_word_by_word_and_is_still_a_sentence(harness):
    """Spell UI's WordsStagger, ported: each word from faded, 10px low and
    blurred to rest over 0.5s, 0.1s apart. The values are the upstream ones.

    Text assertions, because this suite has no browser. What they hold is the
    part that would rot quietly: the sentence must stay in the markup (a page
    whose script never ran still says it), the words must be separated by real
    spaces rather than glued into flex items (so it reads and wraps as one
    sentence), and reduced motion must switch the whole thing off.
    """

    h = harness()
    with TestClient(h.app) as client:
        body = client.get("/live").text
        css = client.get("/assets/live.css").text
        source = client.get("/assets/live.js").text
    # The words live in the markup, not the script.
    assert "Let it see what you see." in body
    assert "Let it see what you see." not in source
    # Upstream's recipe, verbatim.
    keyframes = css.split("@keyframes word-in")[1].split("}\n}")[0]
    assert "opacity: 0" in keyframes
    assert "translateY(10px)" in keyframes
    assert "blur(10px)" in keyframes
    rule = css.split(".headline.stagger .word {")[1].split("}")[0]
    assert "0.5s ease-out" in rule
    assert "--word-stagger: 0.1s" in rule
    # A real space between words, so the sentence stays one sentence.
    assert "heading.append(' ')" in source
    # Reduced motion turns it off rather than merely speeding it up.
    # Up to the block's own closing brace, not the first rule's.
    reduced = css.split("prefers-reduced-motion: reduce")[1].split("\n}")[0]
    assert ".headline.stagger .word { animation: none; }" in reduced


def test_the_qr_encodes_this_server_not_a_caller_supplied_url(harness):
    """A QR generator that draws any URL you hand it is a phishing tool.

    Served from your domain, pointing wherever the requester liked. The address
    is derived from the request instead, so the query string cannot steer it.
    """

    h = harness()
    with TestClient(h.app) as client:
        hijack = client.get(
            "/live/qr.svg?url=https://evil.example/steal",
            headers={"host": "genesis.example", "x-forwarded-proto": "https"},
        )
    assert hijack.status_code in {200, 501}
    if hijack.status_code == 200:
        assert b"evil.example" not in hijack.content
        # The code names its own address, and it is this server's.
        assert b'aria-label="QR code for https://genesis.example/live"' in hijack.content
        # Spell UI's treatment: dots, not squares.
        assert b"<circle " in hijack.content


def test_the_qr_refuses_an_origin_where_the_camera_cannot_work(harness):
    """Plain http off localhost: browsers refuse getUserMedia there.

    A poster pointing at such an address sends people to a page that cannot
    even ask for the camera, so it is better to render nothing than that.
    """

    h = harness()
    with TestClient(h.app) as client:
        answer = client.get(
            "/live/qr.svg",
            headers={"host": "genesis.example", "x-forwarded-proto": "http"},
        )
    assert answer.status_code == 409
    assert "insecure" in answer.text.lower()


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/assets/live.js", "javascript"),
        ("/assets/live.css", "css"),
        # Reused as-is rather than reimplemented for the phone.
        ("/assets/mic-worklet.js", "javascript"),
    ],
)
def test_live_assets_are_served(harness, path, kind):
    h = harness()
    with TestClient(h.app) as client:
        asset = client.get(path)
        assert asset.status_code == 200
        assert kind in asset.headers["content-type"]


@pytest.mark.parametrize(
    "name,kind",
    [
        ("Roboto.woff2", "font/woff2"),
        ("RobotoCondensed-800.woff2", "font/woff2"),
        ("gnsis-flat.svg", "image/svg+xml"),
        ("gnsis-mark.svg", "image/svg+xml"),
        ("gnsis-mark-white.svg", "image/svg+xml"),
        ("orbit.svg", "image/svg+xml"),
        # The two vendored libraries, as modules, with their licences.
        ("web-haptics.mjs", "text/javascript"),
        ("web-haptics-core.mjs", "text/javascript"),
        ("torph.mjs", "text/javascript"),
        # Every licence travels with what it covers.
        ("OFL-Roboto.txt", "text/plain"),
        ("OFL-RobotoCondensed.txt", "text/plain"),
        ("LICENSE-web-haptics.txt", "text/plain"),
        ("LICENSE-torph.txt", "text/plain"),
    ],
)
def test_the_brands_files_are_served(harness, name, kind):
    h = harness()
    with TestClient(h.app) as client:
        asset = client.get(f"/assets/live/{name}")
        assert asset.status_code == 200
        assert kind in asset.headers["content-type"]
        assert len(asset.content) > 100


@pytest.mark.parametrize(
    "path",
    [
        "/assets/live/nope.svg",            # not there
        "/assets/live/live.css",            # there, but not in this folder
        "/assets/live/..",                  # not a file name
        "/assets/live/.hidden",             # hidden
        "/assets/live/..%2Flive.html",      # a slash, however it is spelled
    ],
)
def test_the_brand_route_serves_only_that_folder(harness, path):
    h = harness()
    with TestClient(h.app) as client:
        assert client.get(path).status_code == 404


def test_the_brands_files_ship_with_the_package():
    """A folder the wheel leaves behind is a page with no fonts and no mark."""

    from mcpmft.infer.web import STATIC_DIR

    static = Path(STATIC_DIR)
    pyproject = static.parents[2] / "pyproject.toml"
    assert '"static/live/*"' in pyproject.read_text()
    assert (static / "live" / "gnsis-flat.svg").is_file()


def test_haptics_and_the_morph_are_optional_and_the_switch_is_real(harness):
    """Two vendored libraries, neither of which the page may depend on.

    Both are loaded with a dynamic import inside a try, so a missing or broken
    file leaves the page working and merely silent to the hand. The switch is
    a real input the person can turn off, and off means nothing is triggered.
    """

    h = harness()
    with TestClient(h.app) as client:
        body = client.get("/live").text
        source = client.get("/assets/live.js").text
    assert "import('/assets/live/web-haptics.mjs')" in source
    assert "import('/assets/live/torph.mjs')" in source
    # No static import: the page must not fail to load over a helper.
    assert not [line for line in source.splitlines() if line.startswith("import ")]
    assert 'id="haptics"' in body
    assert "if (!this.engine || !this.enabled) return;" in source


def test_gnsis_has_a_bounded_semantic_haptic_output_tool():
    """The model chooses meaning; it never gets access to raw vibration timing."""

    from gnsis_runtime.online_duplex import (
        MODEL_HAPTIC_CUES,
        _model_tool_schemas,
        _split_model_haptics,
    )

    runtime = SimpleNamespace(settings=SimpleNamespace(tool_schemas=()))
    schemas = _model_tool_schemas(runtime)
    haptic = next(schema for schema in schemas if schema["name"] == "haptic")
    assert tuple(haptic["parameters"]["properties"]["cue"]["enum"]) == MODEL_HAPTIC_CUES
    assert haptic["parameters"]["additionalProperties"] is False
    assert not any(
        name in haptic["parameters"]["properties"]
        for name in ("duration", "frequency", "pattern", "intensity")
    )

    event = SimpleNamespace(
        is_tool_call=True,
        tool_error=None,
        tool_calls=[{"name": "haptic", "arguments": {"cue": "confirmation"}}],
    )
    split = _split_model_haptics(event)
    assert split.cues == (
        {"type": "haptic.cue", "cue": "confirmation", "source": "model"},
    )
    assert split.remainder is None and split.rejected is None


def test_haptic_is_reserved_for_the_runtime():
    """A deployment cannot replace the safe semantic haptic contract."""

    from gnsis_runtime.online_duplex import _model_tool_schemas

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            tool_schemas=(
                {
                    "name": "haptic",
                    "description": "unsafe replacement",
                    "parameters": {"type": "object"},
                },
            )
        )
    )
    with pytest.raises(ValueError, match="reserved"):
        _model_tool_schemas(runtime)


def test_the_built_in_tool_does_not_consume_the_configured_schema_budget():
    """Three business tools beside the three task tools filled the budget of six.

    Appending `haptic` made seven, and a deployment that had always started
    refused to (Codex on a prior implementation review). The runtime's own tools now sit outside
    the configured budget: one slot each, and exactly the tokens their schemas
    render to, measured the way the model core measures them.
    """

    from dataclasses import dataclass

    from mcpmft.tool_protocol import (
        ToolProtocolError,
        compact_json,
        ensure_lean_task_tools,
        normalize_tool_schema,
        validate_realtime_tool_context,
    )

    from gnsis_runtime.online_duplex import (
        MODEL_HAPTIC_TOOL_SCHEMA,
        _model_tool_schemas,
        _reserve_built_in_tools,
    )

    class Tokenizer:
        def encode(self, text, add_special_tokens=False):
            return list(text)

    @dataclass
    class Params:
        max_tool_schemas: int = 6
        max_tool_schema_tokens: int = 0

    business = [
        {"name": f"tool_{n}", "description": "d", "parameters": {"type": "object"}}
        for n in range(3)
    ]
    # A deployment exactly at the old limit: six tools, and a token budget
    # that fits them and not one character more.
    six = ensure_lean_task_tools(business)
    budget = len("\n".join(compact_json(tool) for tool in six))
    tokenizer = Tokenizer()
    validate_realtime_tool_context(six, tokenizer, max_tools=6, max_schema_tokens=budget)

    runtime = SimpleNamespace(settings=SimpleNamespace(tool_schemas=tuple(business)))
    seven = _model_tool_schemas(runtime)
    assert [tool["name"] for tool in seven] == [
        "tool_0", "tool_1", "tool_2", "haptic", "task_start", "task_send", "task_resolve",
    ]
    with pytest.raises(ToolProtocolError, match="exposes 7 tools; maximum is 6"):
        validate_realtime_tool_context(seven, tokenizer, max_tools=6, max_schema_tokens=budget)

    reserved = _reserve_built_in_tools(
        Params(max_tool_schema_tokens=budget), SimpleNamespace(tokenizer=tokenizer)
    )
    haptic = compact_json(normalize_tool_schema(MODEL_HAPTIC_TOOL_SCHEMA))
    assert reserved.max_tool_schemas == 7
    assert reserved.max_tool_schema_tokens == budget + len(haptic) + 1
    validate_realtime_tool_context(
        seven,
        tokenizer,
        max_tools=reserved.max_tool_schemas,
        max_schema_tokens=reserved.max_tool_schema_tokens,
    )

    # A bundle with no tokenizer cannot check tokens, so only the slot is reserved;
    # a params stub without the fields is left alone.
    count_only = _reserve_built_in_tools(Params(max_tool_schema_tokens=budget), SimpleNamespace())
    assert (count_only.max_tool_schemas, count_only.max_tool_schema_tokens) == (7, budget)
    stub = SimpleNamespace(chunk_ms=1000)
    assert _reserve_built_in_tools(stub, SimpleNamespace()) is stub


def test_the_app_reserves_its_built_in_tools_when_it_is_built(harness, monkeypatch):
    """The reservation is worthless unless the app applies it to the params it runs on."""

    from gnsis_runtime import online_duplex

    seen = []
    real = online_duplex._reserve_built_in_tools

    def spy(params, bundle):
        seen.append((params, bundle))
        return real(params, bundle)

    monkeypatch.setattr(online_duplex, "_reserve_built_in_tools", spy)
    harness()
    assert len(seen) == 1


def test_a_haptic_beside_another_call_is_lifted_out_and_the_rest_goes_on():
    """The model may put touch beside a task call in one unit.

    Taken whole, the coordinator refused that batch as mixed, so the cue was
    never felt and the other call was thrown away with it (Codex on the prior project
    #159). Now the cue goes to the phone and the other call goes on alone.
    """

    from gnsis_runtime.online_duplex import _split_model_haptics

    task = {"name": "task_send", "arguments": {"task_id": "t1", "text": "look left"}}
    event = SimpleNamespace(
        index=7,
        is_tool_call=True,
        tool_error=None,
        tool_calls=[{"name": "haptic", "arguments": {"cue": "attention"}}, task],
        metrics={},
    )
    split = _split_model_haptics(event)
    assert split.cues == ({"type": "haptic.cue", "cue": "attention", "source": "model"},)
    assert split.rejected is None
    assert split.remainder is not event
    assert split.remainder.tool_calls == [task]
    assert split.remainder.index == 7 and split.remainder.is_tool_call
    # The original event is not touched.
    assert len(event.tool_calls) == 2


def test_a_unit_without_touch_passes_through_untouched():
    from gnsis_runtime.online_duplex import _split_model_haptics

    plain = SimpleNamespace(is_tool_call=False, tool_error=None, tool_calls=[], text="hi")
    assert _split_model_haptics(plain).remainder is plain
    other = SimpleNamespace(
        is_tool_call=True, tool_error=None, tool_calls=[{"name": "task_send", "arguments": {}}]
    )
    split = _split_model_haptics(other)
    assert split.remainder is other and split.cues == () and split.rejected is None
    errored = SimpleNamespace(
        is_tool_call=True,
        tool_error="a previous tool call is still awaiting its response",
        tool_calls=[],
    )
    assert _split_model_haptics(errored).remainder is errored


def test_a_malformed_haptic_call_is_refused_not_forwarded():
    """A call named haptic that names no cue must never travel on as an external tool."""

    from gnsis_runtime.online_duplex import _split_model_haptics

    event = SimpleNamespace(
        is_tool_call=True,
        tool_error=None,
        tool_calls=[
            {"name": "haptic", "arguments": {"cue": "buzz"}},
            {"name": "task_send", "arguments": {}},
        ],
    )
    split = _split_model_haptics(event)
    assert split.remainder is None and split.cues == ()
    assert split.rejected.startswith("haptic cue must be one of ")


def test_the_phone_renders_model_haptics_as_device_presets(harness):
    """The websocket carries semantics and the browser owns the physical feel."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    assert "case 'haptic.cue':" in source
    assert "attention: 'medium'" in source
    assert "proximity: 'selection'" in source
    assert "confirmation: 'success'" in source
    assert "warning: 'warning'" in source
    assert "lastModelHapticAt" in source
    assert "performance.now() - live.lastModelHapticAt < 750" in source


def test_a_long_status_wraps_instead_of_leaving_the_screen(harness):
    """torph keeps a morph on one line with a rule it injects after ours.

    On a 320px phone the busy message then runs to x=371 and the page's
    overflow: hidden cuts off the half that says what to do. Caught by Codex
    on #158, reproduced in a browser. The pill wraps instead, and it has to
    be `!important`: the library's rule arrives later and keys on its own
    attribute name, so specificity alone is a bet on that name.
    """

    h = harness()
    with TestClient(h.app) as client:
        css = client.get("/assets/live.css").text
    pill = css.split(".status {")[1].split("}")[0]
    assert "white-space: normal !important" in pill
    assert "max-width: 100%" in pill


def test_the_phone_client_asks_for_the_rear_camera(harness):
    """Someone pointing a phone at a thing wants the lens on the far side."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text
    assert "facingMode: { ideal: 'environment' }" in source
    # `ideal`, not `exact`: a laptop with only a front camera must still work,
    # and `exact` makes the browser throw rather than fall back.
    assert "exact:" not in source


def _config(tmp_path, provider: str, name: str, **duplex) -> str:
    """A release config whose worker, if it has one, cannot be satisfied."""

    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    checkpoint = tmp_path / "duplex.pt"
    checkpoint.write_bytes(b"")
    document = {
        "model": {"model_name_or_path": str(model_dir)},
        "duplex": {"checkpoint": str(checkpoint), **duplex},
        "server": {"mode": "lean"},
        "worker": {
            "provider": provider,
            "cwd": str(tmp_path / "nowhere"),
            "settings": {"codex_bin": str(tmp_path / "no-such-codex")},
        },
    }
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


def test_a_server_with_no_action_layer_boots_without_codex(tmp_path):
    """`worker.provider: none` is how a deployment says it has no action layer.

    The direct camera experience does not dispatch work, and a stock config
    names Codex, so it refused to boot over a binary it would never call.
    """

    from gnsis_runtime.cli import load_config, preflight_config

    preflight_config(load_config(_config(tmp_path, "none", "none")))


def test_a_configured_worker_is_still_demanded(tmp_path):
    """Naming a provider still means it has to be there.

    This is the correction to the first attempt, which keyed off `server.mode`
    on the theory that lean mode never dispatches. It does: `task_start`,
    `task_send` and `task_resolve` are native tools the model can call, and
    `gateway.task_start` resolves a provider itself. An empty registry would
    have refused every one of them with `no_eligible_worker`. Caught by Codex.
    """

    from gnsis_runtime.cli import load_config, preflight_config

    with pytest.raises(Exception) as raised:
        preflight_config(load_config(_config(tmp_path, "codex", "codex")))
    assert "worker" in str(raised.value).lower()


def test_the_camera_opt_in_can_actually_be_set(tmp_path):
    """An opt-in no deployment can reach is not an opt-in.

    `persist_camera_frames` lived only on the Python settings object; unknown
    YAML keys are rejected and nothing forwarded a value, so every real
    `gnsis-serve` was stuck at false whatever its operator wanted. Caught by
    Codex.
    """

    from gnsis_runtime.cli import _duplex_settings, load_config

    config = load_config(
        _config(tmp_path, "none", "optin", persist_camera_frames=True)
    )
    assert config.duplex.persist_camera_frames is True
    # And off unless asked, which is the half that matters for a public QR.
    assert load_config(
        _config(tmp_path, "none", "default")
    ).duplex.persist_camera_frames is False

    # Parsing the key is only half of reachable. The value has to survive the
    # trip into the runtime's own settings, and `build_app` loads a model, so
    # that mapping is tested through the function split out of it. Without
    # this the forwarding line could be deleted and every test still passed —
    # which is exactly what happened, and what the commit message claimed had
    # been ruled out.
    assert _duplex_settings(config).persist_camera_frames is True
    assert (
        _duplex_settings(
            load_config(_config(tmp_path, "none", "default2"))
        ).persist_camera_frames
        is False
    )


def test_the_client_negotiates_camera_mode_before_opening_the_screen(harness):
    """A session starts in voice mode and refuses every frame while it is.

    Opening /ws/screen first looks like it works and then waits for a frame
    that will never be accepted. Caught by Codex.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text
    assert "'media.mode'" in source
    assert "media.mode.done" in source
    # The screen socket is attached from the mode acknowledgement, not from
    # `ready`. If `attachScreen` moves back under `ready`, this fails.
    after_ready = source.split("case 'ready':")[1].split("case 'media.mode.done'")[0]
    assert "attachScreen" not in after_ready


def _answer(ws, wanted: str) -> dict:
    """`_drain_until`, except a fatal `error` fails now rather than never."""

    for _ in range(20):
        message = ws.receive_json()
        if message.get("type") == wanted:
            return message
        if message.get("type") == "error" and message.get("fatal"):
            raise AssertionError(f"fatal error before {wanted!r}: {message.get('message')}")
    raise AssertionError(f"never received {wanted!r}")


def test_a_voice_session_takes_camera_frames_only_after_media_mode(harness):
    """The server side of the phone client's handshake, which nothing tested.

    A session starts in voice mode, and a screen channel attached while it is
    in voice mode is refused and closed. The client therefore asks for video
    with `media.mode`, waits for `media.mode.done`, and only then attaches
    (the fix Codex asked for). This pins the server that fix was written
    against, in that order.

    It also pins the harness. Driving the real page in a browser against this
    suite's stubs, the `media.mode` request was answered with a fatal error
    naming a params field the stub did not have. Every test was green because
    none of them made the request. This one does, so the stub has to be whole.
    """

    # The public-QR shape: lean, voice to begin with, client video allowed.
    h = harness(allow_client_video=True, provider_name=None)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            ready = _settle(ws)
            screen = ready["screen"]
            assert screen["enabled"] is True
            attach = f"/ws/screen?session_id={ready['session_id']}&token={screen['token']}"

            # Attached too early: told why, and closed. This is the behaviour
            # that makes negotiating first necessary rather than polite.
            with connect(client, attach) as early:
                refused = early.receive_json()
                assert refused["type"] == "error"
                assert "voice" in refused["message"]
                with pytest.raises(WebSocketDisconnect):
                    early.receive_json()

            ws.send_json({"type": "media.mode", "video": True, "source": "camera"})
            done = _answer(ws, "media.mode.done")
            assert done["video"] is True
            assert done["source"] == "camera"

            # Attached after: ready, and a camera frame is seen.
            with connect(client, attach) as screen_ws:
                _drain_until(screen_ws, "screen.ready")
                screen_ws.send_text(json.dumps(_camera_header("f1")))
                screen_ws.send_bytes(_jpeg())
                accepted = _drain_until(screen_ws, "screen.frame.accepted")
                assert accepted["frame_id"] == "f1"
    # And, lean, nothing kept.
    written = [p for p in h.media_dir.rglob("*") if p.is_file()] if h.media_dir.exists() else []
    assert written == []


def test_the_client_renders_model_chunks(harness):
    """`turn.final.accepted` carries no text; `chunk` does.

    Reading the wrong one left the page silent whenever speech was off.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text
    assert "case 'chunk':" in source
    assert "case 'turn.final.accepted':" not in source


def test_end_says_stop_rather_than_dropping_the_socket(harness):
    """An explicit stop is never parked; a dropped socket is.

    Closing outright holds the single model slot for the whole reconnect
    grace, so ending a session and scanning again told the next person Genesis
    was busy — because of the session they had just ended. Caught by Codex.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text
    assert "type: 'stop'" in source
    assert "session.done" in source
    # Bounded: a server that never answers must not strand a live camera.
    stop_block = source.split("type: 'stop'")[0]
    assert "setTimeout(resolve" in stop_block


def test_the_live_prompt_teaches_recent_visual_context():
    """GNSIS must answer from the recent visual sequence, not the current frame alone.

    The context window already retains ~context_max_units recent multimodal
    units; the prompt is what turns retention into behaviour. This pins the
    section's presence and the failure it is written against: answering "I can
    see a television" when the question was about the remote shown before it.
    """

    from mcpmft.prompts import GNSIS_DUPLEX_SYSTEM_PROMPT

    assert "RECENT VISUAL CONTEXT" in GNSIS_DUPLEX_SYSTEM_PROMPT
    assert "not as an isolated image" in GNSIS_DUPLEX_SYSTEM_PROMPT
    assert "Which one was the Roku" in GNSIS_DUPLEX_SYSTEM_PROMPT
    assert "Do not invent continuity" in GNSIS_DUPLEX_SYSTEM_PROMPT


def test_gnsis_live_mvp_has_no_external_worker_dependency():
    """The deployable MVP is GNSIS perception itself, not the Ornith action layer."""

    config_path = Path(__file__).resolve().parents[2] / "configs" / "gnsis-live.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["worker"] == {"provider": "none"}
    assert document["duplex"]["allow_client_video"] is True
    assert document["duplex"]["media_mode"] == "omni"
    assert set(document["duplex"]["client_video_sources"]) == {"camera", "screen"}


def test_a_phone_session_leaves_a_reconstructable_log_trail(harness, caplog):
    """The phone session IS the eval: its logs must reconstruct what happened.

    After one real session an operator reads the log, not a harness, to tell
    "frame never arrived" apart from "retained but unused". This pins the
    events that reconstruction walks: connect, ready, media.mode, screen
    attach, frame accepted, disconnects — each carrying container + session.
    """

    caplog.set_level("INFO")
    h = harness(allow_client_video=True, provider_name=None)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            ready = _settle(ws)
            attach = (
                f"/ws/screen?session_id={ready['session_id']}"
                f"&token={ready['screen']['token']}"
            )
            ws.send_json({"type": "media.mode", "video": True, "source": "camera"})
            _answer(ws, "media.mode.done")
            with connect(client, attach) as screen:
                _drain_until(screen, "screen.ready")
                screen.send_text(json.dumps(_camera_header("f1")))
                screen.send_bytes(_jpeg())
                _drain_until(screen, "screen.frame.accepted")
                # A malformed frame is a logged, structured drop — not silent.
                screen.send_text("{not json")
                _drain_until(screen, "screen.frame.dropped")
            _stop(ws)

    messages = [record.getMessage() for record in caplog.records]
    seen = "\n".join(messages)
    for event in (
        "channel=duplex session_id=s1",
        "duplex ready: container=",
        "media mode requested: container=",
        "media mode applied: container=",
        "channel=screen session_id=s1",
        "screen ready: container=",
        "screen frame accepted: container=",
        "screen frame dropped: container=",
        "screen disconnected: container=",
    ):
        assert event in seen, f"missing log event: {event}"
    # Diagnostics never carry the screen token or frame contents.
    assert ready["screen"]["token"] not in seen


def test_screen_rejects_are_logged_without_the_token(harness, caplog):
    """Every refused screen attach names a safe reason, never the token."""

    caplog.set_level("INFO")
    h = harness(allow_client_video=True, provider_name=None)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            # No active session under this id at all.
            with connect(client, "/ws/screen?session_id=ghost&token=x") as early:
                assert early.receive_json()["type"] == "error"
            # Real session, still in voice mode.
            with connect(
                client, "/ws/screen?session_id=s1&token=x"
            ) as voice:
                assert "voice" in voice.receive_json()["message"]
            _stop(ws)

    messages = [record.getMessage() for record in caplog.records]
    seen = "\n".join(messages)
    assert "reason=inactive_session" in seen
    assert "reason=voice_mode" in seen


def test_the_mic_journal_is_written_only_when_a_worker_exists(tmp_path):
    """The audio twin of the camera problem.

    Every session's raw microphone PCM went to `<key>.input.pcm` under
    media_dir, and nothing deleted it unless the session was reset. The file
    exists so a worker provider can attach the turn's audio to `turn.final`
    media; with `worker.provider: none` nothing ever reads it, so it must not
    be written.
    """

    from gnsis_runtime.task_tools_online import TaskToolsRealtimeCoordinator

    class _Ledger:
        def list_realtime_context(self, _owner):
            return []

    class _Gateway:
        providers: dict = {}
        ledger = _Ledger()

        async def close(self):
            pass

    lean = TaskToolsRealtimeCoordinator(
        _Gateway(), "owner", "s1", session=None,
        provider_name=None, media_dir=tmp_path / "lean",
    )
    lean.record_pcm16(b"\x00\x01" * 8)

    served = TaskToolsRealtimeCoordinator(
        _Gateway(), "owner", "s2", session=None,
        provider_name="stub", media_dir=tmp_path / "worker",
    )
    served.record_pcm16(b"\x00\x01" * 8)

    written = list(tmp_path.rglob("*.input.pcm"))
    assert len(written) == 1
    assert written[0].parent.name == "worker"


def test_the_speech_rms_diagnostic_can_actually_be_set(tmp_path):
    """Same reachable-opt-in shape as `persist_camera_frames`.

    `input_speech_rms` lived only on the Python config; unknown YAML keys are
    rejected, so no deployment could tune the `input_has_speech` flag.
    """

    from gnsis_runtime.cli import _duplex_settings, load_config

    config = load_config(_config(tmp_path, "none", "rms", input_speech_rms=0.01))
    assert _duplex_settings(config).input_speech_rms == 0.01
    default = _duplex_settings(load_config(_config(tmp_path, "none", "rmsd")))
    assert default.input_speech_rms == 1e-4


def test_health_distinguishes_configured_voice_assets_from_loaded(harness):
    """`configured` is what the deployment asked for; `loaded` is verified.

    Configured-but-not-loaded is exactly the state the Path B checks must
    catch — a field that conflates the two would report a working voice
    path that never proved its assets.
    """

    h = harness(
        talker_checkpoint="/tmp/talker.safetensors",
        token2wav_dir="/tmp/token2wav",
    )
    with TestClient(h.app) as client:
        payload = client.get("/health").json()
    assert payload["talker_checkpoint_configured"] is True
    assert payload["token2wav_configured"] is True
    # The stub model carries no tts/token2wav modules — asked for, not loaded.
    assert payload["tts_loaded"] is False
    assert payload["token2wav_loaded"] is False


def test_the_voice_config_enables_speech_and_nothing_else(tmp_path):
    """gnsis-voice.yaml is the isolated Path B runtime: speech on, nothing
    else changed.

    Each subsystem's state is asserted explicitly so a default can never
    silently decide it — the Gander pair was verified against the release
    manifest (8 text / 50 speech tokens per unit, emitted as 2×25).
    """

    from gnsis_runtime.cli import load_config

    config = load_config("runtime/configs/gnsis-voice.yaml")

    # Voice on: detached Talker on GPU 1, Token2wav + ref audio on the
    # verified Gander pair, and the unit contract values.
    assert config.model.init_tts is True
    assert config.model.token2wav_dir.endswith("/token2wav")
    assert config.duplex.generate_audio is True
    assert config.duplex.talker_checkpoint == "/models/Gander/talker"
    assert config.duplex.detached_talker_device == "cuda:1"
    assert config.duplex.speak_text_tokens_per_unit == 8
    assert config.duplex.talker_speech_tokens_per_unit == 50
    assert config.duplex.talker_emit_speech_tokens == 25
    assert config.server.cuda_visible_devices == "0,1"

    # Everything else exactly as production: raw audio into the model (no
    # ASR), no worker, no memory layer, camera negotiated as before.
    assert config.asr.mode == "disabled"
    assert config.worker.provider == "none"
    assert config.duplex.sliding_window_mode == "context_no_previous"
    assert config.duplex.allow_client_video is True
    assert config.duplex.client_video_mode == "omni"
    assert config.duplex.persist_camera_frames is False


def test_a_refused_switch_hands_the_controls_back(harness):
    """A negotiation that ends in `media.mode.rejected` is terminal.

    The switches were locked when the request went out; if the refusal does
    not unlock them, a camera the server cannot use leaves the page dead until
    End. The lock is released by re-resolving `pendingSource` — which the
    rejection clears — through `syncSourceControls`.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    rejected = source.split("case 'media.mode.rejected':")[1].split("break;")[0]
    assert "live.pendingSource = null" in rejected
    assert "syncSourceControls(session)" in rejected


def test_a_switch_stays_locked_until_the_runtime_answers(harness):
    """The controls unlock on the answer, not on the request leaving.

    Re-enabling in `setSource`'s `finally` let a second switch overlap the
    first negotiation, and whichever `media.mode.done` landed last decided
    what the page believed it was showing. The lock now follows
    `pendingSource`, which only the matching `done`/`rejected` clears.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    helper = source.split("function syncSourceControls(session) {")[1].split("}")[0]
    assert "session.pendingSource" in helper
    assert "setSwitchesDisabled" in helper

    set_source = source.split("async function setSource(kind) {")[1]
    # No second negotiation while one is in flight.
    assert "session.pendingSource" in set_source.split("const duplex")[0]
    # The finally no longer unlocks unconditionally.
    finally_block = set_source.rsplit("} finally {", 1)[1].split("}")[0]
    assert "setSwitchesDisabled(false)" not in finally_block
    assert "syncSourceControls(session)" in finally_block

    done = source.split("case 'media.mode.done':")[1].split("break;")[0]
    assert "syncSourceControls(session)" in done


def test_sight_is_bound_to_a_frame_of_the_new_source(harness):
    """A delayed ACK for an old-source frame must not declare the switch seen.

    `sightSeq` marks the last frame sent before `media.mode.done` resolved the
    switch; `awaitingSight` only clears on a `live-N` acknowledgement with N
    beyond it. The sequence lives on the session so a `screen.ready` re-arm of
    the frame timer cannot restart it.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    assert "live.sightSeq = live.frameSeq" in source
    assert "session.frameSeq += 1" in source
    ack = source.split("payload.type === 'screen.frame.accepted'")[1]
    assert "live-(\\d+)" in ack
    assert "> live.sightSeq" in ack


def test_a_dead_screen_channel_releases_the_session(harness):
    """A screen socket that fails after the switch is a terminal failure too.

    Without a close/error path the page kept the camera live and the switches
    locked on a channel that would never carry a frame again.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    attach = source.split("function attachScreen(session, ready) {")[1]
    assert "socket.addEventListener('close', dropped)" in attach
    assert "socket.addEventListener('error', dropped)" in attach
    assert "The video link dropped." in attach


def test_controls_stay_locked_through_first_sight(harness):
    """`media.mode.done` is the runtime accepting the request, not GNSIS seeing.

    A switch resolves only when a frame from the new source is acknowledged,
    so the lock is `pendingSource || awaitingSight` — not the request alone.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    helper = source.split("function syncSourceControls(session) {")[1].split("}")[0]
    assert "session.pendingSource || session.awaitingSight" in helper

    done = source.split("case 'media.mode.done':")[1].split("case 'media.mode.rejected'")[0]
    assert "live.awaitingSight = live.stats.accepted > 0" in done
    assert "live.sightSeq = live.frameSeq" in done


def test_only_a_new_source_frame_unlocks_and_names_the_source(harness):
    """The final line is said when it is true: a new-source ACK, by name."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    ack = source.split("payload.type === 'screen.frame.accepted'")[1]
    assert "> live.sightSeq" in ack
    assert "Sharing your screen." in ack
    assert "Camera on." in ack
    assert "live.awaitingSight = false" in ack
    assert "syncSourceControls(session)" in ack


def test_a_cancelled_picker_restores_without_a_failure(harness):
    """Closing the share picker is a choice: previous source, no error shown."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    cancelled = source.split("// Closing the picker is a choice")[1].split("return;")[0]
    assert "session.switchTiming = null" in cancelled
    # Clears the "starting" line, not a failure tone.
    assert "tone: 'bad'" not in cancelled


def test_every_switch_attempt_is_timed(harness):
    """Click → request → done → first real frame, as one debug record."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    assert "session.switchAttempt += 1" in source
    assert "timing.requestedAt = performance.now()" in source
    assert "live.switchTiming.doneAt = performance.now()" in source
    log = source.split("console.debug('source_switch'")[1]
    for field in ("attempt", "from", "to", "request_ms",
                  "negotiation_ms", "first_frame_after_done_ms",
                  "total_visible_ms"):
        assert field in log


def test_a_stale_first_ack_cannot_announce_ready(harness):
    """A delayed first ACK outlives its source — it must not unlock or report.

    Frame acknowledgements arrive after their source may have ended. If the
    very first ACK lands after `goDark` or while a replacement picker is open,
    the page must neither say "Session ready." nor enable switches that
    `setSource` would still ignore.
    """

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    first = source.split("live.stats.accepted === 1")[1].split("else if (live.awaitingSight)")[0]
    assert "live.source && !live.pendingSource" in first
    assert "syncSourceControls(session)" in first
    assert "setSwitchesDisabled(false)" not in first


def test_the_tap_is_answered_before_the_server_replies(harness):
    """Status and the busy marker move at click time, not at `media.mode.done`."""

    h = harness()
    with TestClient(h.app) as client:
        source = client.get("/assets/live.js").text

    body = source.split("async function setSource(kind) {")[1]
    request_at = body.index("duplex.send(JSON.stringify({ type: 'media.mode'")
    assert body.index("'Starting to share…'") < request_at
    assert body.index("'Turning the camera on…'") < request_at
    assert body.index("aria-busy") < request_at
