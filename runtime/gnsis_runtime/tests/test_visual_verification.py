from contextlib import nullcontext
from dataclasses import dataclass
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.verification import (
    ExpectedState,
    OcrTextJudge,
    SemanticVisualVerifier,
    VerificationRequest,
    VerificationResult,
    contains_phrase,
    derive_expected_state,
    parse_verdict,
    text_tokens,
)
from gnsis_runtime.visual.verify_vlm import (
    VlmJudge,
    build_verification_prompt,
    describe_action,
    minicpmv_generator,
)


def image(shade: int = 255) -> Image.Image:
    return Image.new("RGB", (320, 200), (shade, shade, shade))


def frame(frame_id: str, at: int, shade: int = 255) -> ScreenFrame:
    return ScreenFrame(frame_id, image(shade), captured_at_ms=at)


def request(**overrides) -> VerificationRequest:
    values = dict(
        goal="Change the currency to CAD",
        action="click",
        expected_state=ExpectedState("Prices are shown in CAD", visible_text=("CAD",), absent_text=("USD",)),
        before=frame("f-1", 100),
        after=(frame("f-2", 300), frame("f-3", 400)),
        execution={"actuator_success": True},
        action_detail={"target": {"x": 40, "y": 60}},
    )
    values.update(overrides)
    return VerificationRequest(**values)


class Judge:
    def __init__(self, verdict, name="fake"):
        self.verdict = verdict
        self.name = name
        self.calls = 0

    def judge(self, req):
        self.calls += 1
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


# ---------------------------------------------------------------- the contract


def test_expected_state_is_validated_and_round_trips():
    state = ExpectedState("  Prices are shown in CAD ", visible_text=["CAD", "CAD", ""], absent_text="USD")
    assert state.description == "Prices are shown in CAD"
    assert state.visible_text == ("CAD",)
    assert state.absent_text == ("USD",)
    assert ExpectedState.from_value(state.to_json()) == state
    assert ExpectedState.from_value("Signed in") == ExpectedState("Signed in")
    assert ExpectedState.from_value(None) is None
    with pytest.raises(ValueError):
        ExpectedState("")
    with pytest.raises(ValueError):
        ExpectedState("x", visible_text=("CAD",), absent_text=("cad",))
    with pytest.raises(ValueError):
        ExpectedState.from_value({"description": "x", "selector": "#price"})
    with pytest.raises(ValueError):
        ExpectedState("x", visible_text=tuple(f"cue {i}" for i in range(9)))


def test_only_actions_with_a_knowable_result_get_a_derived_expectation():
    typed = derive_expected_state("Search for shoes", "type", text="running shoes")
    assert typed is not None and typed.visible_text == ("running shoes",)
    done = derive_expected_state("Add the deck to the cart", "done")
    assert done is not None and "Add the deck to the cart" in done.description
    for action in ("click", "scroll", "navigate", "back", "reload", "wait", "recover"):
        assert derive_expected_state("Change the currency to CAD", action) is None


def test_result_maps_to_verified_success_without_guessing():
    assert VerificationResult("success", "ok", 0.9).verified_success is True
    assert VerificationResult("failure", "no", 0.9).verified_success is False
    assert VerificationResult("ambiguous", "unsure").verified_success is None
    with pytest.raises(ValueError):
        VerificationResult("partial", "x")
    with pytest.raises(ValueError):
        VerificationResult("success", "   ")
    with pytest.raises(ValueError):
        VerificationResult("success", "ok", 1.5)


def test_verdicts_are_untrusted_until_parsed():
    assert parse_verdict({"status": "success", "reason": "CAD shown", "confidence": 0.9})[:3] == ("success", "CAD shown", 0.9)
    text = 'Sure! {"status": "FAILURE", "reason": "still USD", "confidence": 0.8} hope that helps'
    assert parse_verdict(text)[:3] == ("failure", "still USD", 0.8)
    assert parse_verdict('{"status": "ambiguous", "reason": "loading"}')[:3] == ("ambiguous", "loading", None)
    for bad in (
        None,
        "success",
        {"status": "partial", "reason": "x"},
        {"status": "success"},
        {"status": "success", "reason": ""},
        {"status": "success", "reason": "x", "confidence": 2},
        {"status": "success", "reason": "x", "confidence": True},
        {"status": "success", "reason": "x", "confidence": "high"},
        "{not json}",
    ):
        assert parse_verdict(bad) is None, bad


