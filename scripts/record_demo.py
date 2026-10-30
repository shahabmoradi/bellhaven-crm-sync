"""Drive the demo scenes in the recorded Chrome via CDP.

Usage:
  .venv/bin/python scripts/record_demo.py --dry-run   # show cards only, screenshots, NO clicks
  .venv/bin/python scripts/record_demo.py             # full take: approve / reject / approve
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import open_tab, card_js

APP = "http://127.0.0.1:5057/"
SHOTS = Path("/tmp/demo-shots")
SHOTS.mkdir(exist_ok=True)

DRY = "--dry-run" in sys.argv


def show(tab, subject, hold, shotname, status="pending"):
    r = tab.js(card_js(subject, "show", status))
    print(f"show {subject} [{status}]: {r}")
    time.sleep(hold)
    if shotname:
        tab.shot(str(SHOTS / shotname))
    return r


def wait_show(tab, subject, status, tries=10):
    """After a click the app reloads to the full list; poll until the
    card shows its new status, then hold on it."""
    for _ in range(tries):
        if tab.js(card_js(subject, "show", status)) == "shown":
            return True
        time.sleep(2)
    return False


def main():
    tab = open_tab(APP)
    tab.navigate(APP + "?status=pending", wait=2.5)
    # S1: queue overview
    tab.js("window.scrollTo(0, 0)")
    time.sleep(4 if not DRY else 1)
    tab.js("window.scrollBy(0, 500)")
    time.sleep(4 if not DRY else 1)

    # S2/S3: Ashtabula — approve the address completion
    show(tab, "bellhaven-of-ashtabula", 10 if not DRY else 1, "ashtabula.png")
    if not DRY:
        print(tab.js(card_js("bellhaven-of-ashtabula", "approve")))
        time.sleep(3)
        wait_show(tab, "bellhaven-of-ashtabula", "applied")
        time.sleep(7)
        tab.shot(str(SHOTS / "ashtabula-applied.png"))

    # S4/S5: Union Square — reject the unprovable match
    show(tab, "bellhaven-at-union-square", 10 if not DRY else 1, "union.png")
    if not DRY:
        print(tab.js(card_js("bellhaven-at-union-square", "reject")))
        time.sleep(3)
        wait_show(tab, "bellhaven-at-union-square", "rejected")
        time.sleep(6)
        tab.shot(str(SHOTS / "union-rejected.png"))

    # S6/S7: Marietta — approve the CHOW split
    show(tab, "bellhaven-of-marietta", 10 if not DRY else 1, "marietta.png")
    if not DRY:
        print(tab.js(card_js("bellhaven-of-marietta", "approve")))
        time.sleep(3)
        wait_show(tab, "bellhaven-of-marietta", "applied")
        time.sleep(7)
        tab.shot(str(SHOTS / "marietta-applied.png"))

    # S8: the applied view — proof the changes landed (executor results
    # on each card are the CRM's own read-back after writing)
    if not DRY:
        tab.navigate(APP + "?status=applied", wait=2.5)
        tab.js("window.scrollTo(0, 0)")
        time.sleep(6)
        tab.js("window.scrollBy(0, 600)")
        time.sleep(6)
        tab.shot(str(SHOTS / "applied-view.png"))

    print("done", "(dry run)" if DRY else "(full take)")


if __name__ == "__main__":
    main()
