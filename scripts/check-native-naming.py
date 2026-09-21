"""Fail when retired project names leak into the current repository tree."""

from __future__ import annotations

import subprocess
from pathlib import Path


# Current-tree guard: both tracked paths and file contents are checked.
FORBIDDEN = (
    "".join(chr(n) for n in (103, 97, 110, 100, 101, 114)),
    "".join(chr(n) for n in (99, 108, 105, 112, 105, 116)),
)


def tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(p.decode("utf-8")) for p in result.stdout.split(b"\0") if p]


def main() -> int:
    failures: list[str] = []
    for path in tracked_paths():
        folded_path = str(path).casefold()
        for token in FORBIDDEN:
            if token in folded_path:
                failures.append(f"path: {path}")
                break

        try:
            data = path.read_bytes()
        except OSError:
            continue

        folded = data.lower()
        for token in FORBIDDEN:
            if token.encode("ascii") in folded:
                failures.append(f"content: {path}")
                break

    if failures:
        print("Retired project naming found in the current tree:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Native naming check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
