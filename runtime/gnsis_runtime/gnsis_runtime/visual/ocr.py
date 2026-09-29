"""Pixel-only text reading and target snapping for the JEV target pointer.

A small OCR model (RapidOCR / PP-OCR, ONNX) reads visible text boxes from the
rendered frame. The head still chooses the action and a coarse target
distribution; snapping only refines *where* inside the viewport the actuator
lands, using text that is visibly rendered. No DOM information is involved.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:  # tensors are only indexed here; reading text needs no torch
    import torch

MAX_LABEL_WORDS = 5
MIN_MATCH = 0.85
NEAR_CELLS = 0.75
_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class TextBox:
    text: str
    box: tuple[float, float, float, float]  # x, y, w, h in viewport pixels
    score: float

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2


def normalize(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


def goal_match(text: str, goal: str) -> float:
    """Fraction of the (normalised) label found verbatim in the goal; 0 below MIN_MATCH."""
    t, g = normalize(text), normalize(goal)
    if len(t) < 2 or len(t.split()) > MAX_LABEL_WORDS:
        return 0.0
    m = SequenceMatcher(None, t, g, autojunk=False).find_longest_match(0, len(t), 0, len(g))
    q = m.size / len(t)
    return q if q >= MIN_MATCH else 0.0


class TextReader:
    def __init__(self) -> None:
        from rapidocr import RapidOCR

        self.engine = RapidOCR(params={"Global.log_level": "error"})

    def read(self, image: Image.Image, region: tuple[int, int, int, int] | None = None) -> list[TextBox]:
        """Read text boxes from the frame (optionally a crop `region` = x0, y0, x1, y1)."""
        ox, oy = (region[0], region[1]) if region else (0, 0)
        img = image.convert("RGB")
        if region:
            img = img.crop(region)
        out = self.engine(np.asarray(img))
        if out.boxes is None:
            return []
        boxes = []
        for quad, text, score in zip(out.boxes, out.txts, out.scores, strict=True):
            xs, ys = quad[:, 0], quad[:, 1]
            x0, y0 = float(xs.min()) + ox, float(ys.min()) + oy
            boxes.append(TextBox(text, (x0, y0, float(xs.max()) + ox - x0, float(ys.max()) + oy - y0), float(score)))
        return boxes

    def timed_read(self, image: Image.Image, region: tuple[int, int, int, int] | None = None):
        t0 = time.perf_counter()
        boxes = self.read(image, region)
        return boxes, (time.perf_counter() - t0) * 1e3


def box_heat(
    box: tuple[float, float, float, float], p_target: torch.Tensor, grid: tuple[int, int], viewport: tuple[int, int]
) -> float:
    """Target-pointer probability mass over token cells whose centre lies near the box."""
    rows, cols = grid
    cw, ch = viewport[0] / cols, viewport[1] / rows
    x, y, w, h = box
    c0, c1 = int((x - cw / 2) // cw), int((x + w + cw / 2) // cw)
    r0, r1 = int((y - ch / 2) // ch), int((y + h + ch / 2) // ch)
    total = 0.0
    for r in range(max(0, r0), min(rows - 1, r1) + 1):
        for c in range(max(0, c0), min(cols - 1, c1) + 1):
            ux, uy = (c + 0.5) * cw, (r + 0.5) * ch
            if x - cw / 2 <= ux <= x + w + cw / 2 and y - ch / 2 <= uy <= y + h + ch / 2:
                total += float(p_target[r * cols + c])
    return total


def snap_target(
    point: tuple[int, int],
    action: str,
    goal: str,
    p_target: torch.Tensor,
    grid: tuple[int, int],
    viewport: tuple[int, int],
    boxes: list[TextBox],
    use_goal: bool = True,
) -> tuple[tuple[int, int], str]:
    """Refine a coarse target point with rendered text boxes.

    click: prefer a short visible label that appears in the goal, weighted by the
    pointer's probability mass around it, otherwise the nearest short label.
    recover: snap to the nearest short label (dismiss buttons are text buttons).
    type: keep the pointer; visible field labels usually sit outside the input.
    """
    if action == "type":
        return point, "pointer"
    labels = [b for b in boxes if 0 < len(normalize(b.text).split()) <= MAX_LABEL_WORDS]
    if action == "click" and use_goal:
        scored = [(goal_match(b.text, goal) * (box_heat(b.box, p_target, grid, viewport) + 1e-3), b) for b in labels]
        scored = [s for s in scored if s[0] > 0]
        if scored:
            cx, cy = max(scored, key=lambda s: s[0])[1].center
            return (int(cx), int(cy)), "goal_text"
    cw, ch = viewport[0] / grid[1], viewport[1] / grid[0]
    best, best_d = None, float("inf")
    for b in labels:
        x, y, w, h = b.box
        dx = max(x - point[0], 0.0, point[0] - (x + w)) / cw
        dy = max(y - point[1], 0.0, point[1] - (y + h)) / ch
        d = (dx * dx + dy * dy) ** 0.5
        if d < best_d:
            best, best_d = b, d
    if best is not None and best_d <= NEAR_CELLS:
        cx, cy = best.center
        return (int(cx), int(cy)), "near_text"
    return point, "pointer"
