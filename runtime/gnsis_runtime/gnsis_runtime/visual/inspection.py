"""Read-only inspection of the bounded recent frame window, VISTA-style.

Agents look back at a retained frame, zoom into a region at full resolution
(``inspect``), or read exact pixel colours (``read_pixels``). Only frames still
inside the session's bounded, timestamp-indexed history can be addressed;
nothing here stores frames or runs a model.

Adapted from the inspect / read_pixels tool design of VISTA
(https://github.com/joshhhhhan/VISTA, MIT, commit c97c354).
"""

from __future__ import annotations

import base64
import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image

DEFAULT_DISPLAY_SIZE = 1024
MAX_DISPLAY_SIZE = 1536
MAX_ZOOM = 8
MAX_PIXEL_SAMPLES = 4096


class InspectionError(ValueError):
    """An inspection request that does not fit the addressed frame."""


@dataclass(frozen=True, slots=True)
class Region:
    """``x, y, width, height`` in the addressed frame's viewport pixels."""

    x: int
    y: int
    width: int
    height: int

    @classmethod
    def full(cls, size: tuple[int, int]) -> Region:
        return cls(0, 0, size[0], size[1])

    def validate(self, size: tuple[int, int]) -> Region:
        width, height = size
        if self.width <= 0 or self.height <= 0:
            raise InspectionError("region width and height must be positive")
        if (
            self.x < 0
            or self.y < 0
            or self.x + self.width > width
            or self.y + self.height > height
        ):
            raise InspectionError(f"region must lie inside the {width}x{height} frame")
        return self

    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def to_json(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]


def inspect_region(
    image: Image.Image,
    region: Region | None = None,
    display_size: int = DEFAULT_DISPLAY_SIZE,
) -> dict[str, Any]:
    """Crop ``region`` and scale its longer side toward ``display_size``.

    Enlargement uses nearest-neighbour so zoomed pixels stay exact; it is
    capped at ``MAX_ZOOM``. The PNG is returned base64-encoded.
    """

    if not 64 <= display_size <= MAX_DISPLAY_SIZE:
        raise InspectionError(f"display_size must be between 64 and {MAX_DISPLAY_SIZE}")
    image = image.convert("RGB")
    area = (region or Region.full(image.size)).validate(image.size)
    crop = image.crop(area.box())
    scale = min(display_size / max(area.width, area.height), MAX_ZOOM)
    size = (max(1, round(area.width * scale)), max(1, round(area.height * scale)))
    if size != crop.size:
        resample = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.LANCZOS
        crop = crop.resize(size, resample)
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    return {
        "region": area.to_json(),
        "scale": round(scale, 4),
        "image": {
            "mime_type": "image/png",
            "width": size[0],
            "height": size[1],
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


def read_pixels(
    image: Image.Image,
    region: Region,
    step: int = 1,
) -> dict[str, Any]:
    """Exact ``#rrggbb`` samples every ``step`` pixels across ``region``, row-major."""

    if step < 1:
        raise InspectionError("step must be at least 1")
    image = image.convert("RGB")
    area = region.validate(image.size)
    xs = range(area.x, area.x + area.width, step)
    ys = range(area.y, area.y + area.height, step)
    if len(xs) * len(ys) > MAX_PIXEL_SAMPLES:
        raise InspectionError(
            f"region yields {len(xs) * len(ys)} samples; at most {MAX_PIXEL_SAMPLES} "
            "are allowed, so shrink the region or raise step"
        )
    pixels = image.load()
    return {
        "region": area.to_json(),
        "step": step,
        "columns": list(xs),
        "rows": [
            {
                "y": y,
                "colors": ["#{:02x}{:02x}{:02x}".format(*pixels[x, y]) for x in xs],
            }
            for y in ys
        ],
    }


def frame_entry(
    frame_id: str, captured_at_ms: int | None, size: Sequence[int]
) -> dict[str, Any]:
    return {
        "frame_id": frame_id,
        "captured_at_ms": captured_at_ms,
        "width": int(size[0]),
        "height": int(size[1]),
    }
