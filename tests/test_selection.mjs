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

const { createPair, MAX_LOOP } = load();

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
