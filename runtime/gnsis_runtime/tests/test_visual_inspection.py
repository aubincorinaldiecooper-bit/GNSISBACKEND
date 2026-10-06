from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from gnsis_runtime.visual.inspection import (
    MAX_PIXEL_SAMPLES,
    MAX_ZOOM,
    InspectionError,
    Region,
    inspect_region,
    read_pixels,
)


def _decoded(view) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(view["image"]["data"])))


def test_inspect_enlarges_a_region_with_exact_nearest_neighbour_pixels() -> None:
    image = Image.new("RGB", (1280, 942), "white")
    image.putpixel((100, 50), (12, 34, 56))

    view = inspect_region(image, Region(96, 48, 8, 4), display_size=256)
    zoomed = _decoded(view)

    assert view["scale"] == MAX_ZOOM
    assert zoomed.size == (64, 32)
    assert zoomed.getpixel((4 * MAX_ZOOM, 2 * MAX_ZOOM)) == (12, 34, 56)
    assert zoomed.getpixel((0, 0)) == (255, 255, 255)


def test_inspect_without_a_region_fits_the_whole_frame() -> None:
    view = inspect_region(Image.new("RGB", (1280, 942)), display_size=640)

    assert view["region"] == [0, 0, 1280, 942]
    assert (view["image"]["width"], view["image"]["height"]) == (640, 471)


@pytest.mark.parametrize(
    "region",
    [Region(-1, 0, 10, 10), Region(0, 0, 0, 10), Region(1270, 0, 20, 10)],
)
def test_regions_must_lie_inside_the_frame(region) -> None:
    with pytest.raises(InspectionError):
        inspect_region(Image.new("RGB", (1280, 942)), region)


def test_read_pixels_samples_exact_colors_and_bounds_sample_count() -> None:
    image = Image.new("RGB", (100, 100), (1, 2, 3))
    image.putpixel((10, 20), (255, 0, 16))

    pixels = read_pixels(image, Region(10, 20, 3, 2), step=2)

    assert pixels["columns"] == [10, 12]
    assert pixels["rows"] == [{"y": 20, "colors": ["#ff0010", "#010203"]}]
    with pytest.raises(InspectionError, match=str(MAX_PIXEL_SAMPLES)):
        read_pixels(image, Region(0, 0, 100, 100))
    assert len(read_pixels(image, Region(0, 0, 100, 100), step=2)["rows"]) == 50
