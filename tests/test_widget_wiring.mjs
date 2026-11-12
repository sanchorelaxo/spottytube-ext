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

test("the 42s cap is on stored seconds, and every click carries the speed", () => {
  assert.doesNotMatch(content, /setLimit/);
  assert.match(content, /window\.SpottySpeed\.outputSeconds\(s\.duration, sp\.speed\)/);
  // The raw-range verdict must not veto a fast selection: 63s of source
  // heard at 1.5x is a 42s sample, and the deck limit judges stored seconds.
  assert.doesNotMatch(content, /Boolean\(s\.overLimit\)/);
  assert.match(content, /stored > MAX_LOOP \+ 1e-9/);
  // Both clicks into the pair (manual tap and auto-end) pass the heard
  // speed, so the state machine judges the same seconds the deck will.
  // The auto-end click goes through clampOutPoint first, then sp.speed.
  assert.equal((content.match(/pair\.click\(at, rate\.state\(\)\.speed\)/g) || []).length, 1);
  assert.equal((content.match(/pair\.click\(at, sp\.speed\)/g) || []).length, 1);
  assert.doesNotMatch(content, /pair\.click\(at\)/);
  // The send gate re-checks the stored length, not the raw-range flag.
  assert.doesNotMatch(content, /!s\.complete \|\| s\.overLimit/);
});

test("a range that would overrun the deck once stored is flagged", () => {
  assert.match(content, /stored > MAX_LOOP/);
  assert.match(content, /overDeck/);
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

test("the widget offers neither a deck choice nor a save-mode row", () => {
  // Mode moved to the settings panel (B1) and decks are gone entirely.
  assert.doesNotMatch(content, /data-deck=/);
  assert.doesNotMatch(content, /data-mode=/);
  assert.match(content, /class="gear"/);
  assert.match(content, /panel settings/);
  const payload = content.slice(content.indexOf("type: \"sample\""));
  const sent = payload.slice(0, payload.indexOf("});"));
  assert.doesNotMatch(sent, /deck:/);
  assert.doesNotMatch(sent, /mode:/);
});

test("a done job reports the on-unit tape path, not a generated wav path", () => {
  assert.match(content, /\$\{r\.tape\}\/\$\{r\.slot\}\.WAV/);
  assert.doesNotMatch(content, /r\.card_path/);
  assert.doesNotMatch(content, /r\.track/);
});

test("cached settings survive a reload", () => {
  // The widget state no longer carries a mode; settings are cached under
  // their own key and re-applied (including the stored default mode) on load.
  assert.doesNotMatch(content, /mode: ui\.mode/);
  assert.match(content, /spottytubeSettings: cfg/);
  assert.match(content, /\["spottytube", "spottytubeSettings"\]/);
  assert.match(content, /applySettings\(/);
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
  assert.match(fn, /pair\.click\(at, sp\.speed\)/); // same state machine as a manual SET END, at the heard speed
  assert.match(fn, /clampOutPoint\(pair\.state\(\)\.start, raw, sp\.speed\)/); // trimmed so the stored length can never edge past the deck cap
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

// --- labels and the bridge restart button ----------------------------------

const background = readFileSync(join(ext, "background.js"), "utf8");

test("every settings row label points at its own control", () => {
  // Brave flags "<label> isn't associated with a form field": an unassociated
  // label is a dead click target and an a11y error on every rendered row.
  assert.match(content, /input\.id = `spottytube-set-\$\{field\.key\}`/);
  assert.match(content, /label\.htmlFor = input\.id/);
});

test("the readout cells pair label and output with for/id", () => {
  assert.match(content, /<label for="spottytube-out-start">START<\/label><output id="spottytube-out-start">/);
  assert.match(content, /<label for="spottytube-out-end">END<\/label><output id="spottytube-out-end">/);
  assert.match(content, /<label for="spottytube-out-len">LEN<\/label><output id="spottytube-out-len">/);
  assert.doesNotMatch(content, /<label>START<\/label>/);
});

test("the settings panel carries a restart-bridge button below the actions", () => {
  const actions = content.indexOf('class="sactions"');
  const restart = content.indexOf('class="s-restart"');
  assert.ok(actions > -1, "settings actions row missing");
  assert.ok(restart > actions, "restart row must sit under Save/Cancel");
  assert.match(content, /Restart bridge service</);
  assert.match(content, /srestart: root\.querySelector\("\.s-restart"\)/);
});

test("restarting asks the worker, waits for health, then re-syncs settings", () => {
  assert.match(content, /type: "restart"/);
  const fn = content.slice(content.indexOf("async function restartBridge"));
  assert.match(fn, /type: "health"/);
  assert.match(fn, /syncSettings\(\)/);
  assert.match(fn, /back\.version/);
});

test("the worker posts /restart to the bridge", () => {
  assert.match(background, /restart: \(\) => call\("\/restart", \{ method: "POST" \}\)/);
});

// --- the settings panel must fit the viewport: scroll body, pinned footer ---

test("the settings panel is a flex column: scrolling body, pinned footer", () => {
  // A max-height parent clips; only a flex column with min-height:0 on the
  // body makes overflow-y:auto actually scroll (and keeps the buttons reachable).
  assert.match(content, /\.panel\.settings\s*{[^}]*display:\s*flex;[^}]*flex-direction:\s*column/s);
  assert.match(content, /\.panel\.settings\[hidden\]\s*{\s*display:\s*none;\s*}/);
  assert.match(content, /\.sbody\s*{[^}]*overflow-y:\s*auto;[^}]*min-height:\s*0/s);
  assert.match(content, /\.sfoot\s*{[^}]*flex:\s*none/s);
  const iBody = content.indexOf('<div class="sbody">');
  const iFields = content.indexOf('<div class="sfields">');
  const iFoot = content.indexOf('<div class="sfoot">');
  const iSave = content.indexOf('class="s-save"');
  const iRestart = content.indexOf('class="s-restart"');
  assert.ok(iBody > -1, "sbody exists");
  assert.ok(iFields > iBody, "fields live inside the scrolling body");
  assert.ok(iFoot > iFields, "the footer comes after the scrolling body");
  assert.ok(iSave > iFoot && iRestart > iSave, "save and restart are pinned in the footer");
});

test("the scrolling body carries a visible scrollbar", () => {
  assert.match(content, /\.sbody\s*{[^}]*scrollbar-width:\s*thin/s);
  assert.match(content, /\.sbody::-webkit-scrollbar\s*{/);
});

test("opening settings clamps the tall panel back into the viewport", () => {
  // The copied drag anchor was clamped for the small widget, not the tall
  // panel: dragged low, Save/Cancel/Restart land under the viewport edge.
  assert.match(content, /function clampIntoView\(/);
  assert.match(content, /clampIntoView\(el\.settings\)/);
  const fn = content.slice(content.indexOf("function clampIntoView"), content.indexOf("function clampIntoView") + 900);
  assert.match(fn, /getBoundingClientRect/);
  assert.match(fn, /innerHeight/);
});
