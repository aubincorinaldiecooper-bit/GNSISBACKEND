from __future__ import annotations

import json

import pytest
from PIL import Image

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame
from gnsis_runtime.visual.benchmark import (
    LAYA_CONTRACT,
    SMALLER_GNSIS,
    BenchmarkCase,
    LayaContractContestant,
    Observation,
    Oracle,
    PerceivedState,
    PerceivedTarget,
    PolicyContestant,
    RetirementThresholds,
    calibration,
    laya_options,
    load_cases,
    paired_difference,
    parse_visual_state,
    retirement_gate,
    run_cases,
    run_episode,
)
from gnsis_runtime.visual.control import ABSTAIN_WAIT_MS, ExecutionReport, step_from_decision
from gnsis_runtime.visual.legal import IllegalDecision, legal_actions
from gnsis_runtime.visual.real_runs import Box, Execution
from gnsis_runtime.visual.runtime import DecisionGate, PersistentVisualDecisionSession
from gnsis_runtime.visual.schema import Decision, DecisionError, Target

GOAL = 'Search for "red shoes" on https://shop.example/catalog'


def _frame(frame_id: str, ts: int, value: int = 255, size=(64, 32)) -> ScreenFrame:
    return ScreenFrame(frame_id=frame_id, image=Image.new("RGB", size, (value, value, value)), captured_at_ms=ts)


def _consume(buf: LatestScreenFrameBuffer, frame: ScreenFrame) -> None:
    buf.publish(frame)
    buf.consume_for_unit()


class FixedPolicy:
    name = "fixed"

    def __init__(self, *decisions):
        self.decisions = list(decisions)

    def decide(
        self,
        frame,
        goal,
        history,
        motion,
        viewport,
        cache,
        allowed_actions=None,
    ):
        made = self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        if isinstance(made, Exception):
            raise made
        return made if made.frame_id is not None else Decision(**{**made.__dict__, "frame_id": frame.frame_id})


def _session(*decisions, frames=(("f1", 1000, 255),), gate=None, goal=GOAL):
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    for frame_id, ts, value in frames:
        _consume(buf, _frame(frame_id, ts, value))
    session = PersistentVisualDecisionSession(FixedPolicy(*decisions), buf, gate=gate)
    session.set_task(goal)
    return session, buf


# ----------------------------------------------------------------- legal set


def test_legal_set_is_generated_from_the_goal_and_bound_to_the_frame():
    legal = legal_actions(GOAL, "f1", (64, 32))
    keys = [choice.key for choice in legal.choices]
    assert keys == ["scroll:down", "scroll:up", "navigate:0", "type:0", "click", "back", "wait", "done", "recover"]
    assert legal.by_key()["type:0"].text == "red shoes"
    assert legal.by_key()["navigate:0"].url == "https://shop.example/catalog"
    assert legal.match(Decision("type", 0.9, Target(3, 3), text="red shoes", frame_id="f1")).key == "type:0"


@pytest.mark.parametrize(
    "decision, reason",
    [
        (Decision("type", 0.9, Target(3, 3), text="blue shoes", frame_id="f1"), "not derived from the goal"),
        (Decision("navigate", 0.9, url="https://evil.example/", frame_id="f1"), "not derived from the goal"),
        (Decision("click", 0.9, Target(3, 3), text="red shoes", frame_id="f1"), "not derived from the goal"),
        (Decision("click", 0.9, Target(3, 3), frame_id="f0"), "not the observed frame"),
        (Decision("click", 0.9, Target(3, 3)), "not bound to the observed frame"),
        (Decision("click", 0.9, Target(99, 3), frame_id="f1"), "outside viewport"),
        (Decision("run_shell", 0.9, frame_id="f1"), "unknown action"),
    ],
)
def test_invented_targets_values_urls_and_commands_are_rejected(decision, reason):
    legal = legal_actions(GOAL, "f1", (64, 32))
    with pytest.raises(DecisionError, match=reason):
        legal.match(decision)


def test_goal_without_values_offers_no_type_or_navigate():
    legal = legal_actions("open the settings", "f1", (64, 32))
    assert "type" not in legal.actions and "navigate" not in legal.actions


def test_allowed_actions_filter_the_generated_set_but_keep_safe_wait():
    legal = legal_actions(
        GOAL,
        "f1",
        (64, 32),
        allowed_actions=("click",),
    )
    assert legal.actions == {"click", "wait"}


# ------------------------------------------------------- abstention and change


