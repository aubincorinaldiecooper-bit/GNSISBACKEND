"""Keep product-specific market logic out of GNSIS/Panoptic source."""

from __future__ import annotations

from pathlib import Path


SOURCE_ROOTS = ("runtime", "modal", "src")
FORBIDDEN_MARKET_SEMANTICS = (
    "Baystfirm",
    "short_horizon_momentum",
    "gnsis_chart_momentum",
    "baystfirm_chart_momentum",
    "ChartStateHead",
)


def test_baystfirm_market_semantics_do_not_live_in_gnsis() -> None:
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for source_root in SOURCE_ROOTS:
        base = root / source_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".json",
                ".yaml",
                ".yml",
                ".toml",
            }:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            matched = [term for term in FORBIDDEN_MARKET_SEMANTICS if term in text]
            if matched:
                violations.append(f"{path.relative_to(root)}: {', '.join(matched)}")
    assert not violations, (
        "Baystfirm market semantics belong in the Baystfirm repository. "
        "Panoptic may expose generic visual/browser capability only.\n"
        + "\n".join(violations)
    )
