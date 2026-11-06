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
