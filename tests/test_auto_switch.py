"""Unit checks for the auto-switch matcher and rules file.

Run: python3 tests/test_auto_switch.py
The live focus probe is read-only: it must return a tuple or None, never raise.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import auto_switch


def test_match_first_wins():
    rules = [{"match": "fire", "profile": "Work"},
             {"match": "firefox", "profile": "Browse"}]
    assert auto_switch.match_app("Firefox", "New Tab", rules) == "Work"
    assert auto_switch.match_app("Firefox", "New Tab",
                                 rules[1:]) == "Browse"


def test_match_case_insensitive_and_title():
    rules = [{"match": "STRIKE", "profile": "Gaming"}]
    assert auto_switch.match_app("steam", "Counter-Strike 2", rules) == "Gaming"
    assert auto_switch.match_app("steam", "Library", rules) is None


def test_match_skips_blanks():
    rules = [{"match": "  ", "profile": "Gaming"}, {"no-match": 1}]
    assert auto_switch.match_app("anything", "here", rules) is None
    assert auto_switch.match_app("a", "b", []) is None


def test_rules_roundtrip():
    root = Path(tempfile.mkdtemp(prefix="omen-auto-"))
    rules = [{"match": "cs2", "profile": "Gaming"}]
    auto_switch.save_rules(root, rules, "Default")
    loaded, fallback = auto_switch.load_rules(root)
    assert loaded == rules and fallback == "Default"


def test_rules_validation():
    root = Path(tempfile.mkdtemp(prefix="omen-auto-"))
    try:
        auto_switch.save_rules(root, [{"match": "", "profile": "Gaming"}], None)
    except ValueError:
        pass
    else:
        raise AssertionError("blank match should be rejected")
    loaded, _ = auto_switch.load_rules(root)
    assert loaded == []


def test_live_focus_probe():
    detected = auto_switch.active_window()
    assert detected is None or (
        isinstance(detected, tuple) and len(detected) == 2
        and all(isinstance(s, str) for s in detected))
    print(f"     live focus: {detected!r}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
