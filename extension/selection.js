// Start/end pair state machine, kept out of the DOM so it can be tested.
//
// One button drives it: first click marks the in-point, second marks the
// out-point, a third starts a fresh pair. A long press resets everything.
(function (global) {
  "use strict";

  var MAX_LOOP = 42; // seconds; mirrors sample_tool.MAX_LOOP_SECONDS
  var DANGER_SECONDS = 5;  // red-flash window at the end of the count
  var FLASH_MS = 500;      // the flash flips phase every half second

  // The cap is on the range you pick and it does not move with the speed. A
  // slowed range can still overflow the 42 s the deck stores, but that check
  // belongs to whoever knows the speed, not here.
  function createPair() {
    var start = null;
    var end = null;

    function snapshot() {
      return {
        start: start,
        end: end,
        duration: start !== null && end !== null ? end - start : null,
        complete: start !== null && end !== null,
        overLimit: start !== null && end !== null && end - start > MAX_LOOP
      };
    }

    return {
      // seconds = current playback position. Returns {action, ...state}.
      click: function (seconds) {
        if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) {
          return Object.assign({ action: "rejected", reason: "no playback position" }, snapshot());
        }
        var at = Math.floor(seconds);

        if (start === null) {
          start = at;
          end = null;
          return Object.assign({ action: "start" }, snapshot());
        }

        if (end === null) {
          if (at > start) {
            end = at;
            var state = snapshot();
            if (state.overLimit) {
              return Object.assign({ action: "end-too-long" }, state);
            }
            return Object.assign({ action: "end" }, state);
          }
          // Clicked behind the in-point: move the in-point instead of
          // producing an impossible range.
          start = at;
          end = null;
          return Object.assign({ action: "start-moved" }, snapshot());
        }

        // Pair already complete: begin a new selection.
        start = at;
        end = null;
        return Object.assign({ action: "restart" }, snapshot());
      },

      reset: function () {
        start = null;
        end = null;
        return Object.assign({ action: "reset" }, snapshot());
      },

      state: snapshot
    };
  }

  // Wall-clock countdown against the in-point. Pure on purpose: the widget
  // arms it when a start is picked and ticks it; the first tick that reports
  // `expired` is when the out-point gets chosen by itself. Nothing here knows
  // the DOM or the player.
  function createCountdown(totalSeconds) {
    var total = typeof totalSeconds === "number" && isFinite(totalSeconds) && totalSeconds > 0
      ? totalSeconds : MAX_LOOP;
    var deadline = null; // epoch ms; null while idle

    function tick(nowMs) {
      if (deadline === null || typeof nowMs !== "number" || !isFinite(nowMs)) {
        return { running: false, remaining: null, expired: false, danger: false, flashOn: false };
      }
      var remainingMs = deadline - nowMs;
      if (remainingMs <= 0) {
        return { running: true, remaining: 0, expired: true, danger: false, flashOn: false };
      }
      var remaining = Math.ceil(remainingMs / 1000);
      var danger = remaining <= DANGER_SECONDS;
      // The phase is anchored to the deadline so the blink is regular:
      // visible for 500 ms, gone for 500 ms.
      var flashOn = danger && Math.floor(remainingMs / FLASH_MS) % 2 === 0;
      return { running: true, remaining: remaining, expired: false, danger: danger, flashOn: flashOn };
    }

    return {
      arm: function (nowMs) { deadline = nowMs + total * 1000; },
      disarm: function () { deadline = null; },
      running: function () { return deadline !== null; },
      tick: tick
    };
  }

  var api = {
    createPair: createPair,
    createCountdown: createCountdown,
    MAX_LOOP: MAX_LOOP,
    DANGER_SECONDS: DANGER_SECONDS,
    FLASH_MS: FLASH_MS
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.SpottySelection = api;
})(typeof window !== "undefined" ? window : globalThis);
