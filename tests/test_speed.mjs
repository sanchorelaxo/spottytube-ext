// Playback-speed model tests. Run: node --test tests/
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "extension", "speed.js"), "utf8");

function load() {
  const sandbox = { window: {}, module: { exports: {} } };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox.window.SpottySpeed;
}

const { create, normalize, label, outputSeconds, fits,
        MIN_SPEED, MAX_SPEED, DEFAULT_SPEED } = load();

test("starts at 1.00x with snap off and no player seen", () => {
  const s = JSON.parse(JSON.stringify(create().state()));
  assert.deepEqual(s, { speed: 1, snap: false, player: null, label: "1.00x" });
});

test("the range is the one the player offers", () => {
  assert.equal(MIN_SPEED, 0.25);
  assert.equal(MAX_SPEED, 2);
  assert.equal(DEFAULT_SPEED, 1);
});

test("label always carries two decimals", () => {
  assert.equal(label(1), "1.00x");
  assert.equal(label(0.5), "0.50x");
  assert.equal(label(1.75), "1.75x");
});

test("normalize clamps to the range the player offers", () => {
  assert.equal(normalize(0.1), MIN_SPEED);
  assert.equal(normalize(5), MAX_SPEED);
});

test("normalize rounds to hundredths, which is what the slider steps in", () => {
  assert.equal(normalize(1.234), 1.23);
  assert.equal(normalize(0.7499999), 0.75);
});

test("normalize turns junk into the default, never NaN", () => {
  for (const junk of [undefined, null, NaN, "", "fast", {}, true]) {
    assert.equal(normalize(junk), 1, `for ${String(junk)}`);
  }
  // a numeric string is a number, not junk
  assert.equal(normalize("1.5"), 1.5);
});

test("moving the slider sets the speed while snap is off", () => {
  const s = create();
  s.set(1.25);
  assert.equal(s.state().speed, 1.25);
  assert.equal(s.state().label, "1.25x");
  assert.equal(s.state().snap, false);
});

test("moving the slider while snapped is ignored, not fought over", () => {
  const s = create({ snap: true, speed: 1.5 });
  s.set(0.5);
  assert.equal(s.state().speed, 1.5);
});

test("turning snap on adopts the rate that is playing right now", () => {
  const s = create();
  s.follow(1.75);                    // seen, but not applied: snap is off
  assert.equal(s.state().speed, 1);
  assert.equal(s.state().player, 1.75);

  s.toggleSnap(1.75);
  assert.equal(s.state().snap, true);
  assert.equal(s.state().speed, 1.75);
});

test("turning snap on with no live rate uses the last one seen", () => {
  const s = create();
  s.follow(1.25);
  s.toggleSnap();
  assert.equal(s.state().snap, true);
  assert.equal(s.state().speed, 1.25);
});

test("turning snap on with nothing seen keeps the slider value", () => {
  const s = create({ speed: 0.75 });
  s.toggleSnap();
  assert.equal(s.state().snap, true);
  assert.equal(s.state().speed, 0.75);
});

test("snapped speed follows the player, including the normal rate", () => {
  const s = create({ snap: true });
  s.follow(2);
  assert.equal(s.state().speed, 2);
  s.follow(0.5);
  assert.equal(s.state().speed, 0.5);
  s.follow(1);
  assert.equal(s.state().speed, 1);
});

test("after a range change the widget is out of step: the next follow fixes it", () => {
  const s = create({ snap: true, speed: 1.5 });
  s.follow(0.75);
  assert.equal(s.state().label, "0.75x");
});

test("a nonsense rate never reaches the slider", () => {
  const s = create({ snap: true, speed: 1.25 });
  for (const junk of [0, -1, NaN, Infinity, null, undefined, "loud"]) {
    s.follow(junk);
    assert.equal(s.state().speed, 1.25, `for ${String(junk)}`);
  }
});

test("turning snap off keeps whatever the player had reached", () => {
  const s = create({ snap: true });
  s.follow(1.5);
  s.toggleSnap();
  assert.equal(s.state().snap, false);
  assert.equal(s.state().speed, 1.5);
  s.set(0.25);
  assert.equal(s.state().speed, 0.25);
});

test("what the deck will hold is the source range divided by the speed", () => {
  assert.equal(outputSeconds(10, 1), 10);
  assert.equal(outputSeconds(10, 2), 5);
  assert.equal(outputSeconds(10, 0.5), 20);
  assert.equal(outputSeconds(10, 0.25), 40);
  assert.equal(outputSeconds(null, 2), null);
});

test("speed changes whether a range fits on a 42s deck", () => {
  assert.equal(fits(42, 1), true);
  assert.equal(fits(42, 2), true);
  assert.equal(fits(42, 0.5), false);      // 84s once slowed down
  assert.equal(fits(21, 0.5), true);       // exactly 42s
  assert.equal(fits(21, 0.49), false);
  assert.equal(fits(null, 1), false);
});

test("saved state applied after the widget is alive replaces the defaults", () => {
  const s = create();
  s.set(0.5);
  s.restore({ speed: 1.5, snap: true, player: 1.5 });
  assert.equal(s.state().speed, 1.5);
  assert.equal(s.state().snap, true);
  assert.equal(s.state().player, 1.5);
});

test("saved state comes back the way it went in", () => {
  const s = create({ speed: 1.25, snap: true });
  assert.equal(s.state().speed, 1.25);
  assert.equal(s.state().snap, true);
  const again = create(JSON.parse(JSON.stringify(s.state())));
  assert.equal(again.state().speed, 1.25);
  assert.equal(again.state().snap, true);
});
