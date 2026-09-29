"""Native MiniCPM-V 4.6 backbone used as a frozen feature extractor.

Only forward passes are used: the vision tower encodes a rendered frame into
visual tokens, the language model runs a single non-autoregressive pass over the
decision sequence, and hidden states at visual/marker/query positions are
returned for the JEV head. `generate` is never called on the decision path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

from .prompt import CAND_MARK, QUERIES, QUERY_MARK, DecisionLayout
from .schema import ACTIONS


@dataclass(frozen=True)
class BackboneConfig:
    model_dir: str
    dtype: str = "float32"
    device: str = "cpu"
    downsample_mode: str = "16x"
    scale_resolution: int = 896
    layers: tuple[int, ...] = (12, 24)
    threads: int | None = None


@dataclass
class VisualTokens:
    embeds: torch.Tensor  # [N, D] projected into LM space
    grid: tuple[int, int]  # token rows, cols; row-major order
    timing_ms: dict[str, float]


@dataclass
class DecisionFeatures:
    visual: torch.Tensor  # [L, N, D]
    actions: torch.Tensor  # [L, A, D]
    values: torch.Tensor  # [L, V, D]
    queries: torch.Tensor  # [L, Q, D]
    visual_embeds: torch.Tensor  # [N, D] pre-LM visual tokens (task-independent)
    grid: tuple[int, int]
    timing_ms: dict[str, float]


class MiniCPMVBackbone:
    def __init__(self, config: BackboneConfig):
        if config.threads:
            torch.set_num_threads(config.threads)
        self.config = config
        self.processor = AutoProcessor.from_pretrained(config.model_dir)
        self.model = (
            AutoModelForImageTextToText.from_pretrained(config.model_dir, dtype=getattr(torch, config.dtype))
            .to(config.device)
            .eval()
        )
        self.device = torch.device(config.device)
        self.tokenizer = self.processor.tokenizer
        self.core = self.model.model
        self.hidden_size = self.core.language_model.config.hidden_size
        ids = self.tokenizer.convert_tokens_to_ids
        self.image_start_id = ids(self.tokenizer.image_start_token)
        self.image_end_id = ids(self.tokenizer.image_end_token)
        self.image_pad_id = ids(self.tokenizer.image_token)
        self.cand_id = ids(CAND_MARK)
        self.query_id = ids(QUERY_MARK)
        self.divisor = 4 if config.downsample_mode == "4x" else 16

    @torch.inference_mode()
    def encode_visual(self, image: Image.Image) -> VisualTokens:
        t0 = time.perf_counter()
        inputs = self.processor.image_processor(
            images=[image.convert("RGB")],
            slice_mode=False,
            scale_resolution=self.config.scale_resolution,
            downsample_mode=self.config.downsample_mode,
            return_tensors="pt",
        )
        t1 = time.perf_counter()
        sizes = inputs["target_sizes"]
        out = self.core.get_image_features(
            inputs["pixel_values"].to(self.device), sizes.to(self.device), downsample_mode=self.config.downsample_mode
        )
        embeds = out.pooler_output[0]
        self._sync()
        t2 = time.perf_counter()
        side = 4 if self.divisor == 16 else 2
        grid = (int(sizes[0, 0]) // side, int(sizes[0, 1]) // side)
        if grid[0] * grid[1] != embeds.shape[0]:
            raise RuntimeError(f"visual token grid {grid} does not match {embeds.shape[0]} tokens")
        return VisualTokens(embeds, grid, {"preprocess": (t1 - t0) * 1e3, "vision": (t2 - t1) * 1e3})

    def _sync(self) -> None:
        if self.device.type == "cuda":
            torch.cuda.synchronize()

    def _ids(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False)

    @torch.inference_mode()
    def decision_features(self, layout: DecisionLayout, visual: VisualTokens) -> DecisionFeatures:
        t0 = time.perf_counter()
        prefix = self._ids(layout.prefix)
        suffix = self._ids(layout.suffix)
        n = visual.embeds.shape[0]
        ids = prefix + [self.image_start_id] + [self.image_pad_id] * n + [self.image_end_id] + suffix
        input_ids = torch.tensor([ids], device=self.device)
        embeds = self.core.get_input_embeddings()(input_ids)
        vis_start = len(prefix) + 1
        embeds[0, vis_start : vis_start + n] = visual.embeds.to(embeds.dtype)
        out = self.core.language_model(inputs_embeds=embeds, output_hidden_states=True, use_cache=False)
        hidden = torch.stack([out.hidden_states[i][0] for i in self.config.layers]).float().cpu()  # [L, T, D]
        input_ids = input_ids.cpu()
        cand = (input_ids[0] == self.cand_id).nonzero().flatten()
        query = (input_ids[0] == self.query_id).nonzero().flatten()
        if len(query) != len(QUERIES) or len(cand) != len(ACTIONS) + len(layout.values):
            raise RuntimeError("marker count mismatch in decision sequence")
        t1 = time.perf_counter()
        return DecisionFeatures(
            visual=hidden[:, vis_start : vis_start + n].float(),
            actions=hidden[:, cand[: len(ACTIONS)]].float(),
            values=hidden[:, cand[len(ACTIONS) :]].float(),
            queries=hidden[:, query].float(),
            visual_embeds=visual.embeds.float().cpu(),
            grid=visual.grid,
            timing_ms={**visual.timing_ms, "language": (t1 - t0) * 1e3, "tokens": float(len(ids))},
        )