def test_low_confidence_becomes_wait_and_keeps_the_proposal():
    session, _ = _session(Decision("click", 0.3, Target(5, 5)))
    gated = session.decide_gated()
    assert gated.status == "abstain" and "below 0.50" in gated.reason
    assert gated.decision.action == "wait" and gated.decision.frame_id == "f1"
    assert gated.proposed.action == "click" and gated.choice_key == "click"
    assert session.decide().action == "wait"


def test_targets_wait_for_an_unsettled_screen_but_scroll_does_not():
    frames = (("f1", 1000, 0), ("f2", 1200, 255))
    session, _ = _session(Decision("click", 0.9, Target(5, 5)), frames=frames)
    gated = session.decide_gated()
    assert gated.status == "abstain" and "still changing" in gated.reason
    session, _ = _session(Decision("scroll", 0.9, direction="down"), frames=frames)
    assert session.decide_gated().status == "act"


def test_repeating_an_action_that_changed_nothing_abstains_until_the_screen_changes():
    session, buf = _session(Decision("click", 0.9, Target(5, 5)))
    first = session.decide()
    session.record_attempt(first)
    _consume(buf, _frame("f2", 3000, 255))
    gated = session.decide_gated()
    assert gated.status == "abstain" and "no visible change" in gated.reason
    assert gated.change_since_last_action == 0.0
    _consume(buf, _frame("f3", 6000, 0))
    assert session.decide_gated().status == "act"
    assert session.state()["change_since_last_action"] > 0.5


def test_illegal_proposals_are_rejected_not_absorbed():
    session, _ = _session(Decision("type", 0.99, Target(5, 5), text="invented"))
    gated = session.decide_gated()
    assert gated.status == "rejected" and gated.decision.action == "wait"
    with pytest.raises(IllegalDecision):
        session.decide()
    session, _ = _session(DecisionError("decoder produced garbage"))
    assert session.decide_gated().reason == "decoder produced garbage"


def test_a_chosen_wait_is_not_gated_and_the_gate_is_configurable():
    session, _ = _session(Decision("wait", 0.1))
    assert session.decide_gated().status == "act"
    session, _ = _session(Decision("click", 0.3, Target(5, 5)), gate=DecisionGate(min_confidence=0.2))
    assert session.decide_gated().status == "act"
    with pytest.raises(ValueError):
        DecisionGate(min_confidence=1.5)


# ----------------------------------------------------------- actuator mapping


def test_only_the_gated_decision_reaches_the_actuator():
    session, _ = _session(Decision("click", 0.3, Target(5, 5)))
    step = step_from_decision(session.decide_gated(), run_id="r", case_id="c", goal=GOAL)
    assert (step.action, step.wait_ms, step.target, step.frame_id) == ("wait", ABSTAIN_WAIT_MS, None, "f1")

    session, _ = _session(Decision("type", 0.9, Target(5, 6), text="red shoes"))
    step = step_from_decision(session.decide_gated(), run_id="r", case_id="c", goal=GOAL)
    assert (step.action, step.text, step.target.x, step.target.y) == ("type", "red shoes", 5, 6)
    assert step.expected_state is not None and step.expected_state.visible_text == ("red shoes",)


# ------------------------------------------------------------ Laya contract


def test_parse_visual_state_matches_the_browser_parser():
    state = parse_visual_state(
        'noise {"summary": "Shop", "page_stable": true, "targets": ['
        '{"id": "q", "label": "Search", "role": "input", "point": {"x": 0.5, "y": 0.25}, "affordances": ["type_text"]},'
        '{"id": "bad", "point": {"x": 2, "y": 0}}, {"point": {"x": 0.1, "y": 0.1}}]}'
    )
    assert state.page_stable and state.summary == "Shop"
    assert [t.id for t in state.targets] == ["q"] and state.targets[0].affordances == ("TYPE_TEXT",)
    assert parse_visual_state("not json").targets == ()


def test_laya_options_are_a_bounded_projection_of_the_legal_set():
    legal = legal_actions(GOAL, "f1", (100, 50))
    targets = tuple(PerceivedTarget(f"t{i}", f"T{i}", "input", 0.5, 0.5, ("CLICK", "TYPE_TEXT")) for i in range(12))
    options = laya_options(legal, PerceivedState("s", "", True, targets))
    assert len(options) == 20
    for option in options.values():
        legal.match(option.decision)
    one = laya_options(legal, PerceivedState("s", "", True, (PerceivedTarget("q", "Search", "input", 0.5, 0.5, ("TYPE_TEXT",)),)))
    assert one["type:q:0"].decision.target == Target(50, 25)
    assert {"scroll:down", "navigate:0", "back", "wait", "done"} <= set(one)


