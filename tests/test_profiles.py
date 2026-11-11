"""Unit checks for the named-profile store. Run: python3 tests/test_profiles.py"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import profiles


def _root():
    tmp = tempfile.mkdtemp(prefix="omen-profiles-")
    return Path(tmp)


def test_seed_default():
    root = _root()
    active = profiles.ensure_store(root)
    assert active == "Default"
    assert profiles.list_profiles(root) == ["Default"]
    assert profiles.get_active(root) == "Default"
    cfg = profiles.read_profile(root, "Default")
    assert cfg["slots"]["P1"] == [["KP_1"]]
    assert cfg["spacing_ms"] == 20


def test_save_list_rename_delete():
    root = _root()
    profiles.ensure_store(root)
    profiles.save_profile(root, "Gaming", {"spacing_ms": 0, "slots": {}})
    assert profiles.list_profiles(root) == ["Default", "Gaming"]
    profiles.rename_profile(root, "Gaming", "Work")
    assert profiles.list_profiles(root) == ["Default", "Work"]
    profiles.delete_profile(root, "Work")
    assert profiles.list_profiles(root) == ["Default"]


def test_bad_names():
    root = _root()
    profiles.ensure_store(root)
    for bad in ["", "a/b", "..", "x" * 41, "has space", "semi;colon"]:
        try:
            profiles.validate_name(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should be rejected")
    assert profiles.validate_name("  Gaming-2_x ") == "Gaming-2_x"


def test_active_pointer():
    root = _root()
    profiles.ensure_store(root)
    profiles.save_profile(root, "Gaming", {"spacing_ms": 0, "slots": {}})
    profiles.set_active(root, "Gaming")
    assert profiles.get_active(root) == "Gaming"
    try:
        profiles.set_active(root, "Nope")
    except ValueError:
        pass
    else:
        raise AssertionError("activating a missing profile should fail")
    cfg, name, path = profiles.resolve_config(root)
    assert name == "Gaming" and path.is_file() and cfg["spacing_ms"] == 0


def test_resolve_prefer_and_missing():
    root = _root()
    profiles.ensure_store(root)
    cfg, name, _ = profiles.resolve_config(root, prefer="Default")
    assert name == "Default" and isinstance(cfg, dict)
    try:
        profiles.resolve_config(root, prefer="Nope")
    except FileNotFoundError as exc:
        assert "Default" in str(exc)
    else:
        raise AssertionError("missing profile should raise FileNotFoundError")


def test_legacy_migration():
    root = _root()
    legacy = {"spacing_ms": 30,
              "slots": {"P1": [["a"]]},
              "omen_key": {"command": "omen-macro"}}
    root.mkdir(parents=True, exist_ok=True)
    (root / "profile.json").write_text(json.dumps(legacy))
    active = profiles.ensure_store(root)
    assert active == "Default"
    cfg = profiles.read_profile(root, "Default")
    assert cfg["slots"] == {"P1": [["a"]]}
    assert cfg["spacing_ms"] == 30
    assert cfg["commands"] == {"OMEN": "omen-macro"}


def test_config_root_override():
    root = _root()
    os.environ["OMEN_MACRO_ROOT"] = str(root)
    try:
        assert profiles.config_root() == root
    finally:
        del os.environ["OMEN_MACRO_ROOT"]
    assert profiles.config_root() in (
        Path("/etc/omen-macro"), Path.home() / ".config" / "omen-macro")


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
