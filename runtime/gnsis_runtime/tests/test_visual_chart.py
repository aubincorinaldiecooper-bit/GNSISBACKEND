from __future__ import annotations

import io

import pytest

from gnsis_runtime.visual.chart import (
    HORIZON_SECONDS,
    MIN_TRADES,
    Tick,
    build_samples,
    momentum_label,
    render_chart,
)


def _window_ticks(
    start_ms: int = 0,
    *,
    count: int = 31,
    duration_ms: int = 30_000,
    first_price: float = 100.0,
    last_price: float = 100.25,
) -> list[Tick]:
    return [
        Tick(
            start_ms + index * duration_ms // (count - 1),
            first_price + (last_price - first_price) * index / (count - 1),
            1.0,
        )
        for index in range(count)
    ]


def test_momentum_label_uses_exact_thresholds() -> None:
    upward = momentum_label(_window_ticks(), 30_000)
    assert upward is not None
    assert upward[0] == "upward_momentum"
    assert upward[1] == pytest.approx(25.0)
    downward = _window_ticks(first_price=100.0, last_price=99.75)
    result = momentum_label(downward, 30_000)
    assert result is not None
    assert result[0] == "downward_momentum"
    assert result[1] == pytest.approx(-25.0)


def test_momentum_label_abstains_for_insufficient_or_invalid_windows() -> None:
    assert momentum_label(_window_ticks(count=MIN_TRADES - 1), 30_000) is None
    assert momentum_label(_window_ticks(duration_ms=9_000), 30_000) is None
    assert (
        momentum_label(_window_ticks(first_price=0.0, last_price=0.0), 30_000) is None
    )


def test_future_ticks_do_not_change_rendered_chart() -> None:
    observed = _window_ticks(start_ms=1_000_000)
    anchor_ms = observed[-1].ts_ms
    future = observed + [Tick(anchor_ms + 1, 1_000_000.0, 1_000_000.0)]
    original_bytes = io.BytesIO()
    changed_bytes = io.BytesIO()
    render_chart(observed, anchor_ms).save(original_bytes, format="PNG")
    render_chart(future, anchor_ms).save(changed_bytes, format="PNG")
    assert original_bytes.getvalue() == changed_bytes.getvalue()


def test_build_samples_stride_skip_and_deterministic_subsample() -> None:
    ticks = [Tick(ts_ms, 100.0, 1.0) for ts_ms in range(0, 700_000, 1000)]
    samples = build_samples(
        ticks, "BTC-USDT", "train", stride_seconds=60, max_samples=3
    )
    assert [sample.anchor_ms for sample in samples] == [300_000, 480_000, 660_000]
    assert samples == build_samples(
        ticks, "BTC-USDT", "train", stride_seconds=60, max_samples=3
    )
    sparse = [tick for tick in ticks if not 360_000 <= tick.ts_ms < 400_000]
    skipped = build_samples(
        sparse, "BTC-USDT", "train", stride_seconds=60, max_samples=100
    )
    assert 360_000 not in {sample.anchor_ms for sample in skipped}
    assert all(sample.label == "range_bound" for sample in samples)


def test_render_chart_is_deterministic_448_rgb_and_clips_price_bps() -> None:
    ticks = [
        Tick(1_000_000, 200.0, 1.0),
        Tick(1_299_000, 100.0, 1.0),
    ]
    image = render_chart(ticks, 1_300_000)
    first = io.BytesIO()
    second = io.BytesIO()
    image.save(first, format="PNG")
    render_chart(ticks, 1_300_000).save(second, format="PNG")
    assert image.size == (448, 448)
    assert image.mode == "RGB"
    assert first.getvalue() == second.getvalue()
    assert any(
        image.getpixel((x, y)) == (0, 0, 0) for x in range(448) for y in range(3)
    )


def test_outcome_and_persistence_use_distinct_30_second_windows() -> None:
    anchor_ms = 300_000
    ticks = [
        Tick(
            ts_ms,
            100 + (ts_ms / 1_000_000)
            if ts_ms <= anchor_ms
            else 100.3 + (ts_ms - anchor_ms) / 100_000,
            1.0,
        )
        for ts_ms in range(anchor_ms - 30_000, anchor_ms + 30_001, 1000)
    ]
    persistence = momentum_label(ticks, anchor_ms)
    outcome = momentum_label(ticks, anchor_ms + HORIZON_SECONDS * 1000)
    assert persistence is not None
    assert outcome is not None
    assert persistence[1] < outcome[1]


def test_chart_head_forward_mask_temperature_and_abstention() -> None:
    torch = pytest.importorskip("torch")
    from torch.nn import functional as F

    from gnsis_runtime.visual.chart_head import ChartStateHead

    torch.manual_seed(7)
    head = ChartStateHead(hidden_size=16, proj=8)
    embeds = torch.randn(2, 5, 16)
    mask = torch.tensor([[True, True, False, False, False], [True] * 5])
    output = head(embeds, mask)
    assert output.shape == (2, 3)
    changed = embeds.clone()
    changed[0, 2:] = 1e5
    assert torch.allclose(output[0], head(changed, mask)[0])

    logits = torch.tensor(
        [[10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0], [10.0, 0.0, 0.0]]
    )
    targets = torch.tensor([0, 1, 2, 1])
    before = F.cross_entropy(logits, targets).item()
    head.fit_temperature(logits, targets)
    after = F.cross_entropy(logits / head.temperature, targets).item()
    assert after < before
    assert head.decide(torch.tensor([0.49, 0.31, 0.20])) == (
        "upward_momentum",
        pytest.approx(0.49),
        True,
    )
