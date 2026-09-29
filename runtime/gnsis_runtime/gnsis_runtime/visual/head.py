"""JEV-style non-autoregressive decision head.

Three pointer heads score candidates directly from backbone hidden states
(Vision-JEV: score(c) = (Wq h_q + bq)^T (Wk h_c + bk) / sqrt(d)):

- action pointer: action query vs the 8 action-marker states;
- value pointer: value query vs goal-derived argument markers;
- target pointer: target query vs every visual token of the frame, plus a
  bounded in-cell offset regression for sub-token localisation.

The stream's motion signal conditions the action query.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import torch
from torch import nn

from .prompt import QUERIES
from .schema import ACTIONS

Q_ACTION, Q_TARGET, Q_VALUE = (QUERIES.index(q) for q in ("action", "target", "value"))
MAX_OFFSET_CELLS = 1.5


@dataclass(frozen=True)
class HeadConfig:
    hidden_size: int = 1024
    n_layers: int = 2
    proj: int = 256
    pos_freqs: int = 8


def grid_centers(grid: tuple[int, int]) -> torch.Tensor:
    """Normalised (x, y) token-cell centres in row-major order, shape [N, 2]."""
    rows, cols = grid
    ys = (torch.arange(rows, dtype=torch.float32) + 0.5) / rows
    xs = (torch.arange(cols, dtype=torch.float32) + 0.5) / cols
    yy, xx = torch.meshgrid(ys, xs, indexing="ij")
    return torch.stack([xx.flatten(), yy.flatten()], dim=-1)


class LayerMix(nn.Module):
    def __init__(self, n_layers: int, hidden: int):
        super().__init__()
        self.weights = nn.Parameter(torch.zeros(n_layers))
        self.norm = nn.LayerNorm(hidden)

    def forward(self, h: torch.Tensor) -> torch.Tensor:  # [..., L, *, D] with L at dim -3
        w = torch.softmax(self.weights, 0)
        return self.norm(torch.einsum("l,...lnd->...nd", w, h))


class Pointer(nn.Module):
    def __init__(self, hidden: int, proj: int, extra_q: int = 0, extra_k: int = 0):
        super().__init__()
        self.q = nn.Linear(hidden + extra_q, proj)
        self.k = nn.Linear(hidden + extra_k, proj)
        self.scale = proj**-0.5

    def forward(self, q: torch.Tensor, k: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        scores = torch.einsum("bd,bnd->bn", self.q(q), self.k(k)) * self.scale
        if mask is not None:
            scores = scores.masked_fill(~mask, float("-inf"))
        return scores


class JEVDecisionHead(nn.Module):
    def __init__(self, config: HeadConfig):
        super().__init__()
        self.config = config
        d, p = config.hidden_size, config.proj
        self.pos_dim = 4 * config.pos_freqs
        self.mix_q = LayerMix(config.n_layers, d)
        self.mix_c = LayerMix(config.n_layers, d)
        self.mix_v = LayerMix(config.n_layers, d)
        self.motion = nn.Sequential(nn.Linear(1, 32), nn.GELU(), nn.Linear(32, 32))
        self.action = Pointer(d, p, extra_q=32)
        self.value = Pointer(d, p)
        self.target = Pointer(d, p, extra_k=self.pos_dim + d)
        self.offset = nn.Sequential(nn.Linear(2 * d + self.pos_dim, p), nn.GELU(), nn.Linear(p, 2))

    def pos_features(self, centers: torch.Tensor) -> torch.Tensor:
        freqs = 2.0 ** torch.arange(self.config.pos_freqs, dtype=torch.float32, device=centers.device) * math.pi
        ang = centers[..., None] * freqs  # [..., N, 2, F]
        return torch.cat([ang.sin(), ang.cos()], dim=-1).flatten(-2)

    def forward(
        self,
        queries: torch.Tensor,  # [B, L, Q, D]
        actions: torch.Tensor,  # [B, L, A, D]
        values: torch.Tensor,  # [B, L, V, D]
        value_mask: torch.Tensor,  # [B, V]
        visual: torch.Tensor,  # [B, L, N, D]
        visual_embeds: torch.Tensor,  # [B, N, D]
        visual_mask: torch.Tensor,  # [B, N]
        centers: torch.Tensor,  # [B, N, 2]
        motion: torch.Tensor,  # [B]
    ) -> dict[str, torch.Tensor]:
        q = self.mix_q(queries)
        a = self.mix_c(actions)
        v = self.mix_c(values)
        vis = self.mix_v(visual)
        pos = self.pos_features(centers)
        m = self.motion(motion[:, None])
        action_logits = self.action(torch.cat([q[:, Q_ACTION], m], -1), a)
        value_logits = self.value(q[:, Q_VALUE], v, value_mask)
        keys = torch.cat([vis, pos, visual_embeds], -1)
        target_logits = self.target(q[:, Q_TARGET], keys, visual_mask)
        qt = q[:, Q_TARGET, None].expand(-1, vis.shape[1], -1)
        offsets = torch.tanh(self.offset(torch.cat([vis, qt, pos], -1))) * MAX_OFFSET_CELLS
        return {"action": action_logits, "value": value_logits, "target": target_logits, "offset": offsets}


def decision_loss(out: dict[str, torch.Tensor], batch: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    ce = nn.functional.cross_entropy
    action = ce(out["action"], batch["action"])
    has_value = batch["value"] >= 0
    value = ce(out["value"][has_value], batch["value"][has_value]) if has_value.any() else out["value"].sum() * 0
    has_target = batch["target_pos"].any(-1)
    if has_target.any():
        logp = torch.log_softmax(out["target"][has_target], -1)
        pos = batch["target_pos"][has_target]
        target = -(torch.logsumexp(logp.masked_fill(~pos, float("-inf")), -1)).mean()
        off = out["offset"][has_target]
        err = (off - batch["target_offset"][has_target]).abs().sum(-1)
        offset = (err * pos).sum() / pos.sum()
    else:
        target = offset = out["target"].sum() * 0
    total = action + value + target + offset
    return {"total": total, "action": action, "value": value, "target": target, "offset": offset}


def action_index(name: str) -> int:
    return ACTIONS.index(name)
