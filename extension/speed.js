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
      speed: normalize(initial.speed),
      snap: initial.snap === true,
      player: null,
    };
    if (initial.player != null) {
      const seen = Number(initial.player);
      if (Number.isFinite(seen) && seen > 0) state.player = normalize(seen);
    }

    // The slider. Ignored while snapped: the player is in charge then, and
    // fighting it would make the knob jump back on every tick.
    function set(value) {
      if (state.snap) return false;
      state.speed = normalize(value);
      return true;
    }

    // Called every time the page reports a rate, snapped or not, so that
    // turning snap on later adopts a rate even if the player is not moving.
    function follow(rate) {
      const offered = Number(rate);
      if (!Number.isFinite(offered) || offered <= 0) return false;
      state.player = normalize(offered);
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
        state.speed = normalize(offered);
      }
      return true;
    }

    // Saved state arrives after the widget is already alive, so it is applied
    // on top of whatever is there rather than replacing the object.
    function restore(saved) {
      const from = saved || {};
      state.speed = normalize(from.speed);
      state.snap = from.snap === true;
      state.player = null;
      if (from.player != null) {
        const seen = Number(from.player);
        if (Number.isFinite(seen) && seen > 0) state.player = normalize(seen);
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
    normalize, label, outputSeconds, fits, create,
  };
  if (typeof window !== "undefined") window.SpottySpeed = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
