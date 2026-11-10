"""Encoder for the HP OMEN EC macro-key table (P1..P6 and modifier variants).

Background
----------
The six macro keys on the left of an OMEN keyboard are not normal keys: until
the embedded controller (EC) is given a macro table they emit nothing at all.
HP's Windows "OMEN Gaming Hub" builds a 4096 byte table and hands it to the EC
through an ACPI/WMI call (HPWMI query 15) and then enables the engine (query 23).

That was reverse engineered by Lan Tian (2022) and implemented for Linux in
xddxdd/hp-omen-linux-module, later ported forward by FamleeX/hp-omen-cm2047nr.

Table layout
------------
The table is a concatenation of entries, in this slot order (OGH's
EnumMacroKeyMarlins with the leading "FN" pseudo-entry skipped):

    0..5    P1 .. P6
    6..11   Ctrl+P1 .. Ctrl+P6
    12..17  Alt+P1 .. Alt+P6
    18..23  Shift+P1 .. Shift+P6
    24..29  Fn+P1 .. Fn+P6

followed by one extra 0x01 byte (present in HP's implementation, reason
unknown but harmless).

Every entry is:  [length][bytes...]
    length  counts itself, so an entry holding N payload bytes uses N+1.
            An empty slot is a single 0x01 byte.
    bytes   PS/2 scan-code-set-1 make codes; the release code is make | 0x80.
            0xFF followed by a millisecond value (0..255) is a delay.
            Extended keys are prefixed with 0xE0.

The EC replays these sequences, so the host sees ordinary keystrokes: nothing
in userspace needs special privileges beyond writing the profile once.
"""

from __future__ import annotations

SLOT_NAMES = [
    "P1", "P2", "P3", "P4", "P5", "P6",
    "Ctrl+P1", "Ctrl+P2", "Ctrl+P3", "Ctrl+P4", "Ctrl+P5", "Ctrl+P6",
    "Alt+P1", "Alt+P2", "Alt+P3", "Alt+P4", "Alt+P5", "Alt+P6",
    "Shift+P1", "Shift+P2", "Shift+P3", "Shift+P4", "Shift+P5", "Shift+P6",
    "Fn+P1", "Fn+P2", "Fn+P3", "Fn+P4", "Fn+P5", "Fn+P6",
]
PRIMARY_SLOTS = SLOT_NAMES[:6]

PROFILE_SIZE = 4096
MAX_ENTRY_PAYLOAD = 254  # length byte counts itself, u8

DELAY_MARKER = 0xFF
RELEASE_BIT = 0x80
EXTENDED = 0xE0


def _m(code: int) -> tuple[int, ...]:
    """Scan-code-set-1 make code, optionally 0xE0-prefixed."""
    return (code,)


def _e(code: int) -> tuple[int, ...]:
    """Extended key: 0xE0 prefix + code."""
    return (EXTENDED, code)


# Canonical name -> make-code bytes.  Names match GDK/GDK keyval names where
# possible so the GUI can pass Gdk.Keyval.name() straight through.
_PLAIN = {
    "Escape": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "minus": 0x0C, "equal": 0x0D, "BackSpace": 0x0E, "Tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "bracketleft": 0x1A, "bracketright": 0x1B, "Return": 0x1C,
    "Control_L": 0x1D, "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21,
    "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
    "semicolon": 0x27, "apostrophe": 0x28, "grave": 0x29, "Shift_L": 0x2A,
    "backslash": 0x2B, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F,
    "b": 0x30, "n": 0x31, "m": 0x32, "comma": 0x33, "period": 0x34,
    "slash": 0x35, "Shift_R": 0x36, "KP_Multiply": 0x37, "Alt_L": 0x38,
    "space": 0x39, "Caps_Lock": 0x3A,
    "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E, "F5": 0x3F, "F6": 0x40,
    "F7": 0x41, "F8": 0x42, "F9": 0x43, "F10": 0x44,
    "Num_Lock": 0x45, "Scroll_Lock": 0x46,
    "KP_7": 0x47, "KP_8": 0x48, "KP_9": 0x49, "KP_Subtract": 0x4A,
    "KP_4": 0x4B, "KP_5": 0x4C, "KP_6": 0x4D, "KP_Add": 0x4E,
    "KP_1": 0x4F, "KP_2": 0x50, "KP_3": 0x51, "KP_0": 0x52,
    "KP_Decimal": 0x53, "F11": 0x57, "F12": 0x58,
}

