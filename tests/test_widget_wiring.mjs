// Wiring checks for the widget. The DOM is not simulated here; these guard the
// contract between content.js and the files it loads, which is where a rename
// or a dropped field would otherwise pass silently.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const ext = join(here, "..", "extension");
const content = readFileSync(join(ext, "content.js"), "utf8");
const manifest = JSON.parse(readFileSync(join(ext, "manifest.json"), "utf8"));

const scripts = manifest.content_scripts[0].js;

test("speed.js is loaded before content.js uses it", () => {
  assert.ok(scripts.includes("speed.js"));
  assert.ok(scripts.indexOf("speed.js") < scripts.indexOf("content.js"));
});

test("the widget carries a slider over the player's range", () => {
  assert.match(content, /type="range"/);
  assert.match(content, /min="0\.25"/);
  assert.match(content, /max="2"/);
  assert.match(content, /step="0\.05"/);
});

test("the widget carries a snap-to-player control", () => {
  assert.match(content, /class="snap"/);
  assert.match(content, /SNAP TO PLAYER/);
});

test("the speed is sent with the sample request", () => {
  const payload = content.slice(content.indexOf("type: \"sample\""));
  assert.match(payload, /speed:\s*rate\.state\(\)\.speed/);
});

test("the player's rate is read and followed", () => {
  assert.match(content, /playbackRate/);
  assert.match(content, /rate\.follow\(/);
  assert.match(content, /rate\.toggleSnap\(/);
  assert.match(content, /setInterval\(\(\) => \{ if \(alive\(\)\) watchRate\(\)/);
});

test("the 42s cap stays on the picked range, whatever the speed", () => {
  assert.doesNotMatch(content, /setLimit/);
  assert.match(content, /window\.SpottySpeed\.outputSeconds\(s\.duration, sp\.speed\)/);
});

test("a slowed range that would overrun the deck is flagged", () => {
  assert.match(content, /stored > MAX_LOOP/);
});

test("the bar names the version that is running", () => {
  assert.match(content, /chrome\.runtime\.getManifest\(\)\.version/);
  assert.match(content, /class="ver"/);
});

test("a widget outliving its extension shuts itself down", () => {
  assert.match(content, /function alive\(\)/);
  assert.match(content, /function teardown\(\)/);
  assert.match(content, /chrome\.runtime\.id/);
  // every poller checks before it calls chrome.*
  assert.equal((content.match(/alive\(\)/g) || []).length >= 5, true);
  assert.match(content, /clearInterval\(rateTimer\)/);
});

test("storage writes use the callback form, never a bare promise", () => {
  assert.match(content, /void chrome\.runtime\.lastError/);
});

test("speed and snap survive a reload", () => {
  assert.match(content, /speed: sp\.speed, snap: sp\.snap/);
  assert.match(content, /rate\.restore\(s\)/);
});

test("the widget offers a save mode, not a deck choice", () => {
  assert.doesNotMatch(content, /data-deck=/);
  assert.match(content, /data-mode="bump"/);
  assert.match(content, /data-mode="append"/);
  const payload = content.slice(content.indexOf("type: \"sample\""));
  assert.match(payload, /mode:\s*ui\.mode/);
  assert.doesNotMatch(payload.slice(0, payload.indexOf("});")), /deck:/);
});

test("a done job reports the on-unit tape path, not a generated wav path", () => {
  assert.match(content, /\$\{r\.tape\}\/\$\{r\.slot\}\.WAV/);
  assert.doesNotMatch(content, /r\.card_path/);
  assert.doesNotMatch(content, /r\.track/);
});

test("the save mode survives a reload", () => {
  assert.match(content, /mode: ui\.mode, collapsed/);
  assert.match(content, /s\.mode === "append"/);
});

// --- countdown to the auto out-point ---------------------------------------

test("the widget shows a countdown row, hidden until the in-point is picked", () => {
  assert.match(content, /class="countdown" hidden/);
  assert.match(content, /AUTO-END IN/);
  assert.match(content, /renderCountdown\(/);
});

test("the countdown is armed by every action that selects an in-point", () => {
  assert.match(content, /countdown\.arm\(Date\.now\(\)\)/);
  const sync = content.slice(content.indexOf("function syncCountdown"));
  assert.match(sync, /action === "start"/);
  assert.match(sync, /action === "restart"/);
  assert.match(sync, /action === "start-moved"/);
  // a picked out-point or a reset stops the count
  assert.match(sync, /countdown\.disarm\(\)/);
});

test("elapse with no out-point auto-clicks the player position as the end", () => {
  assert.match(content, /function autoEnd\(/);
  assert.match(content, /st\.expired/);
  const fn = content.slice(content.indexOf("function autoEnd"), content.indexOf("function", content.indexOf("function autoEnd") + 10));
  assert.match(fn, /pair\.click\(at\)/);        // same state machine as a manual SET END
  assert.match(fn, /position\(\)/);             // the player's position at the elapse
  assert.match(fn, /syncCountdown\(result\.action\)/);
});

test("the flash is model-driven, red, and only in the danger window", () => {
  assert.match(content, /countdown\.tick\(Date\.now\(\)\)/);
  assert.match(content, /classList\.toggle\("danger", st\.danger\)/);
  assert.match(content, /classList\.toggle\("blink-off", st\.danger && !st\.flashOn\)/);
  assert.match(content, /\.countdown\.danger output \{ color: #ff5252/);
  assert.match(content, /\.countdown\.danger\.blink-off/);
});

test("the countdown ticker checks alive() and is cleared on teardown", () => {
  assert.match(content, /cdTimer = setInterval\(tickCountdown/);
  assert.match(content, /clearInterval\(cdTimer\)/);
  const tick = content.slice(content.indexOf("function tickCountdown"));
  assert.match(tick, /if \(!alive\(\)\)/);
});
