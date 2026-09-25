---
name: testing-simplemem-sidecar
description: Boot and exercise the Omni-SimpleMem sidecar live against SimpleMemProvider/SessionMemoryRecall/CodeMemory for verification runs.
---

# Testing the Omni-SimpleMem sidecar live

## Boot

```bash
python3 -m venv /tmp/smvenv
/tmp/smvenv/bin/pip install -r tools/simplemem/requirements.txt

export GNSIS_SIMPLEMEM_INTERNAL_TOKEN='<at least 32 chars>'
export GNSIS_SIMPLEMEM_URL='http://127.0.0.1:8765'
export GNSIS_SIMPLEMEM_DATA_DIR='/tmp/simplemem-data'
/tmp/smvenv/bin/uvicorn tools.simplemem.sidecar:app --host 127.0.0.1 --port 8765 --app-dir tools/simplemem > /tmp/sidecar.log 2>&1 &
```

- `/health` is unauthenticated; `/ready` needs `X-GNSIS-SimpleMem-Token`.
- First request after boot loads CLIP (~3.5s cold start) — expect one transient 500/timeout.
- `sidecar.request` telemetry lines appear in `/tmp/sidecar.log`.

## Env for service-level tests

`/health` needs `DATABASE_URL` + `REDIS_URL` even with `memory_backend=simplemem`:

```bash
DATABASE_URL='sqlite:////tmp/gnsis-test.db' REDIS_URL='redis://127.0.0.1:6379/0' GNSIS_MEMORY_BACKEND=simplemem \
  python -m gnsis.service.api   # or hit the FastAPI app in-process
```

## Gotchas

- `SimpleMemProvider(url)` reads `GNSIS_SIMPLEMEM_INTERNAL_TOKEN` from env when `token=` is not passed (this used to be a bug — env read sat inside `if not url:`). `api.py`/`tasks.py` call it without `token=`; the codememory mirror passes it explicitly.
- Multi-word `/query` requests 500 with `OpenAIError` unless `OPENAI_API_KEY`/`SIMPLEMEM_API_KEY` is set — upstream needs an LLM for entity extraction. Single-term queries work keyless. `SessionMemoryRecall` degrades to `[]`; `SimpleMemProvider` raises `SimpleMemUnavailable`.
- Short token (<32 chars) is refused at `/ready` (500 by design).
- Bad token → 401; missing/invalid namespace → 400; query on missing namespace → 404.
- S3 durable archive (`durableArchive:true`) needs bucket creds — untested without them. Media endpoints (image/audio/video) need LLM caption keys.

## Quick smoke

```python
from gnsis.memory.simplemem import SimpleMemProvider
p = SimpleMemProvider()  # reads env
p.assert_ready()
p.write_episode(session_id="sess-1", start_sec=12.0, end_sec=30.0,
                summary="deployed the red lantern", memory_type="episode",
                provenance={"source": "smoke"}, approved=True)
p.search("lantern", top_k=4)
p.recent(top_k=4)
```
