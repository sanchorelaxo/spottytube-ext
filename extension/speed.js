// Playback-speed model for the widget. Pure: no DOM, no chrome APIs.
//
// The slider value and the player's rate are kept apart on purpose. "Snap"
// only means "keep the slider on whatever the player is doing"; turning it off
// leaves the slider where the player left it.
(function () {
  "use strict";

  const MIN_SPEED = 0.25;
  const MAX_SPEED = 2;
  const STEP = 0.05;
  const DEFAULT_SPEED = 1;

  // The absolute envelope the backend accepts (mirrors bridge/tool
  // MIN_SPEED/MAX_SPEED). The settings panel can only narrow the slider
  // inside this, never widen it.
  const SPEED_ENV = { min: MIN_SPEED, max: MAX_SPEED };
  const DEFAULT_UI = {
    min: MIN_SPEED, max: MAX_SPEED, step: STEP, defaultSpeed: DEFAULT_SPEED,
  };
  const ui = { ...DEFAULT_UI };

  function clampAbs(value) {
    return Math.min(SPEED_ENV.max, Math.max(SPEED_ENV.min, value));
  }

  function clampUi(value) {
    return Math.min(ui.max, Math.max(ui.min, value));
  }

  // Narrow the slider's working range (D3). Invalid or out-of-envelope
  // values are ignored per field, so a bad settings payload can never push
  // the widget past what the backend accepts.
  function setUiEnvelope(next) {
    const seen = next || {};
    const num = (value) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const min = num(seen.min);
    const max = num(seen.max);
    const step = num(seen.step);
    const dflt = num(seen.defaultSpeed != null ? seen.defaultSpeed : seen["default"]);
    if (min != null && min >= SPEED_ENV.min && min <= SPEED_ENV.max) ui.min = min;
    if (max != null && max >= SPEED_ENV.min && max <= SPEED_ENV.max) ui.max = max;
    if (ui.min > ui.max) { ui.min = DEFAULT_UI.min; ui.max = DEFAULT_UI.max; }
    if (step != null && step > 0 && step <= 1) ui.step = step;
    if (dflt != null) ui.defaultSpeed = clampUi(clampAbs(dflt));
    return uiEnvelope();
  }

  function uiEnvelope() {
    return { ...ui };
  }

  // Anything that is not a usable rate becomes 1x. Clamping to the range the
  // player offers means the widget can never ask ffmpeg for something absurd.
  function normalize(value) {
    let speed;
    if (typeof value === "number") speed = value;
    else if (typeof value === "string" && value.trim() !== "") speed = Number(value);
    else speed = NaN;
    if (!Number.isFinite(speed)) return DEFAULT_SPEED;
    speed = Math.round(speed * 100) / 100;
    return Math.min(MAX_SPEED, Math.max(MIN_SPEED, speed));
  }

  function label(speed) {
    return normalize(speed).toFixed(2) + "x";
  }

  // What the deck has to hold: slowing a range down makes it longer.
  function outputSeconds(seconds, speed) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return null;
    return seconds / normalize(speed);
  }

  function fits(seconds, speed, max) {
    const ceiling = max == null ? 42 : max;
    const output = outputSeconds(seconds, speed);
    return output != null && output <= ceiling + 1e-9;
  }

  function create(saved) {
    const initial = saved || {};
    const state = {
      speed: clampUi(normalize(initial.speed)),
      snap: initial.snap === true,
      player: null,
    };
    if (initial.player != null) {
      const seen = Number(initial.player);
      if (Number.isFinite(seen) && seen > 0) state.player = clampUi(normalize(seen));
    }

    // The slider. Ignored while snapped: the player is in charge then, and
    // fighting it would make the knob jump back on every tick.
    function set(value) {
      if (state.snap) return false;
      state.speed = clampUi(normalize(value));
      return true;
    }

    // Called every time the page reports a rate, snapped or not, so that
    // turning snap on later adopts a rate even if the player is not moving.
    function follow(rate) {
      const offered = Number(rate);
      if (!Number.isFinite(offered) || offered <= 0) return false;
      state.player = clampUi(normalize(offered));
      if (!state.snap) return false;
      state.speed = state.player;
      return true;
    }

    function toggleSnap(rate) {
      if (state.snap) {
        state.snap = false;
        return false;
      }
      state.snap = true;
      const offered = rate == null ? state.player : Number(rate);
      if (offered != null && Number.isFinite(offered) && offered > 0) {
        state.speed = clampUi(normalize(offered));
      }
      return true;
    }

    // Saved state arrives after the widget is already alive, so it is applied
    // on top of whatever is there rather than replacing the object.
    function restore(saved) {
      const from = saved || {};
      state.speed = clampUi(normalize(from.speed));
      state.snap = from.snap === true;
      state.player = null;
      if (from.player != null) {
        const seen = Number(from.player);
        if (Number.isFinite(seen) && seen > 0) state.player = clampUi(normalize(seen));
      }
      return state_();
    }

    function state_() {
      return {
        speed: state.speed,
        snap: state.snap,
        player: state.player,
        label: label(state.speed),
      };
    }

    return { set, follow, toggleSnap, restore, state: state_ };
  }

  const api = {
    MIN_SPEED, MAX_SPEED, STEP, DEFAULT_SPEED,
    SPEED_ENV, DEFAULT_UI, setUiEnvelope, uiEnvelope,
    normalize, label, outputSeconds, fits, create,
  };
  if (typeof window !== "undefined") window.SpottySpeed = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
