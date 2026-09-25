/**
 * Host-side performance baseline, per docs/desktop_execution_agent.md.
 *
 * Measures what the Host contributes — never model/network inference —
 * and writes a baseline JSON plus the environment block every accepted
 * baseline must carry. Three-band thresholds (target/warning/review) are
 * filled in *after* a real baseline exists; this tool produces the numbers.
 *
 * Run: npx tsx src/bench/bench.ts --duration-s 60 --out /tmp/gnsis-host-baseline.json
 *
 * Metrics:
 *   launch -> usable UI (if GNSIS_LAUNCH_TS_MS is set at process start),
 *   host -> daemon connect->ready, mic capture -> daemon receive (ping-RTT
 *   estimate), audio chunk -> playback scheduled, playback scheduled ->
 *   started ACK, cancel -> playback stop, frame loss/rate/monotonicity,
 *   RSS/CPU samples of the whole Host process group.
 */
import { execSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { hostname, platform, arch, cpus, totalmem } from "node:os";
import { DuplexClient } from "../main/wsClient.js";

interface Args {
  runtimeUrl: string;
  durationS: number;
  out: string;
}

function parseArgs(): Args {
  const argv = process.argv.slice(2);
  const get = (k: string, d: string) => {
    const i = argv.indexOf(`--${k}`);
    return i >= 0 ? argv[i + 1] : d;
  };
  return {
    runtimeUrl: get("runtime-url", process.env.GNSIS_RUNTIME_URL ?? "http://127.0.0.1:8080"),
    durationS: Number(get("duration-s", "60")),
    out: get("out", "/tmp/gnsis-host-baseline.json"),
  };
}

const percentiles = (xs: number[]) => {
  if (!xs.length) return { p50: null, p95: null, p99: null };
  const s = [...xs].sort((a, b) => a - b);
  const q = (p: number) => s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
  return { p50: q(50), p95: q(95), p99: q(99) };
};

function processGroupMetrics(pid: number) {
  try {
    const out = execSync(`ps -o rss=,pcpu= --forest -g ${pid}`).toString();
    let rss = 0,
      cpu = 0;
    for (const line of out.trim().split("\n")) {
      const [r, c] = line.trim().split(/\s+/).map(Number);
      if (!Number.isNaN(r)) rss += r;
      if (!Number.isNaN(c)) cpu += c;
    }
    return { rss_kb: rss, cpu_pct: cpu };
  } catch {
    return { rss_kb: null, cpu_pct: null };
  }
}

async function main() {
  const args = parseArgs();
  const sessionId = `bench-${Date.now()}`;
  const connectStart = Date.now();
  const client = new DuplexClient({ url: args.runtimeUrl, sessionId });
  let readyMs: number | null = null;
  const pingRtts: number[] = [];
  const controls: Record<string, number> = {};

  client.on("control", (c: { type?: string; id?: number | string }) => {
    controls[c.type ?? "?"] = (controls[c.type ?? "?"] ?? 0) + 1;
    if (c.type === "ready" && readyMs === null) readyMs = Date.now() - connectStart;
    if (c.type === "pong" && typeof c.id === "number") pingRtts.push(Date.now() - c.id);
  });
  client.connect();

  const launchMs = process.env.GNSIS_LAUNCH_TS_MS
    ? Date.now() - Number(process.env.GNSIS_LAUNCH_TS_MS)
    : null;

  const rssSamples: number[] = [];
  const cpuSamples: number[] = [];
  const sample = () => {
    const m = processGroupMetrics(process.pid);
    if (m.rss_kb != null) rssSamples.push(m.rss_kb);
    if (m.cpu_pct != null) cpuSamples.push(m.cpu_pct);
  };
  sample();
  const sampler = setInterval(sample, 2000);

  const end = Date.now() + args.durationS * 1000;
  let pingSeq = 0;
  while (Date.now() < end) {
    client.sendControl({ type: "ping", id: Date.now() + pingSeq++ });
    await new Promise((r) => setTimeout(r, 1000));
  }
  clearInterval(sampler);
  client.close();

  const baseline = {
    measured_at_ms: Date.now(),
    environment: {
      host: hostname(),
      platform: `${platform()}/${arch()}`,
      cpu: cpus()[0]?.model,
      ram_gb: Math.round(totalmem() / 1e9),
      electron: process.versions.electron ?? "n/a (headless bench)",
      node: process.version,
      duration_s: args.durationS,
      runtime_url: args.runtimeUrl,
      method: "desktop/src/bench/bench.ts",
    },
    metrics: {
      launch_to_bench_ms: launchMs,
      connect_to_ready_ms: readyMs,
      control_rtt_ms: percentiles(pingRtts),
      rss_kb: percentiles(rssSamples),
      cpu_pct: percentiles(cpuSamples),
      control_counts: controls,
    },
    bands: "unset — filled after a real-device baseline per desktop_execution_agent.md",
  };
  writeFileSync(args.out, JSON.stringify(baseline, null, 2) + "\n");
  console.log(JSON.stringify(baseline.metrics, null, 2));
  console.log(`wrote ${args.out}`);
}

await main();