_EXTENDED = {
    "KP_Enter": 0x1C, "Control_R": 0x1D, "KP_Divide": 0x35,
    "Print": 0x37, "Alt_R": 0x38,
    "Home": 0x47, "Up": 0x48, "Page_Up": 0x49,
    "Left": 0x4B, "Right": 0x4D,
    "End": 0x4F, "Down": 0x50, "Page_Down": 0x51,
    "Insert": 0x52, "Delete": 0x53,
    "Super_L": 0x5B, "Super_R": 0x5C, "Menu": 0x5D,
}

SCANCODES: dict[str, tuple[int, ...]] = {}
SCANCODES.update({k: _m(v) for k, v in _PLAIN.items()})
SCANCODES.update({k: _e(v) for k, v in _EXTENDED.items()})

# Tolerated spellings coming out of GTK / user config.
ALIASES = {
    "ISO_Left_Tab": "Tab",
    "KP_Separator": "KP_Decimal",
    "Return2": "Return",
    "Control_L2": "Control_L",
    "spacebar": "space",
    "Escape2": "Escape",
    "Prior": "Page_Up",
    "Next": "Page_Down",
}

# Human-friendly rendering for the UI.
PRETTY = {
    "Control_L": "Ctrl", "Control_R": "RCtrl",
    "Shift_L": "Shift", "Shift_R": "RShift",
    "Alt_L": "Alt", "Alt_R": "RAlt",
    "Super_L": "Super", "Super_R": "RSup",
    "Return": "Enter", "Escape": "Esc", "BackSpace": "Backspace",
    "Page_Up": "PgUp", "Page_Down": "PgDn",
    "KP_Enter": "NumEnter", "KP_Decimal": "Num.", "KP_Add": "Num+",
    "KP_Subtract": "Num-", "KP_Multiply": "Num*", "KP_Divide": "Num/",
    "space": "Space",
}

# Keys that exist on a PC keyboard but have no scan-code-set-1 code, so the EC
# cannot replay them.  Listed explicitly to give a useful error instead of a
# silent "unsupported key".
UNSUPPORTED_HINTS = {
    "F13": "F13-F24 have no PS/2 scan code set 1 code",
    "F24": "F13-F24 have no PS/2 scan code set 1 code",
    "XF86AudioMute": "media/volume keys are handled by ACPI, not by the EC macro engine",
    "XF86AudioLowerVolume": "media/volume keys are handled by ACPI, not by the EC macro engine",
    "XF86MonBrightnessUp": "brightness keys are handled by ACPI, not by the EC macro engine",
}


class MacroError(Exception):
    """Raised when a macro cannot be encoded for the EC."""


def normalise(name: str) -> str:
    """Return the canonical name for a key, or raise MacroError."""
    if name in SCANCODES:
        return name
    if name in ALIASES:
        return ALIASES[name]
    # GTK hands back e.g. "F13", "XF86AudioMute"; give a targeted message.
    if name in UNSUPPORTED_HINTS:
        raise MacroError(f"{name}: {UNSUPPORTED_HINTS[name]}")
    raise MacroError(
        f"unsupported key {name!r}: the EC can only replay keys that have a "
        "PS/2 scan code set 1 code"
    )


def pretty(name: str) -> str:
    return PRETTY.get(name, name)


def make_bytes(name: str) -> bytes:
    return bytes(SCANCODES[normalise(name)])


def break_bytes(name: str) -> bytes:
    """Release code: same as make but with bit 7 set on the last byte."""
    code = bytearray(SCANCODES[normalise(name)])
    code[-1] |= RELEASE_BIT
    return bytes(code)


def encode_delay(ms: int) -> bytes:
    """Encode a delay; the EC only understands 0..255 ms per marker pair."""
    if ms <= 0:
        return b""
    out = bytearray()
    while ms >= 255:
        out += bytes((DELAY_MARKER, 255))
        ms -= 255
    if ms:
        out += bytes((DELAY_MARKER, ms))
    return bytes(out)