class StaticPerceiver:
    def __init__(self, state):
        self.state = state
        self.seen = []

    def perceive(self, observation):
        self.seen.append(observation)
        return self.state


class KeyChooser:
    def __init__(self, key, confidence=0.9):
        self.key, self.confidence = key, confidence
        self.criteria = []

    def choose(self, state, criteria):
        self.criteria.append(dict(criteria))
        return self.key, self.confidence


# --------------------------------------------------------------- benchmark


def _case(case_id, action, *, value=None, box=None, motion=None, family="f"):
    frame = _frame(f"{case_id}:0", 1000, size=(100, 50))
    return BenchmarkCase(
        Observation(case_id=case_id, goal=GOAL, frames=(frame,), motion=motion),
        Oracle(action, value, box),
        family,
    )


class RecordingContestant:
    def __init__(self, name, decide):
        self.name = name
        self.decide = decide
        self.seen = []

    def propose(self, observation, legal):
        self.seen.append((observation, legal.to_json()))
        return self.decide(observation, legal)


def test_contestants_see_identical_inputs_and_never_the_oracle():
    cases = [_case("a", "click", box=Box(40, 20, 20, 10)), _case("b", "wait", motion=0.9)]
    ours = RecordingContestant(SMALLER_GNSIS, lambda obs, legal: Decision("click", 0.9, Target(45, 25), frame_id=legal.frame_id))
    theirs = RecordingContestant(LAYA_CONTRACT, lambda obs, legal: Decision("wait", 0.8, frame_id=legal.frame_id))
    report = run_cases([ours, theirs], cases)
    assert [legal for _, legal in ours.seen] == [legal for _, legal in theirs.seen]
    for observation, _ in ours.seen + theirs.seen:
        assert type(observation) is Observation and not hasattr(observation, "oracle")
    a = report["contestants"][SMALLER_GNSIS]
    assert a["accuracy"]["ok"] == 2 and a["grounding"]["rate"] == 1.0
    assert a["abstention"]["should_wait_recall"]["rate"] == 1.0
    assert report["paired"]["accuracy"]["difference"] == 0.5


def test_invalid_laya_choice_and_low_confidence_are_scored_not_executed():
    case = _case("a", "type", value="red shoes", box=Box(40, 20, 20, 10))
    perceived = PerceivedState("s", "", True, (PerceivedTarget("q", "Search", "input", 0.5, 0.5, ("TYPE_TEXT",)),))
    invalid = LayaContractContestant(StaticPerceiver(perceived), KeyChooser("type:q:7"))
    unsure = LayaContractContestant(StaticPerceiver(perceived), KeyChooser("type:q:0", 0.2))
    unsure.name = "laya-unsure"
    good = LayaContractContestant(StaticPerceiver(perceived), KeyChooser("type:q:0", 0.9))
    good.name = "laya-good"
    report = run_cases([invalid, unsure, good], [case])["contestants"]
    assert report[LAYA_CONTRACT]["validity"]["rate"] == 0.0 and report[LAYA_CONTRACT]["executed_invalid"] == 0
    assert report["laya-unsure"]["abstention"]["false_abstention"]["rate"] == 1.0
    assert report["laya-unsure"]["proposal_accuracy"]["rate"] == 1.0
    assert report["laya-good"]["accuracy"]["rate"] == 1.0


def test_policy_contestant_uses_the_session_policy_seam():
    policy = FixedPolicy(Decision("scroll", 0.9, direction="down"))
    report = run_cases([PolicyContestant(policy)], [_case("a", "scroll", value="down")])
    assert report["contestants"][SMALLER_GNSIS]["accuracy"]["rate"] == 1.0


