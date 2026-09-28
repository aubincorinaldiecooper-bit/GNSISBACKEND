"""Pad variable-size feature records into head batches."""

from __future__ import annotations

import torch

from .head import grid_centers


def collate(records: list[dict]) -> dict[str, torch.Tensor]:
    b = len(records)
    n = max(r["visual"].shape[1] for r in records)
    v = max(r["values"].shape[1] for r in records)
    layers, _, d = records[0]["visual"].shape
    out = {
        "queries": torch.stack([r["queries"].float() for r in records]),
        "actions": torch.stack([r["actions"].float() for r in records]),
        "values": torch.zeros(b, layers, v, d),
        "value_mask": torch.zeros(b, v, dtype=torch.bool),
        "visual": torch.zeros(b, layers, n, d),
        "visual_embeds": torch.zeros(b, n, d),
        "visual_mask": torch.zeros(b, n, dtype=torch.bool),
        "centers": torch.zeros(b, n, 2),
        "motion": torch.tensor([float(r["motion"]) for r in records]),
        "action": torch.tensor([int(r.get("action", 0)) for r in records]),
        "value": torch.tensor([int(r.get("value", -1)) for r in records]),
        "target_pos": torch.zeros(b, n, dtype=torch.bool),
        "target_offset": torch.zeros(b, n, 2),
    }
    for i, r in enumerate(records):
        k, m = r["values"].shape[1], r["visual"].shape[1]
        out["values"][i, :, :k] = r["values"].float()
        out["value_mask"][i, :k] = True
        out["visual"][i, :, :m] = r["visual"].float()
        out["visual_embeds"][i, :m] = r["visual_embeds"].float()
        out["visual_mask"][i, :m] = True
        out["centers"][i, :m] = grid_centers(tuple(r["grid"]))
        if "target_pos" in r:
            out["target_pos"][i, :m] = r["target_pos"]
            out["target_offset"][i, :m] = r["target_offset"]
    return out


HEAD_INPUTS = (
    "queries",
    "actions",
    "values",
    "value_mask",
    "visual",
    "visual_embeds",
    "visual_mask",
    "centers",
    "motion",
)
