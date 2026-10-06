from __future__ import annotations

import json

from PIL import Image

from gnsis_runtime.visual.grounding import (
    OCR_CONFIDENCE,
    Florence2Grounder,
    GroundingRouter,
    box_to_viewport,
    crop_around,
    ground_from_elements,
    parse_qwen_grounding,
    text_tiles,
)
from gnsis_runtime.visual.perception import PerceivedElement, TargetGrounding


def _grounding(status: str, confidence: float, source: str) -> TargetGrounding:
    return TargetGrounding(
        point=(5, 5),
        status=status,
        label=source,
        text="",
        box=(0, 0, 10, 10) if status == "grounded" else None,
        confidence=confidence,
        source=source,
    )


class StubGrounder:
    def __init__(self, name: str, result: TargetGrounding | Exception) -> None:
        self.name = name
        self.result = result
        self.calls = 0

    def ground(self, image, point, focus=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_crop_keeps_the_point_inside_a_bounded_window_at_the_edges() -> None:
    image = Image.new("RGB", (1280, 720))

    corner = crop_around(image, (5, 700), 640)
    assert corner.image.size == (640, 640)
    assert corner.origin == (0, 80)
    assert corner.point == (5, 620)

    centre = crop_around(image, (640, 360), 640)
    assert centre.origin == (320, 40)
    assert centre.point == (320, 320)

    small = crop_around(Image.new("RGB", (200, 100)), (150, 50), 640)
    assert small.image.size == (200, 100)
    assert small.origin == (0, 0)


def test_boxes_map_from_crop_space_back_into_the_viewport() -> None:
    assert box_to_viewport((10.0, 20.0, 110.0, 70.0), (300, 40), (1280, 720)) == (
        310,
        60,
        100,
        50,
    )
    assert box_to_viewport((-50, -50, 2000, 2000), (0, 0), (1280, 720)) == (
        0,
        0,
        1280,
        720,
    )
    assert box_to_viewport((1, 2, 3), (0, 0), (1280, 720)) is None
    assert box_to_viewport(("a", 0, 1, 1), (0, 0), (1280, 720)) is None


def test_rolling_perception_grounds_the_smallest_element_under_the_point() -> None:
    page = PerceivedElement("Page", "window", "", (0, 0, 1280, 720), "", 0.9)
    button = PerceivedElement("Save", "button", "Save", (100, 100, 80, 30), "", 0.8)

    hit = ground_from_elements((page, button), (120, 110), "minicpm")
    assert hit.status == "grounded"
    assert hit.label == "Save"
    assert hit.box == (100, 100, 80, 30)
    assert hit.source == "minicpm"

    miss = ground_from_elements((button,), (5, 5), "minicpm")
    assert miss.status == "unresolved"
    assert miss.box is None
    assert miss.confidence == 0.0


def test_router_only_consults_the_fallback_when_the_primary_is_weak() -> None:
    image = Image.new("RGB", (64, 64))
    strong = StubGrounder("primary", _grounding("grounded", 0.8, "primary"))
    fallback = StubGrounder("fallback", _grounding("grounded", 0.9, "fallback"))

    result = GroundingRouter(strong, fallback).ground(image, (5, 5))
    assert result.source == "primary"
    assert fallback.calls == 0

    weak = StubGrounder("primary", _grounding("unresolved", 0.0, "primary"))
    result = GroundingRouter(weak, fallback).ground(image, (5, 5))
    assert result.source == "fallback"
    assert fallback.calls == 1


def test_router_reports_failures_instead_of_raising_and_keeps_verified_boxes() -> None:
    image = Image.new("RGB", (64, 64))
    broken = StubGrounder("primary", RuntimeError("cuda gone"))
    unsure = StubGrounder("fallback", _grounding("unresolved", 0.2, "fallback"))

    result = GroundingRouter(broken, unsure).ground(image, (5, 5))
    assert result.status == "unresolved"
    assert result.source == "fallback"

    nothing = GroundingRouter(broken).ground(image, (5, 5))
    assert nothing.status == "failed"
    assert nothing.box is None
    assert nothing.confidence == 0.0

    low = StubGrounder("primary", _grounding("grounded", 0.4, "primary"))
    result = GroundingRouter(low, unsure).ground(image, (5, 5))
    assert result.status == "grounded"
    assert result.source == "primary"


def test_fallback_json_box_must_contain_the_point_to_count_as_a_location() -> None:
    image = Image.new("RGB", (1280, 720))
    crop = crop_around(image, (900, 400), 640)
    assert crop.origin == (580, 80)

    grounded = parse_qwen_grounding(
        json.dumps(
            {
                "answer": "Anywhere",
                "box": [400, 450, 600, 550],
                "confidence": 0.9,
                "evidence": "text",
            }
        ),
        point=(900, 400),
        crop=crop,
        viewport=image.size,
        source="qwen",
    )
    assert grounded.status == "grounded"
    assert grounded.box == (836, 368, 128, 64)
    assert grounded.text == "Anywhere"
    assert grounded.confidence == 0.9

    elsewhere = parse_qwen_grounding(
        'Sure: {"answer": "hot tub", "box": [0, 0, 100, 100], "confidence": 0.95}',
        point=(900, 400),
        crop=crop,
        viewport=image.size,
        source="qwen",
    )
    assert elsewhere.status == "unresolved"
    assert elsewhere.box is None
    assert elsewhere.label == "hot tub"
    assert elsewhere.confidence <= 0.3

    garbage = parse_qwen_grounding(
        "no json here", point=(900, 400), crop=crop, viewport=image.size, source="qwen"
    )
    assert garbage.status == "unresolved"
    assert garbage.confidence == 0.0


class FakeFlorence(Florence2Grounder):
    def __init__(self, dense, ocr) -> None:
        super().__init__(device="cpu", dtype="float32", crop_size=640)
        self.dense = dense
        self.ocr = ocr
        self.images: list[Image.Image] = []

    def _load(self) -> None:
        return None

    def _run(self, task, image, max_new_tokens=512):
        self.images.append(image)
        return self.dense if task == "<DENSE_REGION_CAPTION>" else self.ocr


def test_florence_reads_text_under_the_point_and_maps_boxes_to_the_viewport() -> None:
    image = Image.new("RGB", (1280, 720))
    grounder = FakeFlorence(
        dense={
            "bboxes": [[0, 0, 640, 640], [100, 100, 500, 400]],
            "labels": ["web page", "search bar"],
        },
        ocr={
            "quad_boxes": [[280, 310, 380, 310, 380, 330, 280, 330]],
            "labels": ["Anywhere</s>"],
        },
    )

    result = grounder.ground(image, (900, 400))

    assert grounder.images[0].size == (640, 640)
    assert result.status == "grounded"
    assert result.text == "Anywhere"
    assert result.label == "search bar"
    assert result.box == (860, 390, 100, 20)
    assert result.confidence == 0.8
    assert result.source == "florence-2-large-ft"


def test_florence_reports_unresolved_when_nothing_contains_the_point() -> None:
    image = Image.new("RGB", (1280, 720))
    grounder = FakeFlorence(
        dense={"bboxes": [[0, 0, 50, 50]], "labels": ["logo"]},
        ocr={"quad_boxes": [], "labels": []},
    )

    result = grounder.ground(image, (900, 400))

    assert result.status == "unresolved"
    assert result.box is None
    assert result.confidence == 0.0


def test_florence_reads_full_frame_text_per_tile_in_reading_order() -> None:
    image = Image.new("RGB", (1280, 942))
    grounder = FakeFlorence(
        dense={"bboxes": [], "labels": []},
        ocr={
            "quad_boxes": [
                [100, 300, 200, 300, 200, 320, 100, 320],
                [10, 10, 90, 10, 90, 30, 10, 30],
            ],
            "labels": ["SIN.</s>", "FEAR  OF GOD</s>"],
        },
    )

    regions = grounder.read_text(image)

    assert [crop.size for crop in grounder.images] == [(640, 471)] * 4
    assert [region.text for region in regions[:2]] == ["FEAR OF GOD", "FEAR OF GOD"]
    assert regions[0].box == (10, 10, 80, 20)
    assert regions[1].box == (650, 10, 80, 20)
    assert regions[-1].box == (740, 771, 100, 20)
    assert {region.role for region in regions} == {"text"}
    assert {region.confidence for region in regions} == {OCR_CONFIDENCE}


def test_text_tiles_cover_the_frame_without_exceeding_the_tile_size() -> None:
    tiles = text_tiles((1280, 942), 768)

    assert tiles == (
        (0, 0, 640, 471),
        (640, 0, 1280, 471),
        (0, 471, 640, 942),
        (640, 471, 1280, 942),
    )
    assert text_tiles((640, 480), 768) == ((0, 0, 640, 480),)
