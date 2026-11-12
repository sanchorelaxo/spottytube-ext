// Selection state machine tests. Run: node --test tests/
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "extension", "selection.js"), "utf8");

function load() {
  const sandbox = { window: {}, module: { exports: {} } };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox.window.SpottySelection;
}

const { createPair, createCountdown, clampOutPoint, MAX_LOOP, DANGER_SECONDS, FLASH_MS } = load();

test("starts empty", () => {
  const s = JSON.parse(JSON.stringify(createPair().state()));
  assert.deepEqual(s, { start: null, end: null, duration: null, complete: false, overLimit: false });
});

test("first click sets the in-point, floored to whole seconds", () => {
  const pair = createPair();
  const r = pair.click(61.87);
  assert.equal(r.action, "start");
  assert.equal(r.start, 61);
  assert.equal(r.end, null);
  assert.equal(r.complete, false);
});

test("second click sets the out-point and completes the pair", () => {
  const pair = createPair();
  pair.click(61);
  const r = pair.click(72.4);
  assert.equal(r.action, "end");
  assert.equal(r.end, 72);
  assert.equal(r.duration, 11);
  assert.equal(r.complete, true);
});

test("a third click starts a new pair", () => {
  const pair = createPair();
  pair.click(10);
  pair.click(20);
  const r = pair.click(30);
  assert.equal(r.action, "restart");
  assert.equal(r.start, 30);
  assert.equal(r.end, null);
  assert.equal(r.complete, false);
});

test("clicking behind the in-point moves the in-point", () => {
  const pair = createPair();
  pair.click(30);
  const r = pair.click(12);
  assert.equal(r.action, "start-moved");
  assert.equal(r.start, 12);
  assert.equal(r.end, null);
});

test("over the 42s deck limit is flagged, not silently accepted", () => {
  const pair = createPair();
  pair.click(0);
  const r = pair.click(MAX_LOOP + 8);
  assert.equal(r.action, "end-too-long");
  assert.equal(r.overLimit, true);
  assert.equal(r.duration, MAX_LOOP + 8);
});

test("exactly at the limit is allowed", () => {
  const pair = createPair();
  pair.click(5);
  const r = pair.click(5 + MAX_LOOP);
  assert.equal(r.action, "end");
  assert.equal(r.overLimit, false);
});

test("the cap is on stored seconds: the range divided by the heard speed", () => {
  // 63s of source heard at 1.5x is a 42s sample: the deck fits it.
  const fast = createPair();
  fast.click(0);
  const r = fast.click(63, 1.5);
  assert.equal(r.action, "end");
  assert.equal(r.overLimit, false);
  assert.equal(r.duration, 63);

  // The same range at 1x is 63s on the deck: too long.
  const plain = createPair();
  plain.click(0);
  assert.equal(plain.click(63).action, "end-too-long");

  // 85s fits at no speed the player offers (fastest is 2x -> 42.5s).
  const huge = createPair();
  huge.click(0);
  assert.equal(huge.click(85, 2).action, "end-too-long");
});

test("a junk speed falls back to 1x rather than dividing by garbage", () => {
  for (const bad of [0, -1, NaN, Infinity, "1.5", null, undefined]) {
    const pair = createPair();
    pair.click(0);
    assert.equal(pair.click(60, bad).action, "end-too-long", `speed ${bad}`);
  }
});

test("state() judges the limit at the speed the clicks were heard at", () => {
  const fast = createPair();
  fast.click(0, 1.5);
  fast.click(63, 1.5);
  assert.equal(fast.state().overLimit, false);

  const strict = createPair();
  strict.click(0, 1);
  strict.click(63, 1);
  assert.equal(strict.state().overLimit, true);
});

test("clampOutPoint trims an auto end to what the deck stores at the heard speed", () => {
  // The wall-clock elapse at 0.75x can sit 32s past the in-point, which the
  // deck would store as 42.7s. Clamped to a 31s range -> 41.3s stored: fits.
  assert.equal(clampOutPoint(100, 132, 0.75), 131);
  const pair = createPair();
  pair.click(100, 0.75);
  const r = pair.click(clampOutPoint(100, 132, 0.75), 0.75);
  assert.equal(r.action, "end");
  assert.equal(r.overLimit, false);

  // Faster speeds allow a proportionally longer source range: 42s stored
  // at 1.5x is 63s of source.
  assert.equal(clampOutPoint(0, 90, 1.5), 63);

  // Inside the cap: never extend, never shorten.
  assert.equal(clampOutPoint(10, 20, 1), 20);
  assert.equal(clampOutPoint(10, 5, 1), 5);

  // A junk speed falls back to 1x rather than clamping by garbage.
  assert.equal(clampOutPoint(0, 50, NaN), 42);
});

