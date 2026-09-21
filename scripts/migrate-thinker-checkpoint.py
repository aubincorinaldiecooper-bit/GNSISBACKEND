"""Move the Thinker checkpoint to the GNSIS path inside the model volume.

The realtime runtime loads MiniCPM-o plus the Thinker checkpoint from one Modal
Volume. The runtime expects the Thinker checkpoint at
``/models/GNSIS/thinker``.

Run this once before deploying the live runtime:

    GNSIS_MODELS_VOLUME=<existing volume> \
    GNSIS_THINKER_SOURCE=/models/<previous directory>/thinker \
    modal run scripts/migrate-thinker-checkpoint.py

The source path is passed as a function argument rather than read by the remote
container at import time. That matters because environment variables provided
to the local Modal CLI are not automatically present while Modal imports this
module remotely.
"""

from __future__ import annotations

import os

import modal

VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME", "gnsis-model-weights").strip()
SOURCE = os.environ.get("GNSIS_THINKER_SOURCE", "").strip()
if not SOURCE:
    raise RuntimeError(
        "Set GNSIS_THINKER_SOURCE to the checkpoint's current absolute path "
        "inside the volume, for example /models/<previous directory>/thinker"
    )

DESTINATION = "/models/GNSIS/thinker"

models = modal.Volume.from_name(VOLUME_NAME, create_if_missing=False)
app = modal.App("gnsis-thinker-checkpoint-migration")


@app.function(volumes={"/models": models}, timeout=900)
def migrate(source_path: str) -> dict[str, str]:
    """Rename the checkpoint directory, or report that nothing needs doing."""
    from pathlib import Path

    source = Path(source_path)
    destination = Path(DESTINATION)

    if destination.exists() and not source.exists():
        return {
            "status": "already migrated",
            "checkpoint": str(destination),
            "entries": str(len(list(destination.iterdir()))),
            "minicpm_present": str(Path("/models/MiniCPM-o-4_5").is_dir()),
        }
    if destination.exists() and source.exists():
        raise RuntimeError(
            f"both {source} and {destination} exist; refusing to choose between "
            "them. Inspect the volume and remove whichever is not the checkpoint."
        )
    if not source.exists():
        raise FileNotFoundError(
            f"no checkpoint at {source}. Check GNSIS_THINKER_SOURCE against the "
            "volume's actual layout."
        )

    entries = len(list(source.iterdir()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    models.commit()

    if not destination.is_dir():
        raise RuntimeError(f"{destination} is missing after the rename")
    moved = len(list(destination.iterdir()))
    if moved != entries:
        raise RuntimeError(
            f"{destination} has {moved} entries, expected {entries} from {source}"
        )

    model_dir = Path("/models/MiniCPM-o-4_5")
    return {
        "status": "migrated",
        "from": str(source),
        "checkpoint": str(destination),
        "entries": str(moved),
        "minicpm_present": str(model_dir.is_dir()),
    }


@app.local_entrypoint()
def main() -> None:
    result = migrate.remote(SOURCE)
    for key, value in result.items():
        print(f"{key}: {value}")
    if result.get("minicpm_present") == "False":
        print(
            "WARNING: /models/MiniCPM-o-4_5 was not found. The runtime needs it "
            "as well as the checkpoint."
        )
