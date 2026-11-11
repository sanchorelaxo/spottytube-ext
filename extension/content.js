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

  const ui = { mode: "bump", collapsed: false, left: null, top: null,
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
      .moderow { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
      .mode {
        all: unset; text-align: center; cursor: pointer; padding: 6px 0; border-radius: 7px;
        background: #0f1115; border: 1px solid #262a33; color: #98a0b0; font-size: 12px;
      }
      .mode.on { background: #24303f; border-color: #3c5070; color: #dfe8f5; font-weight: 600; }
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
      [hidden] { display: none !important; }
    </style>
    <div class="panel">
      <div class="bar">
        <span class="dot" title="bridge status"></span>
        <span class="name">SpottyTube</span>
        <span class="ver" title="extension version"></span>
        <span class="vid"></span>
        <button class="fold" title="collapse">&#8211;</button>
      </div>
      <div class="body">
        <div class="times">
          <div class="cell empty" data-k="start"><label>START</label><output>&#8211;&#8211;</output></div>
          <div class="cell empty" data-k="end"><label>END</label><output>&#8211;&#8211;</output></div>
          <div class="cell len"><label>LEN</label><output>&#8211;&#8211;</output></div>
        </div>
        <div class="countdown" hidden><span class="cdlabel">AUTO-END IN</span><output>42s</output></div>
        <button class="set">SET START</button>
        <div class="hint">click: start, then end &middot; hold: reset</div>
        <div class="moderow">
          <button class="mode on" data-mode="bump"
                  title="New sample takes slot 1. Everything behind it bumps one slot down B G P R T Y, each file renamed to its new slot. Only when Y/6 is full does the last file fall off &#8212; saved to the card root with a timestamp.">Bump &#183; slot 1</button>
          <button class="mode" data-mode="append"
                  title="New sample takes the first free slot on the chain, in B G P R T Y order. Nothing moves.">Append &#183; next free</button>
        </div>
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
    modes: Array.from(root.querySelectorAll(".mode"))
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

  function render() {
    const sp = rate.state();
    const s = pair.state();
    // What the deck will actually hold: a slowed-down range is longer than the
    // range you picked, and the 42 s ceiling applies to what gets stored.
    const stored = window.SpottySpeed.outputSeconds(s.duration, sp.speed);
    const overDeck = Boolean(s.overLimit) ||
      (stored !== null && stored > MAX_LOOP + 1e-9);

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

    el.modes.forEach((b) => b.classList.toggle("on", b.dataset.mode === ui.mode));
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
  // SET END press goes through, so over-limit and paused-player cases behave
  // exactly as they do by hand.
  function autoEnd() {
    if (pair.state().complete) { countdown.disarm(); render(); return; }
    const at = position();
    if (at === null) {
      countdown.disarm();
      render();
      setStatus(`${MAX_LOOP}s elapsed \u2014 no player position, out-point not set`, "err");
      return;
    }
    const result = pair.click(at);
    syncCountdown(result.action);
    render();
    if (result.action === "end") {
      setStatus(`${MAX_LOOP}s elapsed \u2014 out-point set automatically: ` +
                `${fmt(result.start)} \u2192 ${fmt(result.end)} (${result.duration}s) \u2014 ready`, "");
    } else if (result.action === "end-too-long") {
      setStatus(`${MAX_LOOP}s elapsed \u2014 the auto out-point makes ${result.duration}s, ` +
                `over the ${MAX_LOOP}s maximum. Click to start a new pair, hold to clear.`, "err");
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
    const result = pair.click(at);
    syncCountdown(result.action);
    render();

    if (result.action === "rejected") {
      setStatus(result.reason, "err");
    } else if (result.action === "end-too-long") {
      setStatus(`${result.duration}s is over the ${MAX_LOOP}s maximum slice. ` +
                "Click to start a new pair, hold to clear.", "err");
    } else if (result.action === "start-moved") {
      setStatus(`in-point moved to ${fmt(result.start)} (was after the out-point)`, "");
    } else if (result.action === "end") {
      const sp = rate.state();
      const at = sp.speed === 1 ? "" : ` at ${sp.label}`;
      setStatus(`${fmt(result.start)} \u2192 ${fmt(result.end)} (${result.duration}s${at}) — ready`, "");
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

  // --- save-mode choice -----------------------------------------------------

  el.modes.forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      ui.mode = button.dataset.mode;
      render();
      persist();
    });
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
    if (!s.complete || s.overLimit) return;

    ui.busy = true;
    render();
    setStatus("sending to bridge\u2026", "busy");

    const reply = await ask({
      type: "sample",
      payload: {
        url: pageUrl(),
        start: fmt(s.start),
        end: fmt(s.end),
        mode: ui.mode,
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
        mode: ui.mode, collapsed: ui.collapsed, left: ui.left, top: ui.top,
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
    if (event.target === el.fold) return;
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

  // Restore saved UI state, then check the bridge once.
  try {
    chrome.storage.local.get("spottytube", (saved) => {
      const s = saved && saved.spottytube;
      if (s) {
        ui.mode = s.mode === "append" ? "append" : "bump";
        ui.collapsed = Boolean(s.collapsed);
        rate.restore(s);
        if (typeof s.left === "number" && typeof s.top === "number") place(s.left, s.top);
      }
      render();
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