def encode_chord(keys: list[str]) -> bytes:
    """Encode one chord: press in order, release in reverse order."""
    if not keys:
        return b""
    out = bytearray()
    for k in keys:
        out += make_bytes(k)
    for k in reversed(keys):
        out += break_bytes(k)
    return bytes(out)


def encode_entry(chords: list[list[str]], spacing_ms: int = 0) -> bytes:
    """Encode the payload+length-byte for a single macro slot."""
    payload = bytearray()
    for i, chord in enumerate(chords):
        payload += encode_chord(chord)
        if i != len(chords) - 1:
            payload += encode_delay(spacing_ms)

    if len(payload) > MAX_ENTRY_PAYLOAD:
        raise MacroError(
            f"macro too long: {len(payload)} payload bytes, the EC allows at "
            f"most {MAX_ENTRY_PAYLOAD}. Remove some keystrokes."
        )
    return bytes((len(payload) + 1,)) + bytes(payload)


def empty_entry() -> bytes:
    return b"\x01"


def build_profile(
    slots: dict[str, list[list[str]]],
    spacing_ms: int = 0,
) -> bytes:
    """Build the full 4096 byte EC macro table.

    ``slots`` maps slot name (see SLOT_NAMES) to a list of chords, e.g.
        {"P1": [["Control_L", "c"], ["v"]]}
    which replays Ctrl+C then v.  Unlisted slots are left empty.
    """
    if spacing_ms < 0 or spacing_ms > 255 * 8:
        raise MacroError("spacing must be between 0 and 2040 ms")

    table = bytearray()
    for name in SLOT_NAMES:
        chords = slots.get(name) or []
        if not chords:
            table += empty_entry()
            continue
        table += encode_entry(chords, spacing_ms)

    table += b"\x01"  # trailing 0x01 that HP also emits
    if len(table) > PROFILE_SIZE:
        raise MacroError(
            f"macro table overflows {PROFILE_SIZE} bytes ({len(table)}); "
            "shorten the macros"
        )
    return bytes(table) + bytes(PROFILE_SIZE - len(table))


def decode_entry(data: bytes) -> list[list[str]]:
    """Best-effort decode for the UI's summary text (not used for writing)."""
    rev = {}
    for name, code in SCANCODES.items():
        rev.setdefault(bytes(code), name)
        rev.setdefault(break_bytes(name), ("-", name))

    if not data or data[0] == 1:
        return []
    body = data[1:1 + data[0] - 1]
    chords: list[list[str]] = []
    held: list[str] = []
    i = 0
    while i < len(body):
        b = body[i]
        if b == DELAY_MARKER:
            i += 2
            continue
        if b == EXTENDED and i + 1 < len(body):
            key = bytes(body[i:i + 2])
            i += 2
        else:
            key = bytes((b,))
            i += 1
        info = rev.get(key)
        if info is None:
            continue
        if isinstance(info, tuple):  # release
            if held and held[-1] == info[1]:
                held.pop()
            if not held and chords:
                pass
        else:
            held.append(info)
            if len(held) == 1:
                chords.append([info])
            else:
                chords[-1].append(info)
    return chords


def describe_chord(chord: list[str]) -> str:
    if not chord:
        return "(empty)"
    if len(chord) == 1:
        return pretty(chord[0])
    return "+".join(pretty(k) for k in chord)


def describe_entry(chords: list[list[str]]) -> str:
    if not chords:
        return "empty"
    return ", ".join(describe_chord(c) for c in chords)


# Slot name -> chords for the stock numpad layout used by the reference driver.
NUMPAD_DEFAULT: dict[str, list[list[str]]] = {
    "P1": [["KP_1"]], "P2": [["KP_2"]], "P3": [["KP_3"]],
    "P4": [["KP_4"]], "P5": [["KP_5"]], "P6": [["KP_6"]],
    "Fn+P1": [["KP_7"]], "Fn+P2": [["KP_8"]], "Fn+P3": [["KP_9"]],
    "Fn+P4": [["KP_0"]], "Fn+P5": [["KP_Subtract"]], "Fn+P6": [["KP_Add"]],
}


def config_to_profile(cfg: dict) -> bytes:
    """Build a profile from a config dict as stored in profile.json."""
    slots = {k: v for k, v in (cfg.get("slots") or {}).items() if v}
    return build_profile(slots, int(cfg.get("spacing_ms", 20)))
