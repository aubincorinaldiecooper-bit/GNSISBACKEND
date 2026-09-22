# Recent visual context evaluations

Scenario files for `scripts/eval_recent_visual.py`. Drop JPEG frames into
`frames/` and 16 kHz mono s16le WAV questions into `questions/` (record or
synthesize them; `ffmpeg -i q.mp3 -ar 16000 -ac 1 -f s16le q.wav`).

Run against a deployed runtime:

    python scripts/eval_recent_visual.py \
        --url wss://<runtime-host> --edge-secret "$GNSIS_EDGE_SECRET" \
        --scenario eval/recent_visual/object_reference.json

Audio is paced at real time, so a scenario takes roughly as long as it would
on the phone. `context_conflict` additionally asks about a laptop first so
older conversation context exists before the camera sequence.