test("reset clears both indexes", () => {
  const pair = createPair();
  pair.click(10);
  pair.click(20);
  const r = pair.reset();
  assert.equal(r.action, "reset");
  assert.equal(r.start, null);
  assert.equal(r.end, null);
});

test("a missing playback position is rejected without changing state", () => {
  const pair = createPair();
  pair.click(10);
  for (const bogus of [null, undefined, NaN, -1]) {
    const r = pair.click(bogus);
    assert.equal(r.action, "rejected");
    assert.equal(r.start, 10);
  }
});

// --- countdown -------------------------------------------------------------
// Armed when the in-point is picked; runs to zero on wall time; the last
// DANGER_SECONDS flash every FLASH_MS; at zero the widget auto-ends.

test("the countdown constants are the 5 s warning and the 500 ms flash", () => {
  assert.equal(DANGER_SECONDS, 5);
  assert.equal(FLASH_MS, 500);
});

test("the countdown is idle until armed", () => {
  const cd = createCountdown();
  assert.equal(cd.running(), false);
  // JSON round-trip: objects minted inside the vm context have another
  // Object.prototype, and strict deepEqual compares prototypes.
  assert.deepEqual(JSON.parse(JSON.stringify(cd.tick(0))), {
    running: false, remaining: null, expired: false, danger: false, flashOn: false
  });
});

test("arming starts the count at the 42 s ceiling", () => {
  const cd = createCountdown();
  cd.arm(1000);
  assert.equal(cd.running(), true);
  const st = cd.tick(1000);
  assert.equal(st.running, true);
  assert.equal(st.remaining, MAX_LOOP);
  assert.equal(st.expired, false);
  assert.equal(st.danger, false);
});

test("the count decrements second by second and never goes negative", () => {
  const cd = createCountdown();
  cd.arm(0);
  assert.equal(cd.tick(1000).remaining, MAX_LOOP - 1);
  assert.equal(cd.tick(41000).remaining, 1);
  assert.equal(cd.tick(41500).remaining, 1);   // ceil: "1" until the last second is up
});

test("the last 5 seconds are flagged danger, the rest are not", () => {
  const cd = createCountdown();
  cd.arm(0);
  assert.equal(cd.tick(36000).danger, false);  // 6 s left
  assert.equal(cd.tick(37000).danger, true);   // 5 s left
  assert.equal(cd.tick(41900).danger, true);   // 0.1 s left
});

test("danger flashes: the phase flips every 500 ms, and only in danger", () => {
  const cd = createCountdown();
  cd.arm(0);
  assert.equal(cd.tick(37000).flashOn, true);   // 5000 ms left: visible
  assert.equal(cd.tick(37500).flashOn, false);  // 4500 ms left: hidden
  assert.equal(cd.tick(38000).flashOn, true);   // 4000 ms left: visible
  assert.equal(cd.tick(10000).flashOn, false);  // 32 s left: never flashes
});

test("at zero the countdown reports expired, and stays expired until disarmed", () => {
  const cd = createCountdown();
  cd.arm(0);
  const st = cd.tick(42000);
  assert.equal(st.expired, true);
  assert.equal(st.remaining, 0);
  assert.equal(st.danger, false);
  assert.equal(st.flashOn, false);
  assert.equal(cd.tick(50000).expired, true);
});

test("disarm stops the count", () => {
  const cd = createCountdown();
  cd.arm(0);
  cd.disarm();
  assert.equal(cd.running(), false);
  assert.equal(cd.tick(10).running, false);
});

test("re-arming moves the deadline", () => {
  const cd = createCountdown();
  cd.arm(0);
  cd.arm(10000);
  assert.equal(cd.tick(10000).remaining, MAX_LOOP);
  assert.equal(cd.tick(42000).remaining, 10);   // deadline is now 52000
});