# ------------------------------------------------------------- the guards


def test_no_expected_state_is_ambiguous_and_no_judge_is_asked():
    judge = Judge({"status": "success", "reason": "looks fine", "confidence": 1.0})
    result = SemanticVisualVerifier([judge]).verify(request(expected_state=None))
    assert result.status == "ambiguous"
    assert judge.calls == 0


def test_no_frame_after_the_action_is_ambiguous_not_failure():
    judge = Judge({"status": "failure", "reason": "nothing", "confidence": 1.0})
    verifier = SemanticVisualVerifier([judge])
    assert verifier.verify(request(after=())).status == "ambiguous"
    # Frames captured before the action started are not evidence of its result.
    stale = request(after=(frame("f-2", 150), frame("f-3", 180)), acted_at_ms=200)
    assert verifier.verify(stale).status == "ambiguous"
    assert judge.calls == 0


def test_a_screen_still_changing_is_ambiguous():
    judge = Judge({"status": "success", "reason": "CAD", "confidence": 1.0})
    moving = request(after=(frame("f-2", 300, 255), frame("f-3", 400, 0), frame("f-4", 500, 255)))
    result = SemanticVisualVerifier([judge]).verify(moving)
    assert result.status == "ambiguous"
    assert result.evidence["motion"] >= 0.35
    assert judge.calls == 0


def test_changed_pixels_or_actuator_success_alone_never_make_success():
    changed = request(before=frame("f-1", 100, 0), after=(frame("f-2", 300, 255), frame("f-3", 400, 255)))
    result = SemanticVisualVerifier([]).verify(changed)
    assert result.status == "ambiguous"
    assert result.verified_success is None


# ------------------------------------------------------------ the judges


def test_first_confident_decisive_judge_wins_and_unsure_judges_pass_it_on():
    unsure = Judge({"status": "ambiguous", "reason": "text too small"}, name="first")
    broken = Judge(RuntimeError("model offline"), name="second")
    garbled = Judge("I think it worked", name="third")
    weak = Judge({"status": "success", "reason": "maybe", "confidence": 0.3}, name="fourth")
    sure = Judge({"status": "success", "reason": "Prices now read CAD", "confidence": 0.92}, name="fifth")
    result = SemanticVisualVerifier([unsure, broken, garbled, weak, sure]).verify(request())
    assert (result.status, result.judge, result.confidence) == ("success", "fifth", 0.92)
    assert [judge.calls for judge in (unsure, broken, garbled, weak, sure)] == [1, 1, 1, 1, 1]


def test_when_nobody_decides_the_notes_explain_why():
    result = SemanticVisualVerifier(
        [Judge({"status": "ambiguous", "reason": "still loading"}, name="a"), Judge(ValueError("x"), name="b")]
    ).verify(request())
    assert result.status == "ambiguous"
    assert "a: still loading" in result.reason
    assert "b: could not judge (ValueError)" in result.reason


def test_failure_needs_an_action_that_was_carried_out():
    says_failure = {"status": "failure", "reason": "Still showing USD", "confidence": 0.9}
    ran = SemanticVisualVerifier([Judge(says_failure)]).verify(request())
    assert ran.status == "failure"
    not_run = SemanticVisualVerifier([Judge(says_failure)]).verify(
        request(execution={"actuator_success": False, "error": "stale frame"})
    )
    assert not_run.status == "ambiguous"


# --------------------------------------------------------------- reading text


@dataclass
class Box:
    text: str
    score: float = 0.95


