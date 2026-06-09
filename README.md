# GNSIS

**A stable, dogfoodable self-evolution runtime for tool-calling LLM agents.**

GNSIS is a clean-room MVP of the ideas in
[Autogenesis](https://github.com/DVampire/Autogenesis) — *"a self-evolution
protocol and runtime for LLM-based agent systems."* Autogenesis is ambitious but
explicitly *"undergoing active refactoring"*, with only the tool-calling agent
considered stable. GNSIS closes that gap: it reimplements the **core** of the
idea as something small enough to actually run, today, end-to-end — so you can
dogfood it.

It keeps the two load-bearing concepts:

- **RSPL — Resource Substrate Protocol Layer.** Prompts (and other artifacts)
  are *versioned, lifecycle-aware resources* with content hashing, explicit
  lineage, and rollback.
- **SEPL — Self-Evolution Protocol Layer.** A loop that **propose → assess →
  commit**s improvements with an auditable trail, realizing the cycle
  **Act → Observe → Optimize → Remember.**

It speaks to models through **OpenRouter** (OpenAI-compatible), and ships a
deterministic **offline mock** so the whole thing — and CI — runs with **zero
API key and zero dependencies**.

---

## Quickstart

```bash
pip install -e .

# 1) See it work, offline, in 5 seconds (no API key needed):
gnsis demo
```

```
iter  ver  score  tool  ok  note
  ------------------------------------------------------------
   0    1   0.00    no   ✓  Answer is wrong because the agent did not use the…
   1    2   1.00   yes   ✓  Correct, and computed with the calculator tool.

[result] 0.00 → 1.00 (↑ improved); best is v2
```

The seed prompt guessed and scored `0.00`. The optimizer proposed stronger
tool-use instructions; the loop committed the first version that actually called
the calculator and reached the correct answer (`1.00`) — as a new, lineaged
prompt version you can inspect and roll back.

### Run against a real model (Claude via OpenRouter)

```bash
cp .env.template .env          # then add your key
export OPENROUTER_API_KEY=sk-or-...

gnsis evolve --provider openrouter --fresh
gnsis run --provider openrouter --task "What is (12 + 30) * 2?"
```

Default model is `anthropic/claude-opus-4.8`. Any OpenRouter slug works via
`--model` or `OPENROUTER_MODEL`. Point `OPENROUTER_BASE_URL` at any
OpenAI-compatible endpoint to use a different gateway.

---

## CLI

| Command | What it does |
| --- | --- |
| `gnsis demo` | Offline, end-to-end self-evolution demo (mock model). |
| `gnsis run --task "..."` | Run the tool-calling agent once; prints the tool trace and answer. |
| `gnsis evolve [--task EXPR] [--fresh]` | Run the self-evolution loop over a versioned prompt. |
| `gnsis history --name <prompt>` | Show a prompt's version lineage (parent links + content hashes). |
| `gnsis rollback --name <prompt> --to N` | Roll a prompt back to an earlier version (append-only). |
| `gnsis version` | Print the version. |

Common flags: `--provider {auto,mock,openrouter}` (auto uses OpenRouter when a
key is present, else the mock), `--model <slug>`, `--workdir <dir>`,
`--config <file.py|.json>`.

Artifacts are written under `--workdir` (default `workdir/`):
`resources/` (versioned prompts), `memory/` (what won, and why), `traces/`
(per-run trajectories).

---

## How the self-evolution works

The bundled task asks the agent for an exact arithmetic answer and grades it:

- correct **and** computed with the calculator tool → `1.0`
- correct but guessed (no tool) → `0.6`
- wrong → `0.0`, with feedback naming the gap

The loop runs the current prompt (**Act**), scores it (**Observe**), asks the
optimizer for candidate prompts and keeps the first one that scores higher
(**Optimize**: propose → assess → commit), and records the winner with its
lineage (**Remember**). Each accepted improvement is a new resource version
whose parent is the prompt it improved on — so the history is auditable and any
step is reversible.

With the **mock** model the loop is fully deterministic (great for CI and for
understanding the mechanics). With a **real** model the same loop applies, and
the optimizer additionally asks the model to rewrite the prompt from the failure
feedback.

---

## Programmatic API

```python
from gnsis import Runtime, Config, SelfEvolutionLoop, PromptOptimizer, calculator_task

runtime = Runtime(Config({"provider": "auto", "workdir": "workdir"}))
loop = SelfEvolutionLoop(runtime, optimizer=PromptOptimizer(model=runtime.model))

report = loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
print(report.start_score, "→", report.best_score)   # 0.0 → 1.0
print(report.best_prompt)

# Inspect and reverse the evolution:
for v in runtime.store.history("prompt", "agent_system_prompt"):
    print(f"v{v.version} (parent {v.parent_version}) {v.short_hash} {v.message}")
runtime.store.rollback("prompt", "agent_system_prompt", to_version=1)
```

Run a single agent turn:

```python
agent = runtime.build_agent(
    "You are precise. Always use the calculator tool to compute exact answers."
)
result = agent.run("What is (7 * 6) + 100?")
print(result.output, result.used_tool)   # "The answer is 142." True
```

---

## Architecture

```
src/gnsis/
  resources/   RSPL — Resource, ResourceVersion, ResourceStore (commit/history/rollback)
  models/      provider-neutral interface; MockModel (offline) + OpenRouterModel (stdlib urllib)
  tools/       Tool protocol + safe calculator; OpenAI-compatible specs
  agent/       ToolCallingAgent — the 'Act' phase
  evolution/   SEPL — Task/evaluation, PromptOptimizer, SelfEvolutionLoop
  memory/      durable cross-run event log ('Remember')
  tracer/      per-run trajectory recording ('Observe')
  runtime.py   the composition root that wires it together
  cli.py       the `gnsis` command
```

This maps onto Autogenesis's manager stack (version, model, prompt, memory,
tool, agent), consolidated into one synchronous, dependency-free runtime for
stability.

---

## Development

```bash
python -m unittest discover -s tests -v   # 41 tests, no dependencies
```

CI runs the suite on Python 3.9–3.12 plus an offline CLI smoke test
(`.github/workflows/ci.yml`). A `SessionStart` hook installs the package for
Claude Code web sessions.

---

## Credits & license

GNSIS is MIT-licensed (see `LICENSE`). It is a clean-room reimplementation of a
small subset of [Autogenesis](https://github.com/DVampire/Autogenesis) (also
MIT) — no upstream source is vendored. See `NOTICE`.
