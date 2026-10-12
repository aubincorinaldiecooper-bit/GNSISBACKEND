"""Third-party model adapter for GNSIS 0.5.

The upstream source remains external and pinned in runtime/PROVENANCE.md. This
adapter loads it from an explicit source directory so GNSIS does not maintain a
forked copy of the model implementation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import torch
from PIL import Image

from .local_controller import Candidate, Selection

_JSON_BLOCK = re.compile(r"\[[\s\S]*\]")


class GnsisLocalBackend:
    def __init__(
        self,
        *,
        source_dir: str,
        checkpoint_dir: str,
        base_model_dir: str,
        state_image: str | None = None,
    ):
        source = Path(source_dir).resolve()
        if not (source / "mso" / "infer.py").is_file():
            raise ValueError("OmniJev source_dir does not contain mso/infer.py")
        sys.path.insert(0, str(source))
        from mso.infer import MSO1

        self.model = MSO1(checkpoint_dir, base_model_dir)
        self.state_image = state_image or self._neutral_state(checkpoint_dir)

    @staticmethod
    def _neutral_state(checkpoint_dir: str) -> str:
        del checkpoint_dir
        path = Path(tempfile.gettempdir()) / "gnsis-neutral-local-state.png"
        if not path.exists():
            Image.new("RGB", (64, 64), "white").save(path)
        return str(path)

    def candidates(
        self,
        *,
        text: str,
        history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: list[dict[str, Any]],
    ) -> list[Candidate]:
        prompt = (
            "You are the candidate builder for a local desktop assistant. "
            "Return only a JSON array with at most eight safe next-step candidates. "
            "Each candidate is either "
            '{"kind":"tool","tool":"offered name","args":{},"description":"plain summary"}, '
            '{"kind":"done","message":"brief final answer","description":"task is complete"}, '
            'or {"kind":"fallback","reason":"why local handling is unsafe","description":"use cloud fallback"}. '
            "Use only the supplied tool schemas. Copy names, paths, URLs, and dictated wording "
            "from the trusted request exactly. Do not invent targets or credentials.\n"
            f"REQUEST:\n{text}\n"
            f"HISTORY:\n{json.dumps(history, ensure_ascii=False)}\n"
            f"OFFERED TOOLS:\n{json.dumps(tool_schemas, ensure_ascii=False)}"
        )
        raw = self._generate(prompt, max_new_tokens=768)
        match = _JSON_BLOCK.search(raw)
        if match is None:
            return []
        decoded = json.loads(match.group(0))
        if not isinstance(decoded, list):
            return []
        out: list[Candidate] = []
        for index, item in enumerate(decoded[:8]):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            description = item.get("description")
            if not isinstance(kind, str) or not isinstance(description, str):
                continue
            out.append(
                Candidate(
                    key=f"candidate-{index + 1}",
                    kind=kind,
                    description=description,
                    tool=item.get("tool") if isinstance(item.get("tool"), str) else None,
                    args=item.get("args") if isinstance(item.get("args"), dict) else None,
                    message=item.get("message")
                    if isinstance(item.get("message"), str)
                    else None,
                    reason=item.get("reason")
                    if isinstance(item.get("reason"), str)
                    else None,
                )
            )
        return out

    def select(
        self,
        *,
        text: str,
        history: list[dict[str, Any]],
        candidates: list[Candidate],
    ) -> Selection:
        criteria = {candidate.key: candidate.description for candidate in candidates}
        answer = self.model.system_one(
            {"images": [self.state_image]},
            {
                "next": {
                    "type": "choice",
                    "instructions": (
                        f"Trusted request: {text}\n"
                        f"Completed steps: {json.dumps(history, ensure_ascii=False)}\n"
                        "Which exact offered candidate is the safest correct next step?"
                    ),
                    "criteria": criteria,
                }
            },
        )["next"]
        return Selection(
            key=answer.get("choice"),
            confidence=float(answer.get("confidence", 0.0)),
            abstain=float(answer.get("abstain", 0.0)),
            valid=bool(answer.get("valid", False)),
        )

    def _generate(self, prompt: str, *, max_new_tokens: int) -> str:
        processor = self.model.proc
        backbone = self.model.model.backbone
        text = processor.apply_chat_template(
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            tokenize=False,
            add_generation_prompt=True,
        )
        encoded = processor(text=[text], return_tensors="pt")
        encoded = {
            key: value.to(self.model.dev) if torch.is_tensor(value) else value
            for key, value in encoded.items()
        }
        adapter_off = getattr(backbone, "disable_adapter", None)
        context = adapter_off() if callable(adapter_off) else nullcontext()
        with context, torch.inference_mode():
            output = backbone.generate(
                **encoded,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )
        input_length = int(encoded["input_ids"].shape[-1])
        return processor.batch_decode(
            output[:, input_length:],
            skip_special_tokens=True,
        )[0]


def backend_from_env() -> GnsisLocalBackend:
    required = {
        "source_dir": os.environ.get("GNSIS_OMNIJEV_SOURCE"),
        "checkpoint_dir": os.environ.get("GNSIS_OMNIJEV_CHECKPOINT"),
        "base_model_dir": os.environ.get("GNSIS_QWEN_BASE_MODEL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing local model settings: {', '.join(missing)}")
    return GnsisLocalBackend(
        source_dir=required["source_dir"] or "",
        checkpoint_dir=required["checkpoint_dir"] or "",
        base_model_dir=required["base_model_dir"] or "",
        state_image=os.environ.get("GNSIS_LOCAL_STATE_IMAGE"),
    )
