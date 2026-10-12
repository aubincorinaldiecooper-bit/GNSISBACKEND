import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { TapGesture } from "./tapGesture.js";

class Stream extends EventEmitter {
  setEncoding(): void {}
}

class FakeProcess extends EventEmitter {
  stdout = new Stream();
  stderr = new Stream();
  killed = false;
  kill(): boolean {
    this.killed = true;
    return true;
  }
}

test("helper tap events reach the existing gesture callback", () => {
  const child = new FakeProcess();
  const taps: string[] = [];
  const lines: string[] = [];
  const gesture = new TapGesture({
    platform: "darwin",
    helperPath: "/app/tap",
    spawn: () => child,
    onGesture: (kind) => taps.push(kind),
    log: (line) => lines.push(line),
  });
  gesture.start();
  child.stdout.emit("data", '{"status":"ready"}\n{"gesture":"double"}\n{"gesture":"single"}\n');
  assert.deepEqual(taps, ["double", "single"]);
  assert.deepEqual(lines, [
    "tap gesture: listening for a deliberate double tap",
    "tap gesture: double tap detected",
    "tap gesture: single tap detected",
  ]);
});

test("unsupported Macs keep the ordinary Dock and menu paths without retries", () => {
  const child = new FakeProcess();
  const timers: Array<() => void> = [];
  let spawned = 0;
  const gesture = new TapGesture({
    platform: "darwin",
    helperPath: "/app/tap",
    spawn: () => {
      spawned += 1;
      return child;
    },
    onGesture: () => assert.fail("unsupported hardware must not emit a gesture"),
    log: () => {},
    setTimeout: (fn) => {
      timers.push(fn);
      return fn;
    },
  });
  gesture.start();
  child.stdout.emit("data", '{"status":"unsupported"}\n');
  child.emit("close", 0);
  assert.equal(spawned, 1);
  assert.deepEqual(timers, []);
});

test("non-macOS hosts do not launch the native listener", () => {
  let spawned = false;
  new TapGesture({
    platform: "linux",
    spawn: () => {
      spawned = true;
      return new FakeProcess();
    },
    onGesture: () => {},
    log: () => {},
  }).start();
  assert.equal(spawned, false);
});


test("malformed helper output is ignored without activating voice", () => {
  const child = new FakeProcess();
  let taps = 0;
  const lines: string[] = [];
  const gesture = new TapGesture({
    platform: "darwin",
    helperPath: "/app/tap",
    spawn: () => child,
    onGesture: () => taps++,
    log: (line) => lines.push(line),
  });
  gesture.start();
  child.stdout.emit("data", "not json\n{\"gesture\":\"triple\"}\n");
  assert.equal(taps, 0);
  assert.deepEqual(lines, ["tap gesture: ignored malformed helper output"]);
});

test("an unavailable bundled helper fails quietly instead of retrying forever", () => {
  const child = new FakeProcess();
  const timers: Array<() => void> = [];
  const lines: string[] = [];
  const gesture = new TapGesture({
    platform: "darwin",
    helperPath: "/missing/tap",
    spawn: () => child,
    onGesture: () => {},
    log: (line) => lines.push(line),
    setTimeout: (fn) => {
      timers.push(fn);
      return fn;
    },
  });
  gesture.start();
  child.emit("error", new Error("ENOENT"));
  child.emit("close", null);
  assert.equal(timers.length, 0);
  assert.match(lines[0], /helper failed/);
});

test("turning the preference off and on restarts the listener", () => {
  const children = [new FakeProcess(), new FakeProcess()];
  let spawned = 0;
  const gesture = new TapGesture({
    platform: "darwin",
    helperPath: "/app/tap",
    spawn: () => children[spawned++],
    onGesture: () => {},
    log: () => {},
  });
  gesture.start();
  gesture.stop();
  gesture.start();
  assert.equal(children[0].killed, true);
  assert.equal(spawned, 2);
});
