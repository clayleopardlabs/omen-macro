"""Unit checks for the EC macro table encoder.  Run: python3 tests/test_codec.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import codec


def test_numpad_default_shape():
    p = codec.build_profile(codec.NUMPAD_DEFAULT, 20)
    assert len(p) == 4096
    # P1 -> KP_1: length 3, make 0x4f, break 0xcf
    assert p[0:3] == bytes((0x03, 0x4F, 0xCF))
    # P2 follows immediately
    assert p[3:6] == bytes((0x03, 0x50, 0xD0))
    # P4 at bytes 9..11, P6 at 15..17, then the modifier slots are empty
    assert p[9:12] == bytes((0x03, 0x4B, 0xCB))   # P4 -> KP_4
    assert p[15:18] == bytes((0x03, 0x4D, 0xCD))  # P6 -> KP_6
    assert p[18] == 0x01                          # Ctrl+P1 empty


def test_empty_slots_are_single_01():
    p = codec.build_profile({}, 0)
    assert p[:30] == b"\x01" * 30
    assert p[30] == 0x01  # trailing marker
    assert sum(p) == 31


def test_chord_press_release_order():
    entry = codec.encode_entry([["Control_L", "c"]], 0)
    assert entry == bytes((0x05, 0x1D, 0x2E, 0xAE, 0x9D))


def test_delay_between_chords():
    entry = codec.encode_entry([["a"], ["b"]], 20)
    # len, a down, a up, delay marker+20, b down, b up
    assert entry == bytes((0x07, 0x1E, 0x9E, 0xFF, 20, 0x30, 0xB0))


def test_long_delay_splits():
    entry = codec.encode_entry([["a"], ["b"]], 300)
    assert entry[0] == 0x09
    assert entry[3:9] == bytes((0xFF, 255, 0xFF, 45, 0x30, 0xB0))


def test_extended_key_gets_e0_prefix():
    entry = codec.encode_entry([["Up"]], 0)
    assert entry == bytes((0x05, 0xE0, 0x48, 0xE0, 0xC8))


def test_spacing_zero_has_no_markers():
    entry = codec.encode_entry([["a"], ["b"]], 0)
    assert 0xFF not in entry


def test_unsupported_key_raises():
    for name in ("F13", "XF86AudioMute", "Bogus"):
        try:
            codec.build_profile({"P1": [[name]]})
        except codec.MacroError:
            pass
        else:
            raise AssertionError(f"{name} should be rejected")


def test_too_long_entry_raises():
    chords = [["a"]] * 200  # 200 * 2 bytes + 199 * 2 delay bytes, way over 254
    try:
        codec.encode_entry(chords, 20)
    except codec.MacroError:
        pass
    else:
        raise AssertionError("over-long macro should be rejected")


def test_all_modifier_slots_encode():
    slots = {name: [["a"]] for name in codec.SLOT_NAMES}
    p = codec.build_profile(slots, 0)
    assert len(p) == 4096
    # 30 entries of 3 bytes + trailing marker
    assert sum(1 for b in p if b) == 30 * 3 + 1


def test_roundtrip_description():
    p = codec.build_profile({"P1": [["Control_L", "c"]]}, 0)
    assert codec.describe_entry(codec.decode_entry(p[:p[0]])) == "Ctrl+c"


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
