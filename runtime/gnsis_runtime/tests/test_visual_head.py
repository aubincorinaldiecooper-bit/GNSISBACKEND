from __future__ import annotations

import torch

from gnsis_runtime.visual.batching import HEAD_INPUTS, collate
from gnsis_runtime.visual.decode import decode
from gnsis_runtime.visual.head import HeadConfig, JEVDecisionHead, decision_loss
from gnsis_runtime.visual.labels import action_label, target_labels
from gnsis_runtime.visual.prompt import ValueCandidate
from gnsis_runtime.visual.schema import ACTIONS

VIEWPORT = (1280, 800)
GRID = (13, 20)


def _hit(point, box):
    x, y = point
    bx, by, bw, bh = box
    return bx <= x <= bx + bw and by <= y <= by + bh


def _logits_for(action: str, cell: int, offset, n_values: int):
    n = GRID[0] * GRID[1]
    out = {
        "action": torch.full((1, len(ACTIONS)), -10.0),
        "value": torch.zeros(1, n_values),
        "target": torch.full((1, n), -10.0),
        "offset": torch.zeros(1, n, 2),
    }
    out["action"][0, action_label(action)] = 10.0
    out["target"][0, cell] = 10.0
    out["offset"][0] = offset
    return out


@pytest.mark.parametrize(
    "box",
    [
        (700, 290, 90, 40),
        (5, 5, 40, 22),
        (1180, 760, 90, 36),
        (400, 380, 16, 16),
    ],
)
def test_decoded_point_lands_in_box_given_oracle_labels(box):
    pos, offset = target_labels(box, GRID, VIEWPORT)
    cell = int(pos.nonzero()[0])
    decision = decode(
        _logits_for("click", cell, offset, 1),
        [ValueCandidate("none", "")],
        GRID,
        VIEWPORT,
    )
    assert decision.target is not None
    assert _hit((decision.target.x, decision.target.y), box)


def test_decoder_masks_actions_without_required_argument():
    out = _logits_for("type", 0, torch.zeros(GRID[0] * GRID[1], 2), 1)
    out["action"][0, action_label("wait")] = 5.0
    decision = decode(out, [ValueCandidate("none", "")], GRID, VIEWPORT)
    assert decision.action == "wait"


def test_decoder_keeps_wait_available_under_restricted_actions():
    out = _logits_for("click", 0, torch.zeros(GRID[0] * GRID[1], 2), 1)
    out["action"][0, action_label("wait")] = 10.5
    decision = decode(
        out,
        [ValueCandidate("none", "")],
        GRID,
        VIEWPORT,
        allowed_actions=("click",),
    )
    assert decision.action == "wait"


def _record(action: str, box, n_values: int = 3):
    layers, hidden, n = 2, 64, GRID[0] * GRID[1]
    pos, off = target_labels(box, GRID, VIEWPORT)
    return {
        "queries": torch.randn(layers, 3, hidden),
        "actions": torch.randn(layers, len(ACTIONS), hidden),
        "values": torch.randn(layers, n_values, hidden),
        "visual": torch.randn(layers, n, hidden),
        "visual_embeds": torch.randn(n, hidden),
        "grid": GRID,
        "motion": 0.0,
        "action": action_label(action),
        "value": 0,
        "target_pos": pos,
        "target_offset": off,
    }


def test_head_learns_to_point_at_target_cell():
    torch.manual_seed(0)
    records = [
        _record("click", (700, 290, 90, 40)),
        _record("done", None, n_values=2),
    ]
    head = JEVDecisionHead(HeadConfig(hidden_size=64, n_layers=2, proj=32))
    opt = torch.optim.Adam(head.parameters(), lr=3e-3)
    batch = collate(records)
    for _ in range(150):
        losses = decision_loss(
            head(**{key: batch[key] for key in HEAD_INPUTS}),
            batch,
        )
        opt.zero_grad()
        losses["total"].backward()
        opt.step()
    assert torch.isfinite(losses["total"])
    head.eval()
    with torch.no_grad():
        one = collate([records[0]])
        out = head(**{key: one[key] for key in HEAD_INPUTS})
    decision = decode(
        out,
        [ValueCandidate("none", "")] * 3,
        GRID,
        VIEWPORT,
    )
    assert decision.action == "click"
    assert decision.target is not None
    assert _hit((decision.target.x, decision.target.y), (700, 290, 90, 40))