def test_load_cases_reads_collected_rows_and_native_rows_without_leaking_labels(tmp_path):
    Image.new("RGB", (100, 50)).save(tmp_path / "s0.jpg")
    rows = [
        {"episode": "ep", "family": "search", "goal": GOAL, "history": [], "frame": "s0.jpg", "motion": 0.4,
         "action": "click", "box": [1, 1, 10, 10], "value": None, "viewport": [100, 50]},
        {"case_id": "n1", "goal": GOAL, "frames": [{"path": "s0.jpg", "frame_id": "x", "captured_at_ms": 5}],
         "oracle": {"action": "type", "value": "red shoes", "box": {"x": 1, "y": 1, "width": 5, "height": 5}}},
    ]
    (tmp_path / "states.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    collected, native = load_cases(tmp_path / "states.jsonl")
    assert collected.observation.case_id == "ep#0" and collected.observation.stream_motion() == 0.4
    assert collected.oracle == Oracle("click", None, Box(1, 1, 10, 10)) and collected.family == "search"
    assert native.observation.current.frame_id == "x" and native.oracle.value == "red shoes"


def test_calibration_and_paired_difference():
    cal = calibration([(0.9, True), (0.9, True), (0.1, False), (0.6, False)])
    assert cal["n"] == 4 and cal["ece"] == pytest.approx((2 * 0.1 + 0.1 + 0.6) / 4)
    assert cal["coverage"][-1] == {"threshold": 0.9, "coverage": 0.5, "selective_accuracy": 1.0}
    paired = paired_difference([True, True, False], [True, False, False])
    assert paired["difference"] == pytest.approx(1 / 3) and paired["only_candidate"] == 1
    with pytest.raises(ValueError):
        paired_difference([True], [])


def _strong(n=600):
    contestant = {
        "cases": n,
        "validity": {"rate": 1.0},
        "executed_invalid": 0,
        "calibration": {"ece": 0.05},
        "abstention": {"should_wait_recall": {"rate": 0.95}, "false_abstention": {"rate": 0.05}},
        "latency_ms": {"p95": 40.0},
    }
    laya = {**contestant, "latency_ms": {"p95": 900.0}}
    cases = {
        "contestants": {SMALLER_GNSIS: contestant, LAYA_CONTRACT: laya},
        "paired": {"accuracy": {"lower_95": 0.01}, "grounding": {"lower_95": -0.01}},
    }
    episodes = {
        "contestants": {SMALLER_GNSIS: {"false_done": 0}},
        "paired": {"task_success": {"n": 120, "lower_95": 0.0}},
    }
    return cases, episodes


def test_retirement_gate_needs_every_measurement():
    cases, episodes = _strong()
    assert retirement_gate(cases, episodes)["passed"]
    missing = retirement_gate(cases, None)
    assert not missing["passed"]
    assert {c["name"] for c in missing["checks"] if not c["passed"]} == {
        "paired_episodes", "task_success_vs_laya_lower_95", "false_done"
    }
    assert not retirement_gate({}, None)["passed"]
    worse = retirement_gate({**cases, "paired": {"accuracy": {"lower_95": -0.05}, "grounding": {"lower_95": 0.0}}}, episodes)
    assert [c["name"] for c in worse["checks"] if not c["passed"]] == ["accuracy_vs_laya_lower_95"]
    assert not retirement_gate(*_strong(100), RetirementThresholds(min_cases=500))["passed"]


# ---------------------------------------------------------------- episodes


class ButtonEnv:
    """A page with one button; clicking inside it turns the page dark."""

    episode_id = "button"
    goal = "press the button"
    max_steps = 4

    def __init__(self):
        self.screen_frames = LatestScreenFrameBuffer(max_history_frames=8)
        self.clock = 1000
        self.pressed = False
        self.steps = []
        self._publish()

    def _publish(self):
        self.clock += 2000
        _consume(self.screen_frames, _frame(f"b{self.clock}", self.clock, 0 if self.pressed else 255, size=(100, 50)))

    def execute(self, step):
        self.steps.append(step)
        if step.action == "click" and 40 <= step.target.x <= 60:
            self.pressed = True
        self._publish()
        return ExecutionReport(execution=Execution(actuator_success=True), acted_at_ms=self.clock)

    def succeeded(self):
        return self.pressed


def test_episode_runs_inside_the_production_session_and_scores_outcome_only_at_the_end():
    def press(observation, legal):
        dark = observation.current.image.getpixel((0, 0))[0] == 0
        return Decision("done" if dark else "click", 0.9, None if dark else Target(50, 25), frame_id=legal.frame_id)

    env = ButtonEnv()
    result = run_episode(RecordingContestant(SMALLER_GNSIS, press), env)
    assert result.success and result.claimed_done and result.steps == 1
    assert [s.action for s in env.steps] == ["click"]


def test_episode_repeat_guard_stops_a_stuck_policy_from_hammering():
    def miss(observation, legal):
        return Decision("click", 0.9, Target(5, 5), frame_id=legal.frame_id)

    env = ButtonEnv()
    result = run_episode(RecordingContestant(SMALLER_GNSIS, miss), env)
    assert not result.success and result.abstained == 3
    assert [s.action for s in env.steps] == ["click", "wait", "wait", "wait"]
