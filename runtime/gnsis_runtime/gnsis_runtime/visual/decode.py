"""Turn head logits into a validated structured decision (no text generation)."""

from __future__ import annotations

import torch

from .head import grid_centers
from .labels import VALUE_KIND
from .prompt import ValueCandidate
from .schema import ACTIONS, TARGET_REQUIRED, Decision, Target, bounded_actions


def decode(
    out: dict[str, torch.Tensor],
    values: list[ValueCandidate],
    grid: tuple[int, int],
    viewport: tuple[int, int],
    allowed_actions: tuple[str, ...] | None = None,
    min_confidence: float = 0.5,
    min_target_prob: float = 0.2,
) -> Decision:
    """Decode a single example (batch size 1). Actions whose required argument is
    unavailable are masked, so the decision is always executable."""
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be in [0,1]")
    if not 0.0 <= min_target_prob <= 1.0:
        raise ValueError("min_target_prob must be in [0,1]")
    legal = set(bounded_actions(allowed_actions))
    p_action = torch.softmax(out["action"][0], -1)
    p_value = torch.softmax(out["value"][0, : len(values)], -1)
    p_target = torch.softmax(out["target"][0, : grid[0] * grid[1]], -1)
    kinds = [c.kind for c in values]
    allowed = torch.tensor(
        [action in legal for action in ACTIONS],
        dtype=torch.bool,
        device=p_action.device,
    )
    for i, act in enumerate(ACTIONS):
        kind = VALUE_KIND.get(act)
        if kind and kind not in kinds:
            allowed[i] = False
    if not allowed.any():
        return Decision(action="wait", confidence=0.0)
    p_action = p_action * allowed
    a = int(p_action.argmax())
    action = ACTIONS[a]
    conf = float(p_action[a] / p_action.sum().clamp_min(1e-9))
    kw: dict = {}
    kind = VALUE_KIND.get(action)
    if kind:
        mask = torch.tensor([k == kind for k in kinds])
        pv = p_value * mask
        j = int(pv.argmax())
        conf *= float(pv[j] / pv.sum().clamp_min(1e-9))
        key = {"text": "text", "url": "url", "direction": "direction"}[kind]
        kw[key] = values[j].value
    target = None
    t = int(p_target.argmax())
    t_prob = float(p_target[t])
    if action in TARGET_REQUIRED and t_prob < min_target_prob:
        return Decision(action="wait", confidence=conf * t_prob)
    if action in TARGET_REQUIRED or (action == "recover" and t_prob >= min_target_prob):
        rows, cols = grid
        cx, cy = grid_centers(grid)[t].tolist()
        dx, dy = out["offset"][0, t].tolist()
        x = (cx + dx / cols) * viewport[0]
        y = (cy + dy / rows) * viewport[1]
        target = Target(
            int(min(max(x, 0), viewport[0] - 1)), int(min(max(y, 0), viewport[1] - 1))
        )
        conf *= t_prob
    if action != "wait" and conf < min_confidence:
        return Decision(action="wait", confidence=max(0.0, min(1.0, conf)))
    return Decision(
        action=action, confidence=max(0.0, min(1.0, conf)), target=target, **kw
    )
