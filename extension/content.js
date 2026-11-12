// SpottyTube widget. Injected only on YouTube video pages by the manifest.
//
// The URL is taken from the page; the two time indexes are taken from the
// player's current position. One button drives both: click = in-point,
// second click = out-point, hold = reset.

(() => {
  "use strict";
  if (window.__spottytubeLoaded) return;
  window.__spottytubeLoaded = true;

  const pair = window.SpottySelection.createPair();
  const MAX_LOOP = window.SpottySelection.MAX_LOOP;
  const countdown = window.SpottySelection.createCountdown();
  const rate = window.SpottySpeed.create();
  const LONG_PRESS_MS = 550;
  const POLL_MS = 1200;
  const POLL_LIMIT_MS = 16 * 60 * 1000;
  const RATE_MS = 500;
  const COUNTDOWN_MS = 200;

  const ui = { collapsed: false, left: null, top: null,
             busy: false, timer: null, online: null, ready: false, transport: null };

  // --- page facts ---------------------------------------------------------

  const video = () => document.querySelector("video.html5-main-video") || document.querySelector("video");

  function videoId() {
    const fromQuery = new URLSearchParams(location.search).get("v");
    if (fromQuery) return fromQuery;
    const shorts = location.pathname.match(/^\/(?:shorts|live|embed)\/([A-Za-z0-9_-]{11})/);
    return shorts ? shorts[1] : null;
  }

  function pageUrl() {
    const id = videoId();
    return id ? `https://www.youtube.com/watch?v=${id}` : location.href;
  }

  function position() {
    const el = video();
    return el && isFinite(el.currentTime) ? el.currentTime : null;
  }

  function fmt(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  // --- widget -------------------------------------------------------------

  const host = document.createElement("div");
  host.id = "spottytube-host";
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
    <style>
      :host { all: initial; }
      .panel {
        position: fixed; z-index: 2147483000; width: 268px;
        right: 16px; bottom: 96px;
        background: #16181d; color: #e8e9ec;
        border: 1px solid #2b2f37; border-radius: 12px;
        box-shadow: 0 12px 32px rgba(0,0,0,.5);
        font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
        user-select: none; overflow: hidden;
      }
      .panel.collapsed .body { display: none; }
      .bar {
        display: flex; align-items: center; gap: 8px;
        padding: 9px 10px; background: #1c1f26; cursor: grab;
        border-bottom: 1px solid #262a33;
      }
      .bar.dragging { cursor: grabbing; }
      .dot { width: 8px; height: 8px; border-radius: 50%; background: #6b7280; flex: none; }
      .dot.on { background: #3ecf8e; box-shadow: 0 0 6px #3ecf8e88; }
      .dot.off { background: #e5484d; }
      .name { font-weight: 600; letter-spacing: .02em; }
      .ver { font-size: 10px; opacity: .45; }
      .vid { margin-left: auto; font: 11px/1 ui-monospace, monospace; color: #8b93a1; }
      .fold {
        all: unset; cursor: pointer; color: #8b93a1; padding: 2px 4px;
        font-size: 15px; line-height: 1; border-radius: 4px;
      }
      .fold:hover { color: #fff; background: #2a2e37; }
      .body { padding: 10px; display: grid; gap: 9px; }
      .times { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; }
      .cell { background: #0f1115; border: 1px solid #262a33; border-radius: 8px; padding: 6px 8px; }
      .cell label { display: block; font-size: 9px; letter-spacing: .12em; color: #7d8494; }
      .cell output { font: 500 16px/1.25 ui-monospace, SFMono-Regular, monospace; color: #f0f1f4; }
      .cell.empty output { color: #4b5262; }
      .len output { color: #f5c451; }
      .len.over output { color: #ff6b6b; }
      .countdown { display: flex; align-items: baseline; justify-content: center; gap: 6px; margin-top: -3px; }
      .countdown .cdlabel { font-size: 9px; letter-spacing: .12em; color: #7d8494; }
      .countdown output { font: 700 16px/1.2 ui-monospace, SFMono-Regular, monospace; color: #f0f1f4; }
      .countdown.danger .cdlabel { color: #ff5252; }
      .countdown.danger output { color: #ff5252; }
      .countdown.danger.blink-off .cdlabel, .countdown.danger.blink-off output { visibility: hidden; }
      .set {
        all: unset; display: block; box-sizing: border-box; width: 100%; text-align: center;
        padding: 11px 8px; border-radius: 8px; cursor: pointer;
        background: #f5c451; color: #16181d; font-weight: 700; letter-spacing: .06em;
        font-size: 12px; transition: filter .1s, background .1s;
      }
      .set:hover { filter: brightness(1.08); }
      .set.armed { background: #3ecf8e; }
      .set.holding { background: #e5484d; color: #fff; }
      .hint { font-size: 10.5px; color: #7d8494; text-align: center; margin-top: -3px; }
      .speedrow {
        display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center;
      }
      .speed {
        -webkit-appearance: none; appearance: none; width: 100%; height: 4px; margin: 0;
        border-radius: 4px; background: #262a33; outline: none; cursor: pointer;
      }
      .speed::-webkit-slider-thumb {
        -webkit-appearance: none; appearance: none; width: 14px; height: 14px;
        border-radius: 50%; background: #f5c451; border: 0; cursor: pointer;
      }
      .speed:disabled { opacity: .45; cursor: default; }
      .speed:disabled::-webkit-slider-thumb { background: #7d8494; cursor: default; }
      .rate {
        min-width: 48px; text-align: right;
        font: 500 12px/1 ui-monospace, SFMono-Regular, monospace; color: #f0f1f4;
      }
      .snap {
        all: unset; box-sizing: border-box; width: 100%; text-align: center; cursor: pointer;
        padding: 6px 0; border-radius: 7px; background: #0f1115;
        border: 1px solid #262a33; color: #98a0b0; font-size: 11px; letter-spacing: .05em;
      }
      .snap.on { background: #24303f; border-color: #3c5070; color: #dfe8f5; font-weight: 600; }
      .snap.on .snapdot { background: #3ecf8e; }
      .snapdot {
        display: inline-block; width: 6px; height: 6px; border-radius: 50%;
        background: #4b5262; margin-right: 5px; vertical-align: 1px;
      }
      .send {
        all: unset; box-sizing: border-box; width: 100%; text-align: center; cursor: pointer;
        padding: 9px; border-radius: 8px; background: #2a2e37; color: #e8e9ec;
        border: 1px solid #363b46; font-weight: 600; font-size: 12px;
      }
      .send:hover:not(:disabled) { background: #333844; }
      .send:disabled { opacity: .45; cursor: default; }
      .status {
        font-size: 11px; line-height: 1.35; color: #98a0b0;
        max-height: 84px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
      }
      .status.ok { color: #3ecf8e; }
      .status.err { color: #ff8a8a; }
      .status.busy { color: #f5c451; }
      .status a { color: #7fb4ff; }

      /* --- settings panel (gear button in the bar) ------------------------ */
      .gear {
        all: unset; cursor: pointer; color: #8b93a1; padding: 2px 4px;
        font-size: 14px; line-height: 1; border-radius: 4px;
      }
      .gear:hover { color: #fff; background: #2a2e37; }
      .panel.settings {
        width: 330px; max-height: 78vh;
        display: flex; flex-direction: column;
      }
      /* author display beats the UA [hidden] rule: the flex panel needs its
         own hide or the settings sheet would sit open on every page load */
      .panel.settings[hidden] { display: none; }
      .panel.settings .bar { cursor: default; flex: none; }
      .sbody {
        padding: 10px 10px 6px; overflow-y: auto; min-height: 0; flex: 1 1 auto;
        display: grid; gap: 4px; align-content: start;
        scrollbar-width: thin; scrollbar-color: #3a404c #16181d;
      }
      .sbody::-webkit-scrollbar { width: 10px; }
      .sbody::-webkit-scrollbar-track { background: #16181d; }
      .sbody::-webkit-scrollbar-thumb {
        background: #3a404c; border-radius: 5px; border: 2px solid #16181d;
      }
      .sbody::-webkit-scrollbar-thumb:hover { background: #4a515f; }
      .sfoot {
        flex: none; padding: 8px 10px 10px;
        border-top: 1px solid #262a33; background: #16181d;
      }
      .ssec {
        font-size: 10px; letter-spacing: .12em; text-transform: uppercase;
        color: #7d8494; margin: 8px 0 2px;
      }
      .ssec:first-child { margin-top: 0; }
      .srow { display: grid; grid-template-columns: 1fr 132px; gap: 8px; align-items: center; padding: 1px 0; }
      .srow label { font-size: 11.5px; color: #d6dae2; cursor: help; }
      .srow select, .srow input[type="number"], .srow input[type="text"] {
        background: #0f1115; border: 1px solid #262a33; border-radius: 6px;
        color: #f0f1f4; font: 12px/1.3 ui-monospace, SFMono-Regular, monospace;
        padding: 4px 6px; box-sizing: border-box; width: 100%;
      }
      .srow select:focus, .srow input:focus { outline: none; border-color: #f5c451; }
      .srow input[type="checkbox"] {
        justify-self: start; width: 16px; height: 16px; accent-color: #3ecf8e; cursor: pointer;
      }
      .smsg { font-size: 11px; color: #98a0b0; white-space: pre-wrap; min-height: 14px; }
      .smsg.ok { color: #3ecf8e; }
      .smsg.err { color: #ff8a8a; }
      .smsg.warn { color: #f5c451; }
      .sactions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px; }
      .s-save, .s-cancel {
        all: unset; box-sizing: border-box; text-align: center; cursor: pointer;
        padding: 8px; border-radius: 8px; font-weight: 600; font-size: 12px;
      }
      .s-save { background: #f5c451; color: #16181d; }
      .s-save:hover:not(:disabled) { filter: brightness(1.08); }
      .s-save:disabled { opacity: .5; cursor: default; }
      .s-cancel { background: #2a2e37; color: #e8e9ec; border: 1px solid #363b46; }
      .s-cancel:hover:not(:disabled) { background: #333844; }
      .s-cancel:disabled { opacity: .5; cursor: default; }
      .s-restart {
        all: unset; box-sizing: border-box; display: block; width: 100%;
        text-align: center; cursor: pointer; margin-top: 8px; padding: 7px 8px;
        border-radius: 8px; font-weight: 600; font-size: 11px;
        background: #20242c; color: #c9ced8; border: 1px solid #363b46;
      }
      .s-restart:hover:not(:disabled) { background: #2a2f3a; color: #fff; }
      .s-restart:disabled { opacity: .5; cursor: default; }

      [hidden] { display: none !important; }
    </style>
    <div class="panel">
      <div class="bar">
        <span class="dot" title="bridge status"></span>
        <span class="name">SpottyTube</span>
        <span class="ver" title="extension version"></span>
        <span class="vid"></span>
        <button class="gear" title="SpottyTube settings">&#9881;</button>
        <button class="fold" title="collapse">&#8211;</button>
      </div>
      <div class="body">
        <div class="times">
          <div class="cell empty" data-k="start"><label for="spottytube-out-start">START</label><output id="spottytube-out-start">&#8211;&#8211;</output></div>
          <div class="cell empty" data-k="end"><label for="spottytube-out-end">END</label><output id="spottytube-out-end">&#8211;&#8211;</output></div>
          <div class="cell len"><label for="spottytube-out-len">LEN</label><output id="spottytube-out-len">&#8211;&#8211;</output></div>
        </div>
        <div class="countdown" hidden><span class="cdlabel">AUTO-END IN</span><output>42s</output></div>
        <button class="set">SET START</button>
        <div class="hint">click: start, then end &middot; hold: reset</div>
        <div class="speedrow">
          <input class="speed" type="range" min="0.25" max="2" step="0.05" value="1"
                 aria-label="playback speed">
          <output class="rate">1.00x</output>
        </div>
        <button class="snap" aria-pressed="false"><span class="snapdot"></span><span class="snaptext">SNAP TO PLAYER</span></button>
        <button class="send" disabled>Send to deck</button>
        <div class="status"></div>
      </div>
    </div>
    <div class="panel settings" hidden>
      <div class="bar">
        <span class="dot" hidden></span>
        <span class="name">Settings</span>
        <span class="vid" title="every setting applies on save &#8212; no restart">applies on save</span>
        <button class="fold s-close" title="close settings">&#10005;</button>
      </div>
      <div class="sbody">
        <div class="sfields"></div>
      </div>
      <div class="sfoot">
        <div class="smsg"></div>
        <div class="sactions">
          <button class="s-save" title="validate, store and apply every setting now &#8212; takes effect on the next save, tick and config write, no restart">Save</button>
          <button class="s-cancel" title="discard the changes and close">Cancel</button>
        </div>
        <button class="s-restart" title="restart the local bridge service so it reloads the code on disk &#8212; about 2s downtime; unsaved edits are reloaded from the bridge">Restart bridge service</button>
      </div>
    </div>
  `;

  document.documentElement.appendChild(host);

  const el = {
    panel: root.querySelector(".panel"),
    bar: root.querySelector(".bar"),
    dot: root.querySelector(".dot"),
    vid: root.querySelector(".vid"),
    ver: root.querySelector(".ver"),
    fold: root.querySelector(".fold"),
    start: root.querySelector('[data-k="start"]'),
    end: root.querySelector('[data-k="end"]'),
    len: root.querySelector(".len"),
    countdown: root.querySelector(".countdown"),
    set: root.querySelector(".set"),
    send: root.querySelector(".send"),
    speed: root.querySelector(".speed"),
    rate: root.querySelector(".rate"),
    snap: root.querySelector(".snap"),
    snaptext: root.querySelector(".snaptext"),
    status: root.querySelector(".status"),
    gear: root.querySelector(".gear"),
    settings: root.querySelector(".panel.settings"),
    sfields: root.querySelector(".sfields"),
    smsg: root.querySelector(".smsg"),
    ssave: root.querySelector(".s-save"),
    scancel: root.querySelector(".s-cancel"),
    sclose: root.querySelector(".s-close"),
    srestart: root.querySelector(".s-restart")
  };

  // --- rendering ----------------------------------------------------------

  function setStatus(text, kind = "") {
    el.status.textContent = text || "";
    el.status.className = "status" + (kind ? " " + kind : "");
  }

  // The countdown row: visible while an in-point is waiting for its out-point,
  // red and blinking through the model's danger window. The flash phase comes
  // from the model, not from CSS, so what the tests pin is what blinks.
  function renderCountdown(st) {
    const live = Boolean(st && st.running && !st.expired);
    el.countdown.hidden = !live;
    if (!live) return;
    el.countdown.classList.toggle("danger", st.danger);
    el.countdown.classList.toggle("blink-off", st.danger && !st.flashOn);
    el.countdown.querySelector("output").textContent = `${st.remaining}s`;
  }

  // "63s at 1.50x → 42s" when the speed moves the stored length; "63s" at 1x.
  function deckLabel(duration, sp, stored) {
    if (sp.speed === 1 || stored === null) return `${duration}s`;
    return `${duration}s at ${sp.label} \u2192 ${Math.round(stored * 10) / 10}s`;
  }

  function render() {
    const sp = rate.state();
    const s = pair.state();
    // The 42 s ceiling is on what the deck stores: the picked range divided
    // by the speed it was heard at. A 63 s range at 1.5x is a 42 s sample and
    // fits, so the raw-range verdict must not veto it here.
    const stored = window.SpottySpeed.outputSeconds(s.duration, sp.speed);
    const overDeck = stored !== null && stored > MAX_LOOP + 1e-9;

    el.start.classList.toggle("empty", s.start === null);
    el.start.querySelector("output").textContent = s.start === null ? "\u2013\u2013" : fmt(s.start);
    el.end.classList.toggle("empty", s.end === null);
    el.end.querySelector("output").textContent = s.end === null ? "\u2013\u2013" : fmt(s.end);

    el.len.classList.toggle("over", overDeck);
    el.len.querySelector("output").textContent =
      stored === null ? "\u2013\u2013" : `${Math.round(stored * 10) / 10}s`;
    el.len.title = s.duration === null
      ? ""
      : `${s.duration}s of source at ${sp.label} \u2192 ${Math.round(stored * 10) / 10}s on the deck`;

    el.set.textContent = s.complete ? "NEW SELECTION" : (s.start === null ? "SET START" : "SET END");
    el.set.classList.toggle("armed", s.start !== null && !s.complete);

    el.speed.value = String(sp.speed);
    el.speed.disabled = sp.snap;
    el.rate.textContent = sp.label;
    el.snap.classList.toggle("on", sp.snap);
    el.snap.setAttribute("aria-pressed", sp.snap ? "true" : "false");
    el.snaptext.textContent = sp.snap ? "SNAPPED TO PLAYER" : "SNAP TO PLAYER";
    el.snap.title = sp.snap
      ? "speed follows the player; move the slider to take over"
      : (sp.player === null
          ? "follow the player's speed"
          : `follow the player's speed (now ${window.SpottySpeed.label(sp.player)})`);

    el.send.disabled = !s.complete || overDeck || ui.busy || !ui.ready;
    el.send.textContent = ui.busy ? "Working\u2026" : "Send to deck";

    el.vid.textContent = videoId() || "no video";
    el.panel.classList.toggle("collapsed", ui.collapsed);
    el.fold.innerHTML = ui.collapsed ? "+" : "&#8211;";
    el.bar.style.cursor = "";
    renderCountdown(countdown.running() ? countdown.tick(Date.now()) : null);
  }

  function showOffline(message) {
    ui.online = false;
    ui.ready = false;
    ui.transport = null;
    el.dot.className = "dot off";
    el.dot.title = message || "bridge offline";
    setStatus(message || "bridge not running", "err");
    render();
  }

  // The instrument is the thing that can be missing even when the bridge is
  // fine: no USB cable into a powered Spotykach and no card in a reader leaves
  // nothing that can take a sample, and the dot goes red instead of pretending
  // the send button will work.
  function showNotReady(detail) {
    ui.online = true;
    ui.ready = false;
    ui.transport = null;
    el.dot.className = "dot off";
    el.dot.title = detail || "no usb device and no card";
    setStatus(detail || "no instrument — connect usb or insert the card", "err");
    render();
  }

  function showOnline(transport) {
    ui.online = true;
    ui.ready = true;
    ui.transport = transport || null;
    el.dot.className = "dot on";
    el.dot.title = transport ? `ready over ${transport}` : "bridge running";
    render();
  }

  // --- time selection -----------------------------------------------------

  // The count follows the in-point: armed whenever a start is picked (moved
  // and restarted included), stopped the moment an out-point exists or
  // everything is cleared.
  function syncCountdown(action) {
    if (action === "start" || action === "restart" || action === "start-moved") {
      countdown.arm(Date.now());
    } else if (action === "end" || action === "end-too-long" || action === "reset") {
      countdown.disarm();
    }
  }

  function tickCountdown() {
    if (!alive()) { teardown(); return; }
    if (!countdown.running()) return;
    const st = countdown.tick(Date.now());
    if (st.expired) { autoEnd(); return; }
    renderCountdown(st);
  }

  // Zero reached with no out-point clicked: the player's position at the
  // elapse becomes the out-point, through the same state machine a manual
  // SET END press goes through. One difference: the countdown is wall-clock
  // while the deck cap is on stored seconds, so at speeds below 1x the
  // position can edge a hair past the cap (32s at 0.75x stores as 42.7s).
  // The clamp trims the source end so the stored length always fits.
  function autoEnd() {
    if (pair.state().complete) { countdown.disarm(); render(); return; }
    const raw = position();
    if (raw === null) {
      countdown.disarm();
      render();
      setStatus(`${MAX_LOOP}s elapsed \u2014 no player position, out-point not set`, "err");
      return;
    }
    const sp = rate.state();
    const at = window.SpottySelection.clampOutPoint(pair.state().start, raw, sp.speed);
    const result = pair.click(at, sp.speed);
    syncCountdown(result.action);
    render();
    const stored = window.SpottySpeed.outputSeconds(result.duration, sp.speed);
    if (result.action === "end") {
      const trimmed = at < raw
        ? ` \u2014 end trimmed from ${fmt(Math.floor(raw))} to fit the ${MAX_LOOP}s deck cap`
        : "";
      setStatus(`${MAX_LOOP}s elapsed \u2014 out-point set automatically: ` +
                `${fmt(result.start)} \u2192 ${fmt(result.end)} (${deckLabel(result.duration, sp, stored)})${trimmed} \u2014 ready`, "");
    } else if (result.action === "end-too-long") {
      setStatus(`${MAX_LOOP}s elapsed \u2014 the auto out-point makes ${deckLabel(result.duration, sp, stored)}, ` +
                `over the ${MAX_LOOP}s deck maximum. Click to start a new pair, hold to clear.`, "err");
    } else if (result.action === "start-moved") {
      setStatus(`${MAX_LOOP}s elapsed \u2014 the player never moved past the in-point; ` +
                "counting down again", "");
    }
  }

  let pressTimer = null;
  let longFired = false;

  function tap() {
    const at = position();
    if (at === null) {
      setStatus("no player found on this page", "err");
      return;
    }
    const result = pair.click(at, rate.state().speed);
    syncCountdown(result.action);
    render();

    const sp = rate.state();
    const stored = window.SpottySpeed.outputSeconds(result.duration, sp.speed);
    if (result.action === "rejected") {
      setStatus(result.reason, "err");
    } else if (result.action === "end-too-long") {
      setStatus(`${deckLabel(result.duration, sp, stored)} is over the ${MAX_LOOP}s deck maximum. ` +
                "Click to start a new pair, hold to clear.", "err");
    } else if (result.action === "start-moved") {
      setStatus(`in-point moved to ${fmt(result.start)} (was after the out-point)`, "");
    } else if (result.action === "end") {
      setStatus(`${fmt(result.start)} \u2192 ${fmt(result.end)} (${deckLabel(result.duration, sp, stored)}) — ready`, "");
    } else if (result.action === "restart") {
      setStatus(`new pair from ${fmt(result.start)}`, "");
    } else {
      setStatus(`in-point ${fmt(result.start)} — play on, then click again`, "");
    }
  }

  el.set.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    longFired = false;
    el.set.classList.add("holding");
    pressTimer = setTimeout(() => {
      longFired = true;
      const result = pair.reset();
      countdown.disarm();
      el.set.classList.remove("holding");
      render();
      setStatus("times cleared — click to set a new in-point", "");
      return result;
    }, LONG_PRESS_MS);
  });

  el.set.addEventListener("pointerup", (event) => {
    event.preventDefault();
    event.stopPropagation();
    clearTimeout(pressTimer);
    el.set.classList.remove("holding");
    if (!longFired) tap();
  });

  ["pointercancel", "pointerleave"].forEach((name) => {
    el.set.addEventListener(name, () => {
      clearTimeout(pressTimer);
      el.set.classList.remove("holding");
    });
  });

  // YouTube listens for space/k/j/l globally; keep typing and holding inside
  // the widget from moving the player.
  ["keydown", "keyup", "keypress"].forEach((name) => {
    host.addEventListener(name, (event) => event.stopPropagation());
  });

  // --- settings panel ------------------------------------------------------
  //
  // One source of truth: the bridge serves the schema (labels, controls,
  // ranges, hover help) next to the current values, and this panel renders
  // straight from it — a setting added in tools/settings.py shows up here
  // with no edit to this file. Values the widget itself consumes (countdown
  // timing, speed envelope, send preferences) are applied the moment a save
  // succeeds, and cached in chrome.storage.local so the page keeps behaving
  // per the last known settings while the bridge is down.

  const settingsState = {
    open: false, busy: false,
    schema: null, sections: [], values: null,
    inputs: new Map(), fields: {}
  };

  function applySettings(cfg) {
    if (!cfg) return;
    try {
      window.SpottySelection.setDangerSeconds(cfg.danger_seconds);
      window.SpottySelection.setFlashMs(cfg.flash_ms);
      window.SpottySpeed.setUiEnvelope({
        min: cfg.speed_min, max: cfg.speed_max,
        step: cfg.speed_step, defaultSpeed: cfg.speed_default
      });
      const env = window.SpottySpeed.uiEnvelope();
      el.speed.min = String(env.min);
      el.speed.max = String(env.max);
      el.speed.step = String(env.step);
      rate.set(rate.state().speed);   // re-clamp into the new envelope
      render();
    } catch { /* setters refuse bad values; the widget runs on built-ins */ }
  }

  function cacheSettings(cfg) {
    if (!alive() || !cfg) return;
    try {
      chrome.storage.local.set({ spottytubeSettings: cfg },
        () => { void chrome.runtime.lastError; });
    } catch { /* ignore */ }
  }

  async function syncSettings() {
    const reply = await ask({ type: "settings" });
    if (reply.ok) {
      settingsState.schema = reply.schema || null;
      settingsState.sections = reply.sections || [];
      settingsState.values = reply.settings || null;
      cacheSettings(reply.settings);
      applySettings(reply.settings);
    }
    return reply;
  }

  function setSMsg(text, kind) {
    el.smsg.textContent = text || "";
    el.smsg.className = "smsg" + (kind ? " " + kind : "");
  }

  // One settings row: the label carries the schema's help text as its hover
  // description, and the control follows the schema's `control` field.
  function fieldRow(field, value) {
    const node = document.createElement("div");
    node.className = "srow";
    const label = document.createElement("label");
    label.textContent = field.label;
    if (field.help) {
      label.title = field.help;   // on-hover description
      node.title = field.help;
    }
    let input;
    if (field.control === "select") {
      input = document.createElement("select");
      (field.choices || []).forEach((choice) => {
        const opt = document.createElement("option");
        opt.value = String(choice.value);
        opt.textContent = choice.label || String(choice.value);
        input.appendChild(opt);
      });
      input.value = value == null ? "" : String(value);
    } else if (field.control === "checkbox") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(value);
    } else if (field.control === "number") {
      input = document.createElement("input");
      input.type = "number";
      if (field.min != null) input.min = String(field.min);
      if (field.max != null) input.max = String(field.max);
      if (field.step != null) input.step = String(field.step);
      input.value = value == null ? "" : String(value);
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = value == null ? "" : String(value);
    }
    // Brave logs "<label> isn't associated with a form field" for a label with
    // no control; htmlFor/id also makes the label a click target for the input.
    input.id = `spottytube-set-${field.key}`;
    label.htmlFor = input.id;
    node.appendChild(label);
    node.appendChild(input);
    return { node, input };
  }

  function sectionOrder(schema) {
    if (settingsState.sections.length) return settingsState.sections;
    const seen = [];
    schema.forEach((field) => {
      if (!seen.some((entry) => entry[0] === field.section)) {
        seen.push([field.section, field.section]);
      }
    });
    return seen;
  }

  function renderFields() {
    el.sfields.innerHTML = "";
    settingsState.inputs.clear();
    settingsState.fields = {};
    const schema = settingsState.schema;
    const values = settingsState.values;
    if (!schema || !values) {
      el.ssave.disabled = true;
    } else {
      el.ssave.disabled = false;
      sectionOrder(schema).forEach((section) => {
        const fields = schema.filter((field) => field.section === section[0]);
        if (!fields.length) return;
        const head = document.createElement("div");
        head.className = "ssec";
        head.textContent = section[1];
        el.sfields.appendChild(head);
        fields.forEach((field) => {
          settingsState.fields[field.key] = field;
          const row = fieldRow(field, values[field.key]);
          settingsState.inputs.set(field.key, row.input);
          el.sfields.appendChild(row.node);
        });
      });
    }
    clampIntoView(el.settings);   // content height changed: keep the footer reachable
  }

  function collectValues() {
    const out = {};
    settingsState.inputs.forEach((input, key) => {
      const field = settingsState.fields[key];
      if (!field) return;
      if (field.control === "checkbox") out[key] = input.checked;
      else if (field.control === "number") {
        const text = String(input.value).trim();
        // An empty or non-numeric box is passed through as-is; the bridge's
        // validator rejects it with a field name, which beats guessing here.
        out[key] = text === "" ? text : Number(text);
      } else out[key] = input.value;
    });
    return out;
  }

  function positionSettings() {
    // Open where the user dragged the widget, not in the factory corner.
    if (el.panel.style.left) {
      el.settings.style.left = el.panel.style.left;
      el.settings.style.top = el.panel.style.top;
      el.settings.style.right = "auto";
      el.settings.style.bottom = "auto";
    }
  }

  function clampIntoView(panel) {
    // The drag anchor was clamped for the small widget, but the settings
    // panel is much taller: opened low on screen its footer (Save / Cancel /
    // Restart) lands under the viewport edge, and a clipped position:fixed
    // panel cannot be scrolled into reach. Pull it back fully on screen.
    if (panel.hidden) return;
    const margin = 8;
    const rect = panel.getBoundingClientRect();
    if (!rect.height) return;
    let top = rect.top;
    let left = rect.left;
    let moved = false;
    if (rect.bottom > window.innerHeight - margin) {
      top -= rect.bottom - (window.innerHeight - margin);
      moved = true;
    }
    if (top < margin) { top = margin; moved = true; }
    if (rect.right > window.innerWidth - margin) {
      left -= rect.right - (window.innerWidth - margin);
      moved = true;
    }
    if (left < margin) { left = margin; moved = true; }
    if (moved) {
      panel.style.top = `${Math.round(top)}px`;
      panel.style.left = `${Math.round(left)}px`;
      panel.style.right = "auto";
      panel.style.bottom = "auto";
    }
  }

  function openSettings() {
    settingsState.open = true;
    positionSettings();
    el.settings.hidden = false;
    el.panel.style.display = "none";
    renderFields();
    if (!settingsState.schema) setSMsg("loading settings from the bridge\u2026");
    syncSettings().then((reply) => {
      if (!settingsState.open) return;
      if (reply.ok) {
        renderFields();
        setSMsg("");
        return;
      }
      renderFields();
      setSMsg(settingsState.schema
        ? "bridge unreachable \u2014 showing the last known values; saving needs the bridge"
        : (reply.error || "bridge unreachable \u2014 settings unavailable"), "err");
    });
  }

  function closeSettings() {
    settingsState.open = false;
    el.settings.hidden = true;
    el.panel.style.display = "";
  }

  async function saveSettings() {
    if (settingsState.busy || !settingsState.schema) return;
    settingsState.busy = true;
    el.ssave.disabled = true;
    el.scancel.disabled = true;
    setSMsg("saving\u2026");
    const reply = await ask({ type: "settingsSave", payload: collectValues() });
    settingsState.busy = false;
    el.scancel.disabled = false;
    if (!reply.ok) {
      el.ssave.disabled = false;
      setSMsg(reply.error || "bridge rejected the settings", "err");
      return;
    }
    el.ssave.disabled = false;
    settingsState.values = reply.settings || settingsState.values;
    cacheSettings(reply.settings);
    applySettings(reply.settings);
    let msg = "settings saved \u2014 applied, no restart";
    const card = reply.card_config;
    if (card && card.error) msg += `\nconfig.txt: ${card.error}`;
    else if (card && card.warning) msg += `\n${card.warning}`;
    else if (card && card.written) msg += "\nconfig.txt written to the card";
    setSMsg(msg, card && (card.error || card.warning) ? "warn" : "ok");
    if (!ui.busy) setStatus("settings saved", "");
  }

  // The stale-bridge escape hatch: the service exits non-zero and systemd's
  // Restart=on-failure brings it back running whatever is on disk now. The
  // panel then re-syncs, which both proves the restart and shows the new
  // build's version — unsaved edits are dropped in favour of fresh values.
  async function restartBridge() {
    if (settingsState.busy) return;
    settingsState.busy = true;
    el.srestart.disabled = true;
    el.ssave.disabled = true;
    setSMsg("restarting the bridge service\u2026");
    const reply = await ask({ type: "restart" });
    if (!reply.ok) {
      settingsState.busy = false;
      el.srestart.disabled = false;
      el.ssave.disabled = !(settingsState.schema && settingsState.values);
      setSMsg(reply.error || "restart failed", "err");
      return;
    }
    // The unit is down for RestartSec (2s) plus boot; probe until it answers.
    const deadline = Date.now() + 15000;
    let back = null;
    while (Date.now() < deadline) {
      await sleep(700);
      const probe = await ask({ type: "health" });
      if (probe.ok) { back = probe; break; }
    }
    if (!alive()) return;
    settingsState.busy = false;
    el.srestart.disabled = false;
    if (!back) {
      el.ssave.disabled = !(settingsState.schema && settingsState.values);
      setSMsg("bridge did not come back within 15s \u2014 check: "
        + "systemctl --user status spottytube-bridge", "err");
      return;
    }
    await syncSettings();   // re-pull schema + values from the new build
    if (!alive() || !settingsState.open) return;
    renderFields();
    setSMsg(`bridge restarted \u2014 now v${back.version}`, "ok");
  }

  el.gear.addEventListener("click", (event) => {
    event.stopPropagation();
    if (settingsState.open) closeSettings();
    else openSettings();
  });
  el.sclose.addEventListener("click", (event) => {
    event.stopPropagation();
    closeSettings();
  });
  el.ssave.addEventListener("click", (event) => {
    event.stopPropagation();
    saveSettings();
  });
  el.scancel.addEventListener("click", (event) => {
    event.stopPropagation();
    closeSettings();   // discard: nothing is applied until Save succeeds
  });
  el.srestart.addEventListener("click", (event) => {
    event.stopPropagation();
    restartBridge();
  });

  // --- speed --------------------------------------------------------------

  // The player is the only thing that knows the rate; everything here just
  // reads it. No page-specific API: any <video> element reports playbackRate.
  function playerRate() {
    const el = video();
    const value = el && el.playbackRate;
    return typeof value === "number" && isFinite(value) && value > 0 ? value : null;
  }

  el.speed.addEventListener("input", (event) => {
    event.stopPropagation();
    if (!rate.set(el.speed.value)) return;   // snapped: the player is in charge
    render();
    persist();
  });

  el.snap.addEventListener("click", (event) => {
    event.stopPropagation();
    const on = rate.toggleSnap(playerRate());
    render();
    persist();
    setStatus(on ? `speed follows the player at ${rate.state().label}` : "", "");
  });

  // No event fires on a speed change everywhere, so it is polled. Cheap: one
  // property read.
  let lastRate = null;
  function watchRate() {
    const now = playerRate();
    if (now === null || now === lastRate) return;
    lastRate = now;
    const followed = rate.follow(now);
    render();
    if (followed) {
      persist();
      setStatus(`speed follows the player at ${rate.state().label}`, "");
    }
  }

  // --- send ---------------------------------------------------------------

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function ask(message) {
    return new Promise((resolve) => {
      if (!alive()) {
        resolve({ ok: false, offline: true, error: "the extension was reloaded — reload this page" });
        return;
      }
      chrome.runtime.sendMessage(message, (reply) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, offline: true, error: chrome.runtime.lastError.message });
        } else {
          resolve(reply || { ok: false, error: "no reply from extension worker" });
        }
      });
    });
  }

  async function pollJob(jobId) {
    const started = Date.now();
    while (Date.now() - started < POLL_LIMIT_MS) {
      await sleep(POLL_MS);
      const job = await ask({ type: "job", job_id: jobId });
      if (job.offline) {
        showOffline(job.error);
        return false;
      }
      if (job.status === "queued" || job.status === "running") {
        setStatus("working — downloading and cutting\u2026", "busy");
        continue;
      }
      if (job.status === "done" && job.result) {
        const r = job.result;
        const at = r.speed && r.speed !== 1 ? ` @ ${Number(r.speed).toFixed(2)}x` : "";
        // Show where the sample lives ON THE UNIT — its tape directory and
        // slot — not the generated segment file on this machine.
        setStatus(`saved \u2192 ${r.tape}/${r.slot}.WAV \u00b7 ` +
                  `${Math.round(r.end - r.start)}s${at}\n` +
                  `${r.mode === "append" ? "appended to the first free slot" : "bumped into slot 1"}` +
                  ` \u00b7 via ${r.transport}`, "ok");
        return true;
      }
      const detail = (job.stderr || job.stdout || "sampling tool failed").trim();
      setStatus(detail.split("\n").slice(-4).join("\n"), "err");
      return false;
    }
    setStatus("gave up waiting for the bridge — check `journalctl --user -u spottytube-bridge`", "err");
    return false;
  }

  el.send.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (ui.busy) return;
    const s = pair.state();
    const sp = rate.state();
    // Re-check the stored length live: the slider may have moved since the
    // out-point was picked, and the deck limit is on stored seconds.
    const stored = window.SpottySpeed.outputSeconds(s.duration, sp.speed);
    if (!s.complete || (stored !== null && stored > MAX_LOOP + 1e-9)) return;

    ui.busy = true;
    render();
    setStatus("sending to bridge\u2026", "busy");

    const reply = await ask({
      type: "sample",
      payload: {
        url: pageUrl(),
        start: fmt(s.start),
        end: fmt(s.end),
        speed: rate.state().speed
      }
    });

    if (!reply.ok) {
      ui.busy = false;
      render();
      if (reply.offline) showOffline(reply.error);
      else setStatus(reply.error || "bridge rejected the request", "err");
      return;
    }

    showOnline();
    const done = await pollJob(reply.job_id);
    ui.busy = false;
    render();
    if (done) setStatus(el.status.textContent, "ok");
  });

  // --- drag + persist -----------------------------------------------------

  function persist() {
    if (!alive()) return;
    try {
      const sp = rate.state();
      // Callback form on purpose: the promise form rejects with "Extension
      // context invalidated" inside a page that outlived the extension, and a
      // rejection nobody handles surfaces as an uncaught error in the console.
      chrome.storage.local.set({ spottytube: {
        collapsed: ui.collapsed, left: ui.left, top: ui.top,
        speed: sp.speed, snap: sp.snap, player: sp.player
      } }, () => { void chrome.runtime.lastError; });
    } catch { /* ignore */ }
  }

  // Reloading or updating the extension detaches every copy that is already
  // running in a page, and each chrome.* call then throws "Extension context
  // invalidated". Rather than poll a dead API forever, the widget checks and
  // takes itself out of the page; the next page load injects a fresh copy.
  function alive() {
    try { return Boolean(chrome.runtime && chrome.runtime.id); }
    catch { return false; }
  }

  function teardown() {
    clearInterval(rateTimer);
    clearInterval(statusTimer);
    clearInterval(hrefTimer);
    clearInterval(cdTimer);
    document.removeEventListener("yt-navigate-finish", onNavigate, true);
    document.removeEventListener("yt-page-data-updated", onNavigate, true);
    root.remove();
  }

  let rateTimer = null;
  let statusTimer = null;
  let hrefTimer = null;
  let cdTimer = null;

  function place(left, top) {
    const width = 268;
    const clampedLeft = Math.max(4, Math.min(left, window.innerWidth - width - 4));
    const clampedTop = Math.max(4, Math.min(top, window.innerHeight - 60));
    ui.left = clampedLeft;
    ui.top = clampedTop;
    el.panel.style.left = `${clampedLeft}px`;
    el.panel.style.top = `${clampedTop}px`;
    el.panel.style.right = "auto";
    el.panel.style.bottom = "auto";
  }

  let drag = null;
  el.bar.addEventListener("pointerdown", (event) => {
    if (event.target === el.fold || event.target === el.gear) return;
    const box = el.panel.getBoundingClientRect();
    drag = { dx: event.clientX - box.left, dy: event.clientY - box.top };
    el.bar.classList.add("dragging");
    el.bar.setPointerCapture(event.pointerId);
  });
  el.bar.addEventListener("pointermove", (event) => {
    if (!drag) return;
    place(event.clientX - drag.dx, event.clientY - drag.dy);
  });
  el.bar.addEventListener("pointerup", () => {
    if (!drag) return;
    drag = null;
    el.bar.classList.remove("dragging");
    persist();
  });

  el.fold.addEventListener("click", (event) => {
    event.stopPropagation();
    ui.collapsed = !ui.collapsed;
    render();
    persist();
  });

  // --- lifecycle ----------------------------------------------------------

  function onNavigate() {
    pair.reset();
    countdown.disarm();
    render();
    setStatus("");
    refreshStatus();
  }

  document.addEventListener("yt-navigate-finish", onNavigate, true);
  document.addEventListener("yt-page-data-updated", onNavigate, true);

  let lastHref = location.href;
  hrefTimer = setInterval(() => {
    if (!alive()) { teardown(); return; }
    if (location.href !== lastHref) {
      lastHref = location.href;
      onNavigate();
    }
  }, 1500);

  // /status answers the one question the widget needs: can anything take a
  // sample right now, and over which transport. USB is preferred by the bridge;
  // the card is only reported when USB is not there.
  let checking = false;

  async function refreshStatus() {
    if (checking) return;
    checking = true;
    try {
      const status = await ask({ type: "status" });
      if (status.offline) {
        showOffline(status.error);
        return;
      }
      if (!status.ok) {
        showNotReady(status.error || "bridge could not check the instrument");
        return;
      }
      if (!status.ready) {
        showNotReady(status.error || "no usb device and no card — connect usb or insert the card");
        return;
      }
      showOnline(status.transport);
      if (!ui.busy && el.status.textContent.startsWith("no instrument")) setStatus("");
    } finally {
      checking = false;
    }
  }

  // Shown in the bar so a browser still running an older copy is obvious at a
  // glance after an update; reload the extension to pick up new code.
  try {
    el.ver.textContent = "v" + chrome.runtime.getManifest().version;
  } catch {
    el.ver.remove();
  }

  // Restore saved UI state and cached settings, then check the bridge once.
  try {
    chrome.storage.local.get(["spottytube", "spottytubeSettings"], (saved) => {
      const s = saved && saved.spottytube;
      if (s) {
        ui.collapsed = Boolean(s.collapsed);
        rate.restore(s);
        if (typeof s.left === "number" && typeof s.top === "number") place(s.left, s.top);
      }
      const cached = saved && saved.spottytubeSettings;
      if (cached) applySettings(cached);
      if (!s || s.speed == null) {
        // No remembered speed: open on the configured slider default.
        rate.set(window.SpottySpeed.uiEnvelope().defaultSpeed);
      }
      render();
      syncSettings();   // silently pick up changes made elsewhere
    });
  } catch {
    render();
  }

  render();
  watchRate();
  rateTimer = setInterval(() => { if (alive()) watchRate(); else teardown(); }, RATE_MS);
  // The countdown needs a tick finer than its 500 ms flash phase; 200 ms keeps
  // both the seconds readout and the blink honest without burning the page.
  cdTimer = setInterval(tickCountdown, COUNTDOWN_MS);
  refreshStatus();
  // The Spotykach drops out of transfer mode on its own, so re-check even when
  // the page does nothing; the dot has to go red on its own too.
  statusTimer = setInterval(() => {
    if (alive()) refreshStatus();
    else teardown();
  }, 20000);
})();
