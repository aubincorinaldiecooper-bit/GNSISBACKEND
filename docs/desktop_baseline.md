# GNSIS desktop Host performance baseline

Filled after a representative Electron Host run, per `desktop_chassis_decision.md` / `desktop_execution_agent.md`. Three-band thresholds (target / warning / migration-review) are set from this baseline — not before it.

## Environment

- Machine/model:
- CPU arch:
- RAM:
- OS/version:
- Electron/Host build:
- Daemon build:
- Capture source + resolution:
- Configured frame rate:
- Mic/output device:
- Bluetooth involved:
- Session duration:
- Measurement method: `desktop/src/bench/bench.ts` + OS process sampling
- Provider local or remote:

## Launch and recovery

| Metric | p50 | p95 | p99 | Band |
| --- | --- | --- | --- | --- |
| cold launch -> usable UI | | | | |
| warm launch -> usable UI | | | | |
| host -> daemon initial connect | | | | |
| daemon reconnect (endpoint available) | | | | |
| sleep/wake recovery | | | | |

Correctness gates: no stale audio replay, no revived output epoch, no duplicated background result.

## Audio path (Host contribution only)

| Metric | p50 | p95 | p99 | Band |
| --- | --- | --- | --- | --- |
| mic capture -> daemon receive | | | | |
| audio chunk -> playback scheduled | | | | |
| playback scheduled -> started ACK | | | | |
| cancel -> actual stop | | | | |
| underruns / gaps / dup chunks | | | | |
| stale chunks rejected | | | | |

## Visual path

| Metric | p50 | p95 | p99 | Band |
| --- | --- | --- | --- | --- |
| capture -> daemon receive | | | | |
| host-side frame loss | | | | |
| timestamp monotonicity violations | | | | |
| source switch recovery | | | | |

## Resource use (full Host process group)

| Metric | idle | active | Band |
| --- | --- | --- | --- |
| CPU (% one core) | | | |
| RSS (MB) | | | |
| post-session memory growth | | | |
| battery/thermal notes | | | |

## Long-session notes (30-60 min)

- memory creep:
- audio drift:
- capture degradation:
- playback lag growth:
- reconnect-state issues:
- device handle leaks:
