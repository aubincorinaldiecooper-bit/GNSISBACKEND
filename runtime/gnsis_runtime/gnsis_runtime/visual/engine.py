"""Panoptic perception and optional grounded visual decisions.

PanopticPolicy is the model-independent perception seam used by the runtime.
JEVEngine also implements the separate DecisionProvider interface.
Environment-specific capture and execution are deliberately outside this module.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np
import torch
from PIL import Image

from .backbone import BackboneConfig, MiniCPMVBackbone, VisualTokens
from .batching import HEAD_INPUTS, collate
from .decode import decode
from .grounding import TargetGrounder, failed, ground_from_elements
from .head import HeadConfig, JEVDecisionHead
from .perception import (
    TargetGrounding,
    VisualPerception,
    build_perception_prompt,
    parse_perception,
    validate_target_point,
)
from .prompt import build_layout
from .schema import Decision, validate_decision
from .verify_vlm import minicpmv_generator

REUSE_DISTANCE = 0.002
log = logging.getLogger(__name__)


class VisualFrame(Protocol):
    frame_id: str | int

    def image(self) -> Image.Image: ...

    @property
    def signature(self) -> np.ndarray: ...


class DecisionProvider(Protocol):
    name: str

    def decide(
        self,
        frame: VisualFrame,
        goal: str,
        history: list[dict],
        motion: float,
        viewport: tuple[int, int],
        cache: "VisualCache",
        allowed_actions: tuple[str, ...] | None = None,
    ) -> Decision: ...

    def encode(self, frame: VisualFrame, cache: "VisualCache") -> None: ...


class PanopticPolicy(Protocol):
    name: str

    def perceive(
        self,
        frames: Sequence[VisualFrame],
        motion: float,
        viewport: tuple[int, int],
        focus: str | None = None,
        target: tuple[int, int] | None = None,
    ) -> VisualPerception: ...


@dataclass
class VisualCache:
    """Per-session cache of the last encoded frame; reused while visual state is unchanged."""

    signature: np.ndarray | None = None
    frame_id: str | int | None = None
    tokens: VisualTokens | None = None
    hits: int = 0
    misses: int = 0

    def lookup(self, frame: VisualFrame) -> VisualTokens | None:
        if self.tokens is None or self.signature is None:
            return None
        if (
            frame.frame_id == self.frame_id
            or frame_distance(frame.signature, self.signature) < REUSE_DISTANCE
        ):
            return self.tokens
        return None

    def store(self, frame: VisualFrame, tokens: VisualTokens) -> None:
        self.signature, self.frame_id, self.tokens = (
            frame.signature,
            frame.frame_id,
            tokens,
        )


def frame_signature(image: Image.Image, size: tuple[int, int] = (48, 30)) -> np.ndarray:
    """Small grayscale perceptual signature used only for cache reuse."""

    return (
        np.asarray(image.convert("L").resize(size, Image.BILINEAR), dtype=np.float32)
        / 255.0
    )


def frame_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


@dataclass
class RuntimeVisualFrame:
    """Adapter for GNSIS persistent ScreenFrame instances.

    The runtime owns capture/history. This adapter derives the cache signature from
    the already-captured frame; it never reacquires the screen.
    """

    frame_id: str | int
    _image: Image.Image
    signature: np.ndarray

    @classmethod
    def from_image(
        cls, frame_id: str | int, image: Image.Image
    ) -> "RuntimeVisualFrame":
        rgb = image.convert("RGB")
        return cls(frame_id=frame_id, _image=rgb, signature=frame_signature(rgb))

    def image(self) -> Image.Image:
        return self._image


class JEVEngine:
    name = "minicpm-v-4.6+jev-head"

    def __init__(
        self,
        backbone: BackboneConfig,
        head_path: str,
        grounder: TargetGrounder | None = None,
    ):
        self.backbone = MiniCPMVBackbone(backbone)
        ckpt = torch.load(head_path, map_location="cpu", weights_only=False)
        self.head = JEVDecisionHead(HeadConfig(**ckpt["config"])).eval()
        self.head.load_state_dict(ckpt["state_dict"])
        self._lock = threading.Lock()
        self._generate_perception = minicpmv_generator(
            self.backbone,
            lock=self._lock,
            max_new_tokens=640,
        )
        self.grounder = grounder
        self._grounding_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="panoptic-grounding"
        )

    def encode(self, frame: VisualFrame, cache: VisualCache) -> None:
        with self._lock:
            if cache.lookup(frame) is None:
                cache.store(frame, self.backbone.encode_visual(frame.image()))

    def decide(
        self,
        frame: VisualFrame,
        goal: str,
        history: list[dict],
        motion: float,
        viewport: tuple[int, int],
        cache: VisualCache,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> Decision:
        with self._lock:
            t0 = time.perf_counter()
            visual = cache.lookup(frame)
            if visual is None:
                cache.misses += 1
                visual = self.backbone.encode_visual(frame.image())
                cache.store(frame, visual)
                vision_ms = visual.timing_ms["vision"] + visual.timing_ms["preprocess"]
            else:
                cache.hits += 1
                vision_ms = 0.0
            layout = build_layout(goal, history)
            feats = self.backbone.decision_features(layout, visual)
            t1 = time.perf_counter()
            record = {
                "queries": feats.queries,
                "actions": feats.actions,
                "values": feats.values,
                "visual": feats.visual,
                "visual_embeds": feats.visual_embeds,
                "grid": feats.grid,
                "motion": motion,
            }
            batch = collate([record])
            with torch.inference_mode():
                out = self.head(**{k: batch[k] for k in HEAD_INPUTS})
            decision = decode(
                out,
                layout.values,
                feats.grid,
                viewport,
                allowed_actions=allowed_actions,
            )
            t2 = time.perf_counter()
        timing = {
            "vision": round(vision_ms, 1),
            "language": round(feats.timing_ms["language"], 1),
            "head": round((t2 - t1) * 1e3, 2),
            "total": round((t2 - t0) * 1e3, 1),
        }
        return validate_decision(
            replace(decision, frame_id=frame.frame_id, timing_ms=timing),
            viewport,
        )

    def perceive(
        self,
        frames: Sequence[VisualFrame],
        motion: float,
        viewport: tuple[int, int],
        focus: str | None = None,
        target: tuple[int, int] | None = None,
    ) -> VisualPerception:
        selected = tuple(frames[-4:])
        if not selected:
            raise ValueError("Panoptic perception requires a current frame")
        point = None if target is None else validate_target_point(target, viewport)
        grounding: Future[TargetGrounding] | None = None
        if point is not None and self.grounder is not None:
            grounding = self._grounding_pool.submit(
                self.grounder.ground, selected[-1].image(), point, focus
            )
        raw = self._generate_perception(
            tuple(frame.image() for frame in selected),
            build_perception_prompt(
                viewport,
                temporal=len(selected) > 1,
                focus=focus,
                target=point,
            ),
        )
        perception = parse_perception(
            raw,
            frame_id=str(selected[-1].frame_id),
            observed_frame_ids=tuple(str(frame.frame_id) for frame in selected),
            motion=motion,
            viewport=viewport,
        )
        if point is None:
            return perception
        if grounding is None:
            return replace(
                perception,
                grounding=ground_from_elements(perception.elements, point, self.name),
            )
        try:
            return replace(perception, grounding=grounding.result())
        except Exception:
            log.exception("target grounding failed at %s", point)
            return replace(perception, grounding=failed(point, self.grounder.name))
