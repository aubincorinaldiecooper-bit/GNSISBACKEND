"""Move the Thinker checkpoint to the path this branch expects, inside one volume.

The realtime runtime loads two things from the model volume: the MiniCPM-o
directory, whose path is unchanged, and the Thinker checkpoint, which this
branch expects at ``/models/GNSIS/thinker``. On an existing volume the
checkpoint still sits under its previous directory name.

This does not copy the weights. It renames one directory inside the volume,
which is a metadata operation, so it costs seconds rather than the hours a
volume-to-volume copy of the checkpoints would take. The volume name itself
does not have to change: ``GNSIS_MODELS_VOLUME`` points the deploy at whatever
volume already exists.

Run it once, before deploying this branch:

    GNSIS_MODELS_VOLUME=<the existing volume> \\
    GNSIS_THINKER_SOURCE=/models/<previous directory>/thinker \\
    modal run scripts/migrate-thinker-checkpoint.py

It is safe to run twice. It never deletes anything, never writes over an
existing destination, and reports what it found either way.
"""

from __future__ import annotations

import os

import modal

VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME", "").strip()
if not VOLUME_NAME:
    raise RuntimeError(
        "Set GNSIS_MODELS_VOLUME to the existing model volume. There is no "
        "default: guessing a volume name here could touch the wrong one."
    )

# Where the checkpoint is today. No default, so this file carries no assumption
# about the previous naming and cannot move something it was not pointed at.
SOURCE = os.environ.get("GNSIS_THINKER_SOURCE", "").strip()
if not SOURCE:
    raise RuntimeError(
        "Set GNSIS_THINKER_SOURCE to the checkpoint's current absolute path "
        "inside the volume, for example /models/<previous directory>/thinker"
    )

# Where modal/gnsis.py and runtime/configs/gnsis-live.yaml expect it.
DESTINATION = "/models/GNSIS/thinker"

models = modal.Volume.from_name(VOLUME_NAME, create_if_missing=False)

app = modal.App("gnsis-thinker-checkpoint-migration")


@app.function(volumes={"/models": models}, timeout=900)
def migrate() -> dict[str, str]:
    """Rename the checkpoint directory, or report that nothing needs doing."""
    from pathlib import Path

    source = Path(SOURCE)
    destination = Path(DESTINATION)

    if destination.exists() and not source.exists():
        return {
            "status": "already migrated",
            "checkpoint": str(destination),
            "entries": str(len(list(destination.iterdir()))),
        }
    if destination.exists() and source.exists():
        # Both present means someone has already made a destination, and
        # picking one would risk discarding the real checkpoint.
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

    # Read the volume back rather than trusting the rename silently.
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
    result = migrate.remote()
    for key, value in result.items():
        print(f"{key}: {value}")
    if result.get("minicpm_present") == "False":
        print(
            "WARNING: /models/MiniCPM-o-4_5 was not found. The runtime needs it "
            "as well as the checkpoint."
        )
