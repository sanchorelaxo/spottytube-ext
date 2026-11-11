#!/usr/bin/env python3
"""Stand-in for yt_sample.py so the bridge can be tested without a card.

Behaviour is driven by environment variables:
    STUB_ARGS_FILE   write the received argv (one per line) to this path
    STUB_SLEEP       seconds to sleep before answering
    STUB_FAIL        "1" -> exit 1 with a yt-dlp style error
    STUB_CARD_OK     "0" -> --check-card exits 1
"""

import json
import os
import sys
import time


def card_line():
    return "card: /tmp/fake-card\n  SK/ folder: yes\n  writable:   yes\nstate: ready"


def main():
    args = sys.argv[1:]

    args_file = os.environ.get("STUB_ARGS_FILE")
    if args_file:
        with open(args_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(args))

    if "--check-card" in args:
        if os.environ.get("STUB_CARD_OK", "1") == "0":
            print("error: no SD card found. Insert the Spotykach card and re-run", file=sys.stderr)
            return 1
        print(card_line())
        return 0

    time.sleep(float(os.environ.get("STUB_SLEEP", "0")))

    if os.environ.get("STUB_FAIL") == "1":
        print("error: yt-dlp failed: HTTP Error 403: Forbidden", file=sys.stderr)
        return 1

    def value(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default

    deck = (value("--deck") or "a").upper()
    mode = value("--mode") or "bump"
    print("card: /tmp/fake-card  (ready)")
    print(json.dumps({
        "source": value("--url"),
        "video_id": "abcdefghijk",
        "start": 60.0,
        "end": 72.0,
        "deck": deck,
        "mode": mode,
        "tape": "B",
        "slot": 1 if deck == "A" else 2,
        "segment": "/tmp/fake-card/segments/clip.WAV",
        "card_path": "/tmp/fake-card/SK/B/1.WAV" if deck == "A" else "/tmp/fake-card/SK/B/2.WAV",
        "installed": True,
        "backup": None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