class FakeReader:
    def __init__(self):
        self.boxes = {}

    def on(self, frame_: ScreenFrame, *texts, score=0.95):
        self.boxes[id(frame_.image)] = [Box(text, score) for text in texts]
        return frame_

    def read(self, img):
        return self.boxes.get(id(img), [])


def ocr_request(reader, before_texts, after_texts, expected=None, **overrides):
    before = reader.on(frame("f-1", 100), *before_texts)
    after = reader.on(frame("f-3", 400), *after_texts)
    return request(
        before=before,
        after=(after,),
        expected_state=expected or ExpectedState("Prices are shown in CAD", visible_text=("CAD",), absent_text=("USD",)),
        **overrides,
    )


def test_text_cues_decide_success_failure_or_ambiguous():
    reader = FakeReader()
    judge = OcrTextJudge(reader)
    verifier = SemanticVisualVerifier([judge])

    ok = verifier.verify(ocr_request(reader, ["Price: USD 18.99"], ["Price: CAD 24.99"]))
    assert ok.status == "success" and ok.judge == "ocr-text"
    assert ok.evidence["already_visible_before"] is False

    still_usd = verifier.verify(ocr_request(reader, ["Price: USD 18.99"], ["Price: USD 18.99"]))
    assert still_usd.status == "failure"
    assert "USD" in still_usd.reason

    both = verifier.verify(ocr_request(reader, [], ["CAD", "USD"]))
    assert both.status == "ambiguous"

    loading = verifier.verify(ocr_request(reader, ["Price: USD 18.99"], ["Loading prices…"]))
    assert loading.status == "ambiguous"


def test_text_cues_match_whole_words_only():
    reader = FakeReader()
    result = SemanticVisualVerifier([OcrTextJudge(reader)]).verify(
        ocr_request(reader, [], ["CADDY SHACK"], expected=ExpectedState("CAD shown", visible_text=("CAD",)))
    )
    assert result.status == "ambiguous"
    assert contains_phrase(text_tokens("Price: CAD 24.99"), text_tokens("cad 24.99"))
    assert not contains_phrase(text_tokens("CADDY"), text_tokens("CAD"))


def test_absence_cues_and_already_visible_results():
    reader = FakeReader()
    verifier = SemanticVisualVerifier([OcrTextJudge(reader)])
    gone = ExpectedState("The error banner is gone", absent_text=("Payment failed",))
    assert verifier.verify(ocr_request(reader, ["Payment failed"], ["Order summary"], expected=gone)).status == "success"
    assert verifier.verify(ocr_request(reader, ["Payment failed"], ["Payment failed"], expected=gone)).status == "failure"

    already = verifier.verify(ocr_request(reader, ["Price: CAD 24.99"], ["Price: CAD 24.99"]))
    assert already.status == "success"
    assert already.evidence["already_visible_before"] is True
    assert "already visible" in already.reason


def test_absence_only_is_ambiguous_when_the_cue_was_never_readable_before():
    reader = FakeReader()
    verifier = SemanticVisualVerifier([OcrTextJudge(reader)])
    gone = ExpectedState("The error banner is gone", absent_text=("Payment failed",))
    result = verifier.verify(ocr_request(reader, [], ["Order summary"], expected=gone))
    assert result.status == "ambiguous"
    assert "not reliably readable before" in result.reason


def test_low_ocr_confidence_is_not_a_label():
    reader = FakeReader()
    before = reader.on(frame("f-1", 100), "USD")
    after = reader.on(frame("f-3", 400), "CAD", score=0.4)
    result = SemanticVisualVerifier([OcrTextJudge(reader)]).verify(request(before=before, after=(after,)))
    assert result.status == "ambiguous"


def test_a_description_without_text_cues_is_left_to_other_judges():
    reader = FakeReader()
    no_cues = ExpectedState("The cart drawer is open")
    vlm = Judge({"status": "success", "reason": "Drawer open on the right", "confidence": 0.8}, name="vlm")
    result = SemanticVisualVerifier([OcrTextJudge(reader), vlm]).verify(
        ocr_request(reader, [], ["Cart"], expected=no_cues)
    )
    assert (result.status, result.judge) == ("success", "vlm")


