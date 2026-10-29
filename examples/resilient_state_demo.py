"""
resilient_state_demo.py
-----------------------
The crash-safe progress pattern used by the pipeline, in miniature.

What it demonstrates
  1. ATOMIC STATE SAVES   write to a temp file, flush to disk, then swap it in,
                          keeping a .bak copy of the last good version.
  2. DURABLE RESULTS      every finished item is appended and fsync'd at once,
                          so a crash can never lose completed work.
  3. JOB TAGS             the state file records which job it belongs to, so a
                          leftover file from a different job is never reused.
  4. RESUME               run it again after a crash and it carries on from the
                          exact item where it stopped, without duplicates.

Try it (no dependencies, Python 3.8+):

    python resilient_state_demo.py --reset --crash-after 5   # dies after 5 items
    python resilient_state_demo.py                           # resumes with item 6
    python resilient_state_demo.py                           # nothing left to do
"""

import argparse
import json
import os
import shutil
import sys
import time

STATE_FILE = "demo_state.json"
RESULTS_FILE = "demo_results.txt"
JOB_TAG = "demo-job|sample-city"
ITEMS = [f"Sample Company {n:02d}" for n in range(1, 13)]


# ------------------------------------------------------------ safe file I/O --

def save_json(path: str, data: dict) -> None:
    """Atomic save: temp file -> flush -> fsync -> swap in. Keeps a .bak."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")  # last known-good copy
    os.replace(tmp, path)  # atomic on Windows, macOS and Linux


def load_json(path: str):
    """Return the saved dict, or None if there is no file yet.
    A damaged file stops the run instead of silently starting over."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        sys.exit(f"{path} is damaged. Rename {path}.bak to {path} and run again.")


def append_result(path: str, line: str) -> None:
    """Append one finished result and force it to disk immediately."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def already_written() -> set:
    if not os.path.exists(RESULTS_FILE):
        return set()
    with open(RESULTS_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


# --------------------------------------------------------------------- main --

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete old demo files first")
    parser.add_argument("--crash-after", type=int, default=0,
                        help="simulate a hard crash after this many items")
    args = parser.parse_args()

    if args.reset:
        for name in (STATE_FILE, STATE_FILE + ".bak", RESULTS_FILE):
            if os.path.exists(name):
                os.remove(name)

    state = load_json(STATE_FILE)
    if state and state.get("job") == JOB_TAG and state.get("pending"):
        print(f"Resuming: {state['done_count']} done, {len(state['pending'])} left.")
    else:
        if state and state.get("job") != JOB_TAG:
            print("State file belongs to a different job, starting fresh.")
        state = {"job": JOB_TAG, "done_count": 0, "pending": list(ITEMS)}
        save_json(STATE_FILE, state)
        print(f"Starting new job with {len(ITEMS)} items.")

    written = already_written()
    processed_this_run = 0

    while state["pending"]:
        item = state["pending"][0]
        number = state["done_count"] + 1
        print(f"[{number}/{state['done_count'] + len(state['pending'])}] {item}")

        time.sleep(0.15)  # stand-in for real work (scrolling, scraping, ...)

        # Result first, progress second: a crash between the two can only cause
        # a repeat of one item (skipped below), never a lost one.
        if item not in written:
            append_result(RESULTS_FILE, item)
            written.add(item)

        state["pending"].pop(0)
        state["done_count"] = number
        save_json(STATE_FILE, state)
        processed_this_run += 1

        if args.crash_after and processed_this_run == args.crash_after and state["pending"]:
            print("SIMULATED CRASH (hard exit, no cleanup). Run the script again to resume.")
            os._exit(1)

    print(f"Finished. {state['done_count']} items in {RESULTS_FILE}.")


if __name__ == "__main__":
    main()
