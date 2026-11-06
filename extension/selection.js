// Start/end pair state machine, kept out of the DOM so it can be tested.
//
// One button drives it: first click marks the in-point, second marks the
// out-point, a third starts a fresh pair. A long press resets everything.
(function (global) {
  "use strict";

  var MAX_LOOP = 42; // seconds; mirrors sample_tool.MAX_LOOP_SECONDS

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

  var api = { createPair: createPair, MAX_LOOP: MAX_LOOP };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.SpottySelection = api;
})(typeof window !== "undefined" ? window : globalThis);
