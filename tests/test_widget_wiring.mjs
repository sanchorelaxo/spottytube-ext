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

// Pull a top-level (two-space indented) function's source text out of
// content.js so a test can run the SHIPPED body, not a re-implementation.
// Fail-closed: if the pattern stops matching, the test must blow up rather
// than silently assert against an empty string.
function extract(name) {
  const src = content.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}`));
  assert.ok(src, `could not extract ${name}() from content.js`);
  return src[0];
}

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

test("a select with no stored value falls back to the schema default", () => {
  // A <select> whose value matches no option silently shows the FIRST option,
  // so falling back to "" made the Placement mode row default to bump and then
  // persist bump on Save, never the schema's append. The select branch must
  // fall back to field.default instead.
  const fn = extract("fieldRow");
  const start = fn.indexOf('field.control === "select"');
  const end = fn.indexOf('field.control === "checkbox"');
  assert.ok(start > -1 && end > start, "the select branch must exist before we inspect it");
  const select = fn.slice(start, end);
  assert.match(select, /value == null \? String\(field\.default \?\? ""\)/);
  assert.doesNotMatch(select, /value == null \? ""/);
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

test("the scrolling body hides its scrollbar but still scrolls", () => {
  // Deliberate change: expanding Advanced outgrows the 78vh panel, so the
  // body scrolls the whole list and the scrollbar stays hidden. It was
  // "thin" before; a visible bar is not wanted here.
  assert.match(content, /\.sbody\s*{[^}]*overflow-y:\s*auto/s);
  assert.match(content, /\.sbody\s*{[^}]*scrollbar-width:\s*none/s);
  assert.match(content, /\.sbody::-webkit-scrollbar\s*{\s*width:\s*0;\s*height:\s*0;\s*}/);
  assert.doesNotMatch(content, /scrollbar-width:\s*thin/);
  assert.doesNotMatch(content, /::-webkit-scrollbar-thumb/);
  assert.doesNotMatch(content, /::-webkit-scrollbar-track/);
});

test("the advanced group is a collapsible section driven by the bridge", () => {
  assert.match(content, /section\.collapsible/);
  assert.match(content, /"ssec toggle"/);
  assert.match(content, /aria-expanded/);
  assert.match(content, /settingsState\.collapsed/);
  assert.match(content, /group\.hidden/);
});

test("a collapsed group hides its rows through the author rule", () => {
  // [hidden] alone loses to an author display rule, same trap as the panel.
  assert.match(content, /\.sgroup\[hidden\]\s*{\s*display:\s*none;\s*}/);
  assert.match(content, /collapsed:\s*new Map\(\)/);
});

test("neither the group nor the panel nests a second scroller", () => {
  const iGroup = content.indexOf(".sgroup");
  assert.ok(iGroup !== -1, "the .sgroup rule must exist before we inspect it");
  const afterGroup = content.slice(iGroup);
  const block = afterGroup.slice(0, afterGroup.indexOf("}") + 1);
  assert.doesNotMatch(block, /max-height|overflow/);
});

// The shipped function bodies run against plain-object stubs. The bridge sends
// objects now; a stale bridge may still send [key, label] pairs, and when it
// sends no sections at all the order is derived from the schema. All three
// paths are exercised through sectionOrder() itself, not a copy of its logic.
test("sectionOrder normalizes the new, stale and derived payload shapes", () => {
  const make = (sections) => new Function(
    "settingsState",
    extract("sectionOrder") + "\nreturn sectionOrder;"
  )({ sections });

  // 1. current bridge: object entries, with the collapsible hint preserved.
  const fixture = [
    { key: "audio", label: "Audio pipeline" },
    { key: "advanced", label: "Advanced", collapsible: true, collapsed: true }
  ];
  const ordered = make(fixture)([]);
  assert.deepEqual(ordered, [
    { key: "audio", label: "Audio pipeline", collapsible: false, collapsed: false },
    { key: "advanced", label: "Advanced", collapsible: true, collapsed: true }
  ]);

  // 2. stale bridge: legacy [key, label] pairs normalize to the object shape.
  const stale = make([["audio", "Audio pipeline"], ["advanced", "Advanced"]])("whatever"); 
  assert.deepEqual(stale, [
    { key: "audio", label: "Audio pipeline", collapsible: false, collapsed: false },
    { key: "advanced", label: "Advanced", collapsible: false, collapsed: false }
  ]);

  // 3. schema-derived fallback: one entry per distinct section, first-seen
  // order, and a repeated section key does not duplicate the entry.
  const derived = make([])([
    { key: "a1", section: "audio" },
    { key: "a2", section: "audio" },
    { key: "b1", section: "advanced" }
  ]);
  assert.deepEqual(derived, [
    { key: "audio", label: "audio", collapsible: false, collapsed: false },
    { key: "advanced", label: "advanced", collapsible: false, collapsed: false }
  ]);
});

// The collapse state is ephemeral (spec 71-74): closing the panel must forget
// which groups were opened, so the next open re-collapses Advanced. This runs
// the SHIPPED closeSettings() against plain stubs; delete the clear() and it
// fails.
test("closeSettings forgets the collapse state so the next open re-collapses", () => {
  const state = { open: true, collapsed: new Map() };
  const el = { settings: { hidden: false }, panel: { style: {} } };
  const closeSettings = new Function(
    "settingsState", "el",
    extract("closeSettings") + "\nreturn closeSettings;"
  )(state, el);

  state.collapsed.set("advanced", false);   // the user opened Advanced
  closeSettings();

  assert.equal(state.collapsed.size, 0, "the remembered collapse state must be dropped on close");
  assert.equal(state.open, false);
  assert.equal(el.settings.hidden, true);
});

// The collapsible head is a div with role="button", so it must answer the
// keyboard itself. Its keydown handler needs a real DOM to run, so this greps
// the shipped handler's slice (not the whole file) and fails if the listener,
// either key, preventDefault, or the toggle call goes missing.
test("the collapsible head toggles on both Enter and Space", () => {
  const fn = extract("renderFields");
  const i = fn.indexOf('addEventListener("keydown"');
  assert.ok(i !== -1, "the collapsible head must carry a keydown listener");
  const handler = fn.slice(i, fn.indexOf("});", i) + 3);
  assert.match(handler, /event\.key === "Enter"/);
  assert.match(handler, /event\.key === " "/);
  assert.match(handler, /event\.preventDefault\(\)/);
  assert.match(handler, /setOpen\(/);
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

// rnnoise needs a voice and the other methods ignore one, so the voice row is
// live only while arnndn is selected. This greps the shipped body (not a
// re-implementation) and fails if it stops reading the method, stops touching
// the picker, drops the listener, or is never wired into renderFields.
test("the voice picker is live only while arnndn is selected", () => {
  const src = extract("syncModelPicker");
  assert.match(src, /nr_method/);
  assert.match(src, /nr_model/);
  assert.match(src, /\.disabled\s*=/);
  assert.match(src, /"arnndn"/);
  assert.match(src, /addEventListener\("change"/);
  assert.match(extract("renderFields"), /syncModelPicker\(\)/);
  assert.match(src, /model\.disabled\s*=\s*method\.value\s*!==\s*"arnndn"/);
  assert.match(src, /pick\(\)/);
});

test("the speech shortcut sets the band, never posts itself", () => {
  const src = extract("installSpeechPreset");
  assert.match(src, /highpass_hz/);
  assert.match(src, /lowpass_hz/);
  assert.match(src, /Isolate audio speech/);
  assert.doesNotMatch(src, /settingsState\.inputs\.set/);
  assert.doesNotMatch(src, /collectValues/);
  // The call site: without it the shortcut never appears in the panel.
  assert.match(extract("renderFields"), /installSpeechPreset\(host\)/);
  // The band's values, not merely that some value is written.
  assert.match(content, /SPEECH_BAND = \{ highpass_hz: 200, lowpass_hz: 3000 \}/);
  // Both filters on and both cutoffs at the band: an AND, not an OR.
  assert.match(src, /matchesSpeechBand\(boxes\.highpass, true\)\s*&&/);
  // Each cutoff gets its own frequency, and the two are never swapped.
  assert.match(src, /setSpeechBandControl\(boxes\.highpass_hz, SPEECH_BAND\.highpass_hz\)/);
  assert.match(src, /setSpeechBandControl\(boxes\.lowpass_hz, SPEECH_BAND\.lowpass_hz\)/);
  // Ticking writes the cutoffs; unticking only clears the two boxes.
  assert.match(src, /if \(box\.checked\) \{/);
  // The shortcut's own listener. A bare /addEventListener\("change"/ would not
  // do: the sibling loop at :817 also matches it, so this names the box.
  assert.match(src, /box\.addEventListener\("change"/);
  // The checkbox branch compares, it does not merely read.
  assert.match(extract("matchesSpeechBand"), /input\.checked === want/);
  // The early return: an older bridge without the low-pass sends no row, so
  // the shortcut must bail rather than install a half-wired preset.
  assert.match(src, /if \(SPEECH_KEYS\.some\(\(key\) => !boxes\[key\]\)\) return;/);
  // The four fields re-derive the tick when one of them is edited by hand:
  // without this loop the box goes stale and claims a band that is not set.
  assert.match(src, /boxes\[key\]\.addEventListener\("change", sync\)/);
});

// Save closes the panel on success, exactly like Cancel, and reports the card
// note on the main status line instead of inside the (now gone) panel. Greps
// the SHIPPED saveSettings body so dropping the close or the status line fails
// here rather than only in a browser.
test("a successful save leaves the panel like cancelling does", () => {
  const src = extract("saveSettings");
  // The panel closes on success...
  assert.match(src, /closeSettings\(\)/);
  // ...exactly once, and only after the failed-save branch has already
  // returned: a rejected save must keep the panel (with its error text) open.
  assert.equal((src.match(/closeSettings\(\)/g) || []).length, 1);
  const failReturn = src.indexOf("if (!reply.ok)");
  assert.ok(failReturn !== -1, "the failure branch must stay in saveSettings");
  assert.ok(src.indexOf("closeSettings()") > failReturn,
    "closeSettings() must sit after the failed-save return");
  // The card note no longer goes into the panel...
  assert.doesNotMatch(src, /setSMsg\(msg/);
  // ...it is the status line message, warn-styled when the card has a caveat.
  assert.match(src, /setStatus\(message, kind\)/);
  assert.match(src, /message = card\.error \|\| card\.warning/);
  assert.match(src, /kind = "warn"/);
  // An unchanged save carries no card_config at all, so the read must tolerate
  // its absence.
  assert.match(src, /reply\.card_config \|\| \{\}/);
  // A written card says so on the status line.
  assert.match(src, /config\.txt written/);
});
