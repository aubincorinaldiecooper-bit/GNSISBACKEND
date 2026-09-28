"""Training/eval labels for the decision head, derived from oracle states."""

from __future__ import annotations

import torch

from .prompt import ValueCandidate
from .schema import ACTIONS

VALUE_KIND = {"type": "text", "navigate": "url", "scroll": "direction"}


def value_label(action: str, value: str | None, values: list[ValueCandidate]) -> int:
    kind = VALUE_KIND.get(action)
    if kind is None:
        return 0  # the "none" candidate
    for i, cand in enumerate(values):
        if cand.value == value and (cand.kind == kind or (kind == "text" and cand.kind == "text")):
            return i
    return -1


def target_labels(
    box: tuple[float, float, float, float] | None,
    grid: tuple[int, int],
    viewport: tuple[int, int],
) -> tuple[torch.Tensor, torch.Tensor]:
    """Positive token cells (centre inside the box, or the cell containing the box centre)
    and per-cell offset (in cell units) from the cell centre to the box centre."""
    rows, cols = grid
    pos = torch.zeros(rows * cols, dtype=torch.bool)
    offset = torch.zeros(rows * cols, 2)
    if box is None:
        return pos, offset
    width, height = viewport
    x, y, w, h = box
    x0, y0 = max(0.0, x) / width, max(0.0, y) / height
    x1, y1 = min(width, x + w) / width, min(height, y + h) / height
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for r in range(rows):
        for c in range(cols):
            ux, uy = (c + 0.5) / cols, (r + 0.5) / rows
            i = r * cols + c
            if x0 <= ux <= x1 and y0 <= uy <= y1:
                pos[i] = True
            offset[i, 0] = (cx - ux) * cols
            offset[i, 1] = (cy - uy) * rows
    pos[min(rows - 1, int(cy * rows)) * cols + min(cols - 1, int(cx * cols))] = True
    return pos, offset


def action_label(action: str) -> int:
    return ACTIONS.index(action)
