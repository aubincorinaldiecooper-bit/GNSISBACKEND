
## Video understanding — "Kickflip Roadster" clip (44 s, 1080p)

Same six questions on both sides. OmniJev used its native video path
(16-frame timestamped mosaic → real video tokens); Gemini 2.5 Flash got
the actual mp4. Run on Modal L4, 2026-10-19.

| Question | OmniJev (L4) | Gemini 2.5 Flash (real mp4) |
|---|---|---|
| Free description | noul score 0.94 — 0.48 s warm | Correct, 10.0 s, $0.0047 |
| Brand of vehicles | **Miss**: "Porsche" (conf 0.41) | "Tesla", 7.6 s, $0.0045 |
| Most unusual event | **Flying car** ✓ (0.96), 0.51 s | Same ✓, 8.0 s, $0.0045 |
| Large vehicle on stage | **Semi truck** ✓ (0.99), 0.49 s | Same ✓, 7.6 s, $0.0045 |
| Setting | **Night stage w/ smoke+lights** ✓ (0.98), 0.49 s | Same ✓, 7.1 s, $0.0013 |
| Which event first | **Parked lineup first** ✓ (0.50), 1.28 s | Same ✓, 7.3 s, $0.0045 |
| **Score / cost** | 5/6 · ~$0.0003 total | 6/6 · ~$0.024 total |

Honest read: OmniJev is ~15× faster and ~80× cheaper per question and
nailed everything physical (flying car, semi, setting, ordering) but
missed brand — badges aren't visible in the frames and it hedged at
0.41 confidence instead of abstaining. Gemini read the whole mp4 and
recognized the vehicles outright. Corrective lever: more frames or a
multi-choice abstain threshold, not a bigger model.

### Honesty + "look closer" pass (measured, same clip)

First-pass brand answer landed at 0.41 confidence (below a sane commit
threshold). Re-asking on full-res crops of the same timestamps:

- t=15 crop (Semi + Cybertruck visible): corrected to **"Tesla"**
  (conf 0.34 — right answer, still honestly hedged).
- t=30 crop (gold concept car, no badge): still "Porsche" (conf 0.55) —
  zoom cannot create evidence that isn't in the frame.

Design consequence: gate commits on confidence (abstain beats a false
answer), then run the zoom pass on the abstain path before deferring.
The t=30 miss also shows top-confidence alone isn't enough — gating on
the gap to the runner-up catches confident-but-unsupported picks.