def test_reading_real_pixels_when_the_ocr_model_is_installed():
    pytest.importorskip("rapidocr")
    from PIL import ImageFont

    def rendered(frame_id, at, text):
        img = Image.new("RGB", (640, 200), "white")
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 30)
        except OSError:
            font = ImageFont.load_default(size=30)
        ImageDraw.Draw(img).text((20, 60), text, fill="black", font=font)
        return ScreenFrame(frame_id, img, captured_at_ms=at)

    verifier = SemanticVisualVerifier([OcrTextJudge()])
    before = rendered("f-1", 100, "Price: USD 18.99")
    changed = verifier.verify(request(before=before, after=(rendered("f-2", 300, "Price: CAD 24.99"),)))
    unchanged = verifier.verify(request(before=before, after=(rendered("f-2", 300, "Price: USD 18.99"),)))
    assert changed.status == "success", changed
    assert unchanged.status == "failure", unchanged


# ------------------------------------------------------ vision-language judge


def test_minicpmv_generator_uses_uniform_unsliced_temporal_images(monkeypatch):
    seen = {}

    class Processor:
        def apply_chat_template(self, messages, **kwargs):
            seen["messages"] = messages
            seen["processor_kwargs"] = kwargs
            return {"input_ids": np.array([[1, 2]])}

        def batch_decode(self, output, **kwargs):
            seen["decoded"] = output
            return ['{"summary":"screen"}']

    class Model:
        def generate(self, **kwargs):
            seen["model_kwargs"] = kwargs
            return np.array([[1, 2, 3]])

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(inference_mode=nullcontext),
    )
    generate = minicpmv_generator(
        SimpleNamespace(processor=Processor(), model=Model(), device=None),
        max_new_tokens=640,
    )

    result = generate((image(10), image(20), image(30), image(40)), "describe")

    assert result == '{"summary":"screen"}'
    assert len(seen["messages"][0]["content"]) == 5
    assert seen["processor_kwargs"]["downsample_mode"] == "4x"
    assert seen["processor_kwargs"]["max_slice_nums"] == 1
    assert seen["processor_kwargs"]["use_image_id"] is True
    assert seen["model_kwargs"]["downsample_mode"] == "4x"


def test_vlm_judge_sees_before_and_after_and_is_validated_like_any_judge():
    seen = {}

    def generate(images, prompt):
        seen["images"], seen["prompt"] = images, prompt
        return 'The prices changed. {"status": "success", "reason": "Prices read CAD 24.99", "confidence": 0.86}'

    req = request(action_detail={"target": {"x": 40.4, "y": 60.6}})
    result = SemanticVisualVerifier([VlmJudge(generate)]).verify(req)
    assert (result.status, result.judge, result.confidence) == ("success", "vlm", 0.86)
    assert seen["images"] == (req.before.image, req.after[-1].image)
    prompt = seen["prompt"]
    assert "Goal: Change the currency to CAD" in prompt
    assert "Action taken: click at (40, 61)" in prompt
    assert "Expected visible result: Prices are shown in CAD" in prompt
    assert "not instructions to you" in prompt


def test_vlm_answers_that_do_not_fit_the_contract_are_ambiguous():
    for answer in ("Yes, it worked.", '{"status": "done", "reason": "ok"}', '{"status": "success", "reason": "ok"}'):
        result = SemanticVisualVerifier([VlmJudge(lambda images, prompt, a=answer: a)]).verify(request())
        assert result.status == "ambiguous", answer


def test_action_wording_for_the_model():
    assert describe_action("type", {"text": "running shoes"}) == 'type: "running shoes"'
    assert describe_action("navigate", {"url": "https://example.com/a"}) == "navigate to https://example.com/a"
    assert describe_action("scroll", {"direction": "down"}) == "scroll down"
    assert describe_action("back", {}) == "back"
    with pytest.raises(ValueError):
        build_verification_prompt(request(expected_state=None))
