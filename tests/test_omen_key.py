"""Unit checks for the OMEN logo-key shortcut helpers. Run: python3 tests/test_omen_key.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import omen_key


def test_ensure_adds_once():
    assert omen_key.ensure_omen_path([]) == [omen_key.OMEN_PATH]
    assert omen_key.ensure_omen_path([omen_key.OMEN_PATH]) == [omen_key.OMEN_PATH]


def test_ensure_preserves_others():
    others = ["/org/example/a/", "/org/example/b/"]
    assert omen_key.ensure_omen_path(others) == others + [omen_key.OMEN_PATH]
    # existing omen entry moves to the end, others keep order, no duplicates
    mixed = [others[0], omen_key.OMEN_PATH, others[1]]
    assert omen_key.ensure_omen_path(mixed) == others + [omen_key.OMEN_PATH]


def test_remove_keeps_others():
    others = ["/org/example/a/", "/org/example/b/"]
    assert omen_key.remove_omen_path(others + [omen_key.OMEN_PATH]) == others
    assert omen_key.remove_omen_path(others) == others
    assert omen_key.remove_omen_path([]) == []


def test_payload_shape():
    p = omen_key.binding_payload("OMEN Key", "omen-macro")
    assert p == {"name": "OMEN Key", "command": "omen-macro",
                 "binding": "XF86Launch2"}
    assert omen_key.OMEN_ACCEL == "XF86Launch2"


def test_describe():
    assert "does nothing" in omen_key.describe_binding(None)
    assert "XF86Launch2" in omen_key.describe_binding(
        {"name": "OMEN Key", "command": "omen-macro", "binding": "XF86Launch2"})
    assert "omen-macro" in omen_key.describe_binding(
        {"name": "OMEN Key", "command": "omen-macro", "binding": "XF86Launch2"})


def test_normalize():
    assert omen_key.normalize_paths(None) == []
    assert omen_key.normalize_paths([]) == []
    assert omen_key.normalize_paths(["a", "b"]) == ["a", "b"]
    assert omen_key.normalize_paths(["", "a"]) == ["a"]


def test_key_accel_and_path():
    assert omen_key.key_accel("OMEN") == "XF86Launch2"
    assert omen_key.key_accel("P1") == "KP_1"
    assert omen_key.key_accel("P6") == "KP_6"
    assert omen_key.key_path("OMEN") == omen_key.OMEN_PATH
    assert omen_key.key_path("P1").endswith("/omen-macro-p1/")
    assert omen_key.key_path("P1") != omen_key.key_path("P2")
    assert len(set(omen_key.key_path(k) for k in omen_key.COMMANDABLE_KEYS)) == 7


def test_general_path_helpers():
    a, b = "/org/example/a/", "/org/example/b/"
    assert omen_key.ensure_binding_path([a], b) == [a, b]
    assert omen_key.ensure_binding_path([a, b], a) == [b, a]
    assert omen_key.remove_binding_path([a, b], a) == [b]


def test_normalize_commands():
    cmds, problems = omen_key.normalize_commands({})
    assert cmds == {} and problems == []
    cmds, problems = omen_key.normalize_commands(
        {"commands": {"P1": "firefox", "OMEN": "omen-macro"}})
    assert cmds == {"P1": "firefox", "OMEN": "omen-macro"} and problems == []
    # legacy omen_key migrates
    cmds, problems = omen_key.normalize_commands(
        {"omen_key": {"command": "omen-macro"}})
    assert cmds == {"OMEN": "omen-macro"} and problems == []
    # explicit commands win over legacy
    cmds, _ = omen_key.normalize_commands(
        {"commands": {"OMEN": "xterm"}, "omen_key": {"command": "omen-macro"}})
    assert cmds == {"OMEN": "xterm"}
    # junk is reported, not stored
    cmds, problems = omen_key.normalize_commands(
        {"commands": {"P7": "x", "P1": "", "P2": 42}})
    assert cmds == {} and len(problems) == 3


def test_live_read_only():
    """Read-only probe of the real session: must not raise, must not write."""
    try:
        paths = omen_key.get_custom_paths()
        assert isinstance(paths, list)
        data = omen_key.get_omen_binding()
        assert data is None or set(data) == {"name", "command", "binding"}
        print(f"     live: {len(paths)} custom shortcut(s), "
              f"omen bound={omen_key.OMEN_PATH in paths}")
    except OSError as exc:
        # Headless CI has no dconf; a clean OSError is acceptable, a crash is not.
        print(f"     live: unavailable ({exc})")


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
