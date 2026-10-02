from gnsis_runtime.visual.policy_benchmark import BenchmarkCase, PolicyResult, score
from gnsis_runtime.visual.real_runs import Box, Point


def test_policy_payload_excludes_oracle_labels():
    case = BenchmarkCase(
        case_id="search",
        frame_id="f-1",
        goal="Search for 'GNSIS'",
        image_ref="frames/search.png",
        allowed_actions=("click", "type", "wait"),
        expected_action="type",
        target_box=Box(10, 20, 100, 40),
    )

    assert case.policy_input() == {
        "case_id": "search",
        "frame_id": "f-1",
        "goal": "Search for 'GNSIS'",
        "image_ref": "frames/search.png",
        "allowed_actions": ["click", "type", "wait"],
    }


def test_common_score_covers_laya_retirement_metrics():
    cases = (
        BenchmarkCase(
            "search",
            "f-1",
            "Search for 'GNSIS'",
            "frames/search.png",
            ("click", "type", "wait"),
            "type",
            target_box=Box(10, 20, 100, 40),
        ),
        BenchmarkCase(
            "loading",
            "f-2",
            "Wait for results",
            "frames/loading.png",
            ("click", "wait"),
            "wait",
            expect_abstain=True,
        ),
    )
    results = (
        PolicyResult("search", "f-1", "type", 0.9, Point(40, 30), True, True),
        PolicyResult("loading", "f-2", "wait", 0.3),
    )

    report = score(cases, results)

    assert report["missing"] == []
    assert all(
        metric["rate"] == 1.0
        for metric in report["metrics"].values()
        if metric["total"]
    )
