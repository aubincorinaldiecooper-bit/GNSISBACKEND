from __future__ import annotations

from gnsis_visual_sdk.desktop_eval import Screen, execute_action


def test_click_denormalizes_against_the_captured_screen() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_xdotool(*args: str) -> tuple[bool, str]:
        calls.append(args)
        return True, ""

    import gnsis_visual_sdk.desktop_eval as module

    original = module._run_xdotool
    module._run_xdotool = fake_xdotool
    try:
        result = execute_action(
            {"action": "click", "target": {"x": 0.5, "y": 0.25}},
            Screen(3200, 2400),
            browser_url_prefix=None,
        )
    finally:
        module._run_xdotool = original

    assert result["success"] is True
    assert calls == [("mousemove", "1600", "600", "click", "1")]


def test_navigate_is_pinned_to_the_allowed_origin() -> None:
    result = execute_action(
        {"action": "navigate", "url": "http://evil.example/phish"},
        Screen(3200, 2400),
        browser_url_prefix="http://127.0.0.1:8899/",
    )

    assert result["success"] is False
    assert "outside allowed origin" in result["message"]


def test_unknown_action_is_rejected_without_executing() -> None:
    result = execute_action(
        {"action": "open_terminal"},
        Screen(3200, 2400),
        browser_url_prefix=None,
    )

    assert result["success"] is False
    assert "unsupported desktop action" in result["message"]


def test_done_never_runs_desktop_input() -> None:
    calls: list[tuple[str, ...]] = []

    import gnsis_visual_sdk.desktop_eval as module

    original = module._run_xdotool
    module._run_xdotool = lambda *args: calls.append(args) or (True, "")
    try:
        result = execute_action(
            {"action": "done"},
            Screen(3200, 2400),
            browser_url_prefix=None,
        )
    finally:
        module._run_xdotool = original

    assert result == {"success": True, "done": True, "message": "Task is complete."}
    assert calls == []
