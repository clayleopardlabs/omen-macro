"""Tests for the manual macro editor: the text parser in codec.py and the pure
helpers behind the GUI's editor dialog.

Run from the repo root:  python3 tests/test_editor.py

Plain script on purpose (pytest is not a dependency, see test_codec.py for the
same house style).  No display, no root, no network: nothing here creates a GTK
window.  ``omen_macro.gui`` imports headlessly (``gi`` + the widgets modules do
not touch X/Wayland until a window is realised), so the helper tests can import
it directly; if that ever stops being true they report a clear skip.

Model under test (codec.py parser, incl. the working-tree numpad-plus fix):

* a macro is ``list[list[str]]``: ordered steps; a step/chord is the list of
  canonical key names pressed together;
* ``+`` joins keys inside one step, whitespace/comma/semicolon separate steps;
* a double-quoted field is literal text, one step per character (Shift added
  for uppercase and US-layout shifted punctuation);
* names are case-insensitive; canonical names, ALIASES and the UI's PRETTY
  names are all accepted; the pretty name "Num+" is the single key KP_Add, and
  a '+' belongs to a key name when the text so far plus '+' is a known name
  ("Ctrl+Num+" is Ctrl + KP_Add).  format_chord() falls back to canonical names
  for multi-key chords whose pretty name contains '+', so mixed numpad-plus
  chords round-trip.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import codec


def expect_macro_error(fn, *args, **kwargs) -> str:
    """Run fn, assert it raises codec.MacroError, and return its message.

    Also fails when the callee raises something else (IndexError, ValueError,
    KeyError...): the editor maps MacroError to a banner and lets anything else
    crash the click handler, so the exception type is part of the contract.
    """
    try:
        result = fn(*args, **kwargs)
    except codec.MacroError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - the wrong type is a failure here
        raise AssertionError(
            f"expected codec.MacroError, got {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"expected codec.MacroError, got {result!r}")


# --------------------------------------------------------------------------- #
# Parsing: single keys, separators, chords
# --------------------------------------------------------------------------- #
def test_single_character_keys():
    assert codec.parse_steps("a") == [["a"]]
    assert codec.parse_steps("A") == [["a"]]          # uppercase = same key
    assert codec.parse_steps("z") == [["z"]]
    assert codec.parse_steps("Z") == [["z"]]
    for digit in "0123456789":
        assert codec.parse_steps(digit) == [[digit]]
    assert codec.parse_steps("-") == [["minus"]]      # bare punctuation key
    assert codec.parse_steps(" ") == []               # only whitespace
    assert codec.parse_steps("  h  ") == [["h"]]      # surrounding spaces ok


def test_sequential_separators():
    want = [["h"], ["e"]]
    for text in ("h e", "h,e", "h;e", "h\te", "h\ne", "h , ; e", "  h ,e  "):
        assert codec.parse_steps(text) == want, text
    assert codec.parse_steps("h+e") == [["h", "e"]]   # one chord, not two steps
    assert codec.parse_steps("h e t") == [["h"], ["e"], ["t"]]


def test_chord_join_and_spaces_around_plus():
    assert codec.parse_steps("Ctrl+t") == [["Control_L", "t"]]
    assert codec.parse_steps("Ctrl + t") == [["Control_L", "t"]]
    assert codec.parse_steps("Ctrl  +  t") == [["Control_L", "t"]]
    assert codec.parse_steps("Ctrl +Shift+t") == [["Control_L", "Shift_L", "t"]]
    assert codec.parse_steps("  Ctrl + t  ") == [["Control_L", "t"]]
    assert codec.parse_steps("a + b") == [["a", "b"]]


def test_modifier_chords():
    assert codec.parse_steps("Ctrl+Shift+t") == [
        ["Control_L", "Shift_L", "t"]
    ]
    assert codec.parse_steps("ctrl+shift+t") == [
        ["Control_L", "Shift_L", "t"]
    ]
    assert codec.parse_steps("Ctrl+Alt+Delete") == [
        ["Control_L", "Alt_L", "Delete"]
    ]
    assert codec.parse_steps("Super+Left") == [["Super_L", "Left"]]
    assert codec.parse_steps("Shift+Tab") == [["Shift_L", "Tab"]]
    assert codec.parse_steps("CTRL + T") == [["Control_L", "t"]]


def test_aliases():
    assert codec.parse_steps("shift+a") == [["Shift_L", "a"]]
    assert codec.parse_steps("enter") == [["Return"]]
    assert codec.parse_steps("ESC") == [["Escape"]]
    assert codec.parse_steps("backspace") == [["BackSpace"]]
    assert codec.parse_steps("pgup") == [["Page_Up"]]
    assert codec.parse_steps("spacebar") == [["space"]]
    assert codec.parse_steps("Prior") == [["Page_Up"]]
    assert codec.parse_steps("Next") == [["Page_Down"]]
    assert codec.parse_steps("ISO_Left_Tab") == [["Tab"]]
    assert codec.parse_steps("KP_Separator") == [["KP_Decimal"]]
    assert codec.parse_steps("win") == [["Super_L"]]
    assert codec.parse_steps("meta") == [["Super_L"]]
    assert codec.parse_steps("del") == [["Delete"]]
    assert codec.parse_steps("ins") == [["Insert"]]


def test_pretty_names():
    """Every PRETTY name the UI prints must be typeable back unchanged."""
    for canonical, nice in codec.PRETTY.items():
        assert codec.parse_steps(nice) == [[canonical]], nice
        assert codec.parse_steps(nice.upper()) == [[canonical]], nice.upper()
    # ... and the canonical names themselves.
    for canonical in codec.SCANCODES:
        assert codec.parse_steps(canonical) == [[canonical]], canonical


def test_numpad_names():
    assert codec.parse_steps("Num+") == [["KP_Add"]]
    assert codec.parse_steps("NUM+") == [["KP_Add"]]
    assert codec.parse_steps("num +") == [["KP_Add"]]   # spaces around '+'
    assert codec.parse_steps("Num-") == [["KP_Subtract"]]
    assert codec.parse_steps("Num.") == [["KP_Decimal"]]
    assert codec.parse_steps("Num*") == [["KP_Multiply"]]
    assert codec.parse_steps("Num/") == [["KP_Divide"]]
    assert codec.parse_steps("NumEnter") == [["KP_Enter"]]
    assert codec.parse_steps("KP_1") == [["KP_1"]]
    assert codec.parse_steps("KP_Add") == [["KP_Add"]]
    # A numpad key joined with a modifier still needs the canonical spelling.
    assert codec.parse_steps("Ctrl+KP_1") == [["Control_L", "KP_1"]]


# --------------------------------------------------------------------------- #
# Parsing: quoted literal text
# --------------------------------------------------------------------------- #
def test_literal_quoted_text():
    assert codec.parse_steps('"abc"') == [["a"], ["b"], ["c"]]
    assert codec.parse_steps('"Hi!"') == [
        ["Shift_L", "h"], ["i"], ["Shift_L", "1"]
    ]
    assert codec.parse_steps('"a:b"') == [
        ["a"], ["Shift_L", "semicolon"], ["b"]
    ]
    assert codec.parse_steps('"_"') == [["Shift_L", "minus"]]
    assert codec.parse_steps('"~"') == [["Shift_L", "grave"]]
    assert codec.parse_steps('"A"') == [["Shift_L", "a"]]
    assert codec.parse_steps('" "') == [["space"]]
    assert codec.parse_steps('"+"') == [["Shift_L", "equal"]]
    assert codec.parse_steps('"0123"') == [["0"], ["1"], ["2"], ["3"]]
    assert codec.parse_steps('"a b"') == [["a"], ["space"], ["b"]]
    assert codec.parse_steps('  "Hi"  ') == [["Shift_L", "h"], ["i"]]  # strip
    assert codec.parse_steps('""') == []


def test_literal_quoted_errors():
    msg = expect_macro_error(codec.parse_steps, '"é"')
    assert "cannot type" in msg and "é" in msg
    msg = expect_macro_error(codec.parse_steps, '"🙂"')
    assert "cannot type" in msg
    for bad in ('"', '"Hi', '"Hi there', '"' + "x" * 20):
        msg = expect_macro_error(codec.parse_steps, bad)
        assert "unterminated" in msg, (bad, msg)


def test_empty_input_is_no_steps():
    for text in ("", "   ", "\t", "\n", " \t\n "):
        assert codec.parse_steps(text) == [], repr(text)
    # parse_chord() is the one that refuses emptiness.
    assert "empty step" in expect_macro_error(codec.parse_chord, "")
    assert "empty key" in expect_macro_error(codec.parse_key_token, "   ")


# --------------------------------------------------------------------------- #
# Parsing: errors
# --------------------------------------------------------------------------- #
def test_duplicate_key_in_chord_rejected():
    for text in ("a+a", "Ctrl+Ctrl", "Ctrl+Control_L", "Shift+Shift_L",
                 "ctrl + CONTROL_L"):
        msg = expect_macro_error(codec.parse_steps, text)
        assert "twice" in msg, (text, msg)
    # The same key in different steps is fine.
    assert codec.parse_steps("a, a") == [["a"], ["a"]]


def test_unsupported_key_errors_are_targeted():
    msg = expect_macro_error(codec.parse_steps, "F13")
    assert "F13-F24" in msg, msg
    msg = expect_macro_error(codec.parse_steps, "F24")
    assert "F13-F24" in msg, msg
    msg = expect_macro_error(codec.parse_steps, "XF86AudioMute")
    assert "ACPI" in msg, msg
    msg = expect_macro_error(codec.parse_steps, "Bogus")
    assert "unsupported key" in msg and "Bogus" in msg, msg
    # An unknown name inside a chord names the offending token.
    msg = expect_macro_error(codec.parse_steps, "Ctrl+Bogus")
    assert "Bogus" in msg, msg


def test_shifted_single_char_gives_hint():
    for char, target in (("!", "1"), ("@", "2"), (":", "semicolon"),
                         ("_", "minus"), ("~", "grave"), ("?", "slash")):
        msg = expect_macro_error(codec.parse_steps, char)
        assert "shifted character" in msg, (char, msg)
        assert f"Shift+{target}" in msg, (char, msg)
        assert "quotes" in msg, (char, msg)
    # A lone '"' is consumed by parse_steps() as the start of a quoted field,
    # so the shifted-character hint for it is only reachable token-wise.
    msg = expect_macro_error(codec.parse_key_token, '"')
    assert "shifted character" in msg and "Shift+apostrophe" in msg, msg


def test_parse_key_token():
    assert codec.parse_key_token(" h ") == "h"
    assert codec.parse_key_token("H") == "h"
    assert codec.parse_key_token("Ctrl") == "Control_L"
    assert codec.parse_key_token("NUM+") == "KP_Add"
    assert codec.parse_key_token("Esc") == "Escape"
    assert codec.parse_key_token("Page_Up") == "Page_Up"
    msg = expect_macro_error(codec.parse_key_token, "")
    assert "empty key" in msg
    msg = expect_macro_error(codec.parse_key_token, "F13")
    assert "F13" in msg


def test_parse_chord():
    assert codec.parse_chord("Ctrl+Shift+t") == [
        "Control_L", "Shift_L", "t"
    ]
    assert codec.parse_chord("h+e") == ["h", "e"]
    assert codec.parse_chord("Num+") == ["KP_Add"]
    assert codec.parse_chord("Num-") == ["KP_Subtract"]
    assert codec.parse_chord("Ctrl+KP_1") == ["Control_L", "KP_1"]
    assert codec.parse_chord("  Ctrl + t  ") == ["Control_L", "t"]
    # Num+ inside a chord: the leading '+' belongs to the key name.
    assert codec.parse_chord("Ctrl+Num+") == ["Control_L", "KP_Add"]
    assert codec.parse_chord("Ctrl+Num-") == ["Control_L", "KP_Subtract"]
    assert codec.parse_chord("Num++a") == ["KP_Add", "a"]
    assert "empty step" in expect_macro_error(codec.parse_chord, "  ")
    assert "empty key" in expect_macro_error(codec.parse_chord, "Ctrl+")
    assert "empty key" in expect_macro_error(codec.parse_chord, "+a")
    assert "empty key" in expect_macro_error(codec.parse_chord, "Ctrl++t")
    assert "twice" in expect_macro_error(codec.parse_chord, "Ctrl+Control_L")


def test_literal_text_steps():
    assert codec.literal_text_steps("") == []
    assert codec.literal_text_steps("abc") == [["a"], ["b"], ["c"]]
    assert codec.literal_text_steps("A") == [["Shift_L", "a"]]
    assert codec.literal_text_steps("!") == [["Shift_L", "1"]]
    assert codec.literal_text_steps(" ") == [["space"]]
    msg = expect_macro_error(codec.literal_text_steps, "é")
    assert "cannot type" in msg


def test_bad_input_always_raises_macro_error():
    """Every malformed editor field must fail as MacroError, never otherwise."""
    bad_inputs = [
        "!", "@", "#", "$", "%", "^", "&", "*", "(", ")", "_", "+", "{",
        "}", "|", ":", '"', "<", ">", "?", "~",
        "Ctrl+", "+a", "Ctrl++t", "a++b", "++", "a+a", "Ctrl+Control_L",
        "F13", "F24", "XF86AudioMute", "XF86MonBrightnessUp", "Bogus",
        "0x1b", "NoSuchKey+Ctrl",
        '"', '"Hi', '"unterminated', '"é"', '"🙂"',
        # Pretty numpad-plus with a key pasted straight onto it (ambiguous).
        "Ctrl+Num+Shift", "Num+Shift", "Num+ b",
    ]
    for text in bad_inputs:
        expect_macro_error(codec.parse_steps, text)
    # A handful of values that look broken but are legal.
    for text, want in [
        ("-", [["minus"]]),
        ("=", [["equal"]]),
        ("`", [["grave"]]),
        ('"~"', [["Shift_L", "grave"]]),
        ("Num+", [["KP_Add"]]),
        # "Num+" inside a chord: first '+' is part of the key name.
        ("Ctrl+Num+", [["Control_L", "KP_Add"]]),
        ("Num++a", [["KP_Add", "a"]]),
        ("Ctrl+Num-", [["Control_L", "KP_Subtract"]]),
    ]:
        assert codec.parse_steps(text) == want, (text, codec.parse_steps(text))


# --------------------------------------------------------------------------- #
# Formatting: round-trips
# --------------------------------------------------------------------------- #
def test_format_roundtrip_every_scan_code():
    """parse_steps(format_chord([key])) == [[key]] for all 80 canonical keys."""
    failures = []
    for key in codec.SCANCODES:
        text = codec.format_chord([key])
        try:
            got = codec.parse_steps(text)
        except codec.MacroError as exc:
            failures.append((key, text, f"MacroError: {exc}"))
            continue
        if got != [[key]]:
            failures.append((key, text, got))
    assert not failures, failures


def test_format_steps_and_describe_entry_agree():
    chords = [["Control_L", "c"], ["v"]]
    assert codec.format_steps(chords) == "Ctrl+c, v"
    assert codec.describe_entry(chords) == codec.format_steps(chords)
    for chord in ([["h"]], [["h", "e"]], [["Ctrl" and "Control_L"]],
                  [["KP_Add"]], [["Shift_L", "t"]]):
        assert codec.describe_entry(chord) == codec.format_steps(chord)
    # One deliberate divergence: describe_entry() is display-only and keeps
    # the pretty "Num+", while format_steps() must stay parseable and so uses
    # canonical names for a chord that mixes KP_Add (see format_chord()).
    mixed = [["Control_L", "KP_Add"], ["v"]]
    assert codec.describe_entry(mixed) == "Ctrl+Num+, v"
    assert codec.format_steps(mixed) == "Control_L+KP_Add, v"
    assert codec.parse_steps(codec.format_steps(mixed)) == mixed
    # The empty spellings differ on purpose: describe_entry() is for the row.
    assert codec.describe_entry([]) == "empty"
    assert codec.format_steps([]) == ""
    assert codec.format_chord([]) == ""
    assert codec.describe_chord([]) == "(empty)"


def test_format_steps_skips_empty_chords():
    assert codec.format_steps([["a"], [], ["b"]]) == "a, b"
    assert codec.format_steps([]) == ""
    assert codec.format_chord(["Control_L", "Shift_L", "t"]) == "Ctrl+Shift+t"
    assert codec.format_chord(["KP_Add"]) == "Num+"
    assert codec.format_chord(["KP_Decimal"]) == "Num."


def test_split_chord():
    assert codec.split_chord([]) == []
    assert codec.split_chord(["h"]) == [["h"]]
    assert codec.split_chord(["h", "e"]) == [["h"], ["e"]]
    assert codec.split_chord(["Control_L", "Shift_L", "t"]) == [
        ["Control_L"], ["Shift_L"], ["t"]
    ]
    # The split result is directly usable as a macro.
    chord = ["Control_L", "c"]
    steps = codec.split_chord(chord)
    assert steps == [["Control_L"], ["c"]]
    assert codec.parse_steps(codec.format_steps(steps)) == steps


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
def _sample_chords(seed: int, count: int, max_keys: int = 4):
    """Deterministic random chords of 1..max_keys distinct keys.

    Every canonical key is included, KP_Add too: since format_chord() falls
    back to canonical names for chords whose pretty name contains '+'
    ("Num+"), mixed numpad-plus chords round-trip like any other chord.
    """
    rng = random.Random(seed)
    keys = list(codec.SCANCODES)
    return [
        rng.sample(keys, rng.randint(1, max_keys))
        for _ in range(count)
    ]


def test_chord_property_roundtrip():
    """format_chord -> parse_steps round-trips for 1..4 key chords."""
    for chord in _sample_chords(20241124, 300):
        text = codec.format_chord(chord)
        assert codec.parse_steps(text) == [chord], (chord, text)
        assert codec.parse_chord(text) == chord, (chord, text)


def test_macro_property_roundtrip():
    """format_steps -> parse_steps round-trips whole multi-step macros."""
    rng = random.Random(424242)
    keys = list(codec.SCANCODES)
    for _ in range(200):
        macro = []
        for _step in range(rng.randint(0, 5)):
            macro.append(rng.sample(keys, rng.randint(1, 3)))
        text = codec.format_steps(macro)
        assert codec.parse_steps(text) == macro, (macro, text)


def test_numpad_plus_in_chords():
    """KP_Add's pretty name "Num+" contains the chord separator.

    format_chord() keeps "Num+" for a one-key chord but falls back to canonical
    names as soon as the chord has more keys, so both spellings round-trip.
    """
    # Formatting: pretty alone, canonical when mixed.
    assert codec.format_chord(["KP_Add"]) == "Num+"
    assert codec.format_chord(["Control_L", "KP_Add"]) == "Control_L+KP_Add"
    assert codec.format_chord(["KP_Add", "KP_Subtract"]) == (
        "KP_Add+KP_Subtract"
    )
    for chord in (["KP_Add"], ["Control_L", "KP_Add"],
                  ["Control_L", "Shift_L", "KP_Add"], ["KP_Add", "a"]):
        text = codec.format_chord(chord)
        assert codec.parse_steps(text) == [chord], (chord, text)
    # Hand-typed pretty spelling of a mixed chord parses too.
    assert codec.parse_steps("Ctrl+Num+") == [["Control_L", "KP_Add"]]
    assert codec.parse_steps("ctrl+num+") == [["Control_L", "KP_Add"]]
    assert codec.parse_steps("Ctrl + Num+") == [["Control_L", "KP_Add"]]
    assert codec.parse_steps("NUM++a") == [["KP_Add", "a"]]
    assert codec.parse_steps("a+Num+") == [["a", "KP_Add"]]
    assert codec.parse_steps("Num+") == [["KP_Add"]]
    assert codec.parse_steps("Ctrl+Num-") == [["Control_L", "KP_Subtract"]]
    # Every other key paired with KP_Add round-trips, in both orders.
    failures = []
    for key in codec.SCANCODES:
        if key == "KP_Add":
            continue
        for chord in (["KP_Add", key], [key, "KP_Add"]):
            text = codec.format_chord(chord)
            if codec.parse_steps(text) != [chord]:
                failures.append((chord, text, codec.parse_steps(text)))
    assert not failures, failures


def test_numpad_plus_ambiguities_still_raise():
    """Text that cannot be split unambiguously fails cleanly, with a hint path.

    A key name is continued while text-so-far+'+' is a known name, so a key
    typed right after the pretty "Num+" with no separator ("Ctrl+Num+Shift")
    becomes the bogus token "Num+Shift".  Canonical spelling or a separator
    works; this pins the current (intended) limits rather than silently
    corrupting the chord.
    """
    for text in ("Ctrl+Num+Shift", "Num+Shift", "Ctrl+Num+KP_1",
                 "Ctrl+Num+Num-", "Num++", "+Num+"):
        expect_macro_error(codec.parse_steps, text)
    # The documented ways to write it.
    assert codec.parse_steps("Ctrl+KP_Add+Shift") == [
        ["Control_L", "KP_Add", "Shift_L"]
    ]
    assert codec.parse_steps("Num++Shift") == [["KP_Add", "Shift_L"]]
    assert codec.parse_steps("KP_Add+Shift") == [["KP_Add", "Shift_L"]]
    # A space after "Num+" is folded into the chord by the "spaces around +"
    # rule, so this is ambiguous too - a comma keeps the steps apart.
    expect_macro_error(codec.parse_steps, "Num+ b")
    assert codec.parse_steps("Num+, b") == [["KP_Add"], ["b"]]
    assert codec.parse_steps("Num+; b") == [["KP_Add"], ["b"]]
    # A duplicate key is still rejected, even with a pretty numpad-plus.
    assert "twice" in expect_macro_error(codec.parse_chord, "a+a")


# --------------------------------------------------------------------------- #
# GUI helpers (module-level, pure: no window, no display)
# --------------------------------------------------------------------------- #
def _load_gui():
    """Import omen_macro.gui lazily; return (module, error_or_None)."""
    try:
        from omen_macro import gui
    except Exception as exc:  # noqa: BLE001 - reported as an explicit skip
        return None, f"{type(exc).__name__}: {exc}"
    return gui, None


def _gui_with_helpers():
    gui, error = _load_gui()
    assert error is None, f"cannot import omen_macro.gui: {error}"
    missing = [
        name for name in ("format_editor_entry", "parse_editor_entries",
                          "editor_preview")
        if not callable(getattr(gui, name, None))
    ]
    assert not missing, f"omen_macro.gui is missing helper(s): {missing}"
    return gui


def _gui_row_text():
    """The dialog's row-prefill function: editor_row_text() or, once the Lead
    simplifies it away, format_editor_entry() (which the dialog then uses)."""
    gui = _gui_with_helpers()
    row_text = getattr(gui, "editor_row_text", None)
    return row_text if callable(row_text) else gui.format_editor_entry


def test_gui_format_editor_entry():
    gui = _gui_with_helpers()
    assert gui.format_editor_entry([]) == ""
    assert gui.format_editor_entry(["Control_L", "c"]) == "Ctrl+c"
    assert gui.format_editor_entry(["Control_L", "Shift_L", "t"]) == (
        "Ctrl+Shift+t"
    )
    assert gui.format_editor_entry(["KP_Add"]) == "Num+"
    assert gui.format_editor_entry(["KP_Decimal"]) == "Num."
    assert gui.format_editor_entry(["Return"]) == "Enter"
    # A chord that mixes KP_Add spells the canonical names (parseable).
    assert gui.format_editor_entry(["Control_L", "KP_Add"]) == (
        "Control_L+KP_Add"
    )
    # The spelling is canonical, not a copy of whatever the user typed.
    assert gui.format_editor_entry(["Shift_L", "a"]) == "Shift+a"


def test_gui_format_then_parse_every_key():
    """One editor row per key: prefill -> parse gives the chord back."""
    gui = _gui_with_helpers()
    failures = []
    for key in codec.SCANCODES:
        text = gui.format_editor_entry([key])
        try:
            got = gui.parse_editor_entries([text])
        except codec.MacroError as exc:
            failures.append((key, text, f"MacroError: {exc}"))
            continue
        if got != [[key]]:
            failures.append((key, text, got))
    assert not failures, failures


def test_gui_parse_editor_entries_flattens_rows():
    gui = _gui_with_helpers()
    assert gui.parse_editor_entries([]) == []
    assert gui.parse_editor_entries(["", "  ", "\t\n"]) == []
    # Row order is step order; blank rows are placeholders, not steps.
    assert gui.parse_editor_entries(["h", "", "e", "   "]) == [["h"], ["e"]]
    # A row may expand to several steps and stays in place.
    assert gui.parse_editor_entries(["h e", "Ctrl+t"]) == [
        ["h"], ["e"], ["Control_L", "t"]
    ]
    assert gui.parse_editor_entries(['"Hi!"', "v"]) == [
        ["Shift_L", "h"], ["i"], ["Shift_L", "1"], ["v"]
    ]
    assert gui.parse_editor_entries(["Ctrl+Shift+t", "", "Num+"]) == [
        ["Control_L", "Shift_L", "t"], ["KP_Add"]
    ]


def test_gui_parse_editor_entries_error_names_row():
    gui = _gui_with_helpers()
    msg = expect_macro_error(gui.parse_editor_entries, ["h", "", "Bogus"])
    assert msg.startswith("step 3:"), msg
    assert "Bogus" in msg
    msg = expect_macro_error(gui.parse_editor_entries, ["h", "!"])
    assert msg.startswith("step 2:"), msg
    assert "shifted character" in msg
    msg = expect_macro_error(gui.parse_editor_entries, ['"Hi', "h"])
    assert msg.startswith("step 1:"), msg
    assert "unterminated" in msg
    # A row that expands to several steps still reports its own row number.
    msg = expect_macro_error(gui.parse_editor_entries, ["h e", "Ctrl+F13"])
    assert msg.startswith("step 2:"), msg
    assert "F13" in msg
    # Blank rows before the failure do not shift the number the user sees.
    msg = expect_macro_error(gui.parse_editor_entries, ["", "", "a+a"])
    assert msg.startswith("step 3:"), msg


def test_gui_parse_editor_entries_bad_text_is_macro_error():
    """Malformed rows must fail as codec.MacroError, never ValueError/IndexError.

    The dialog only catches MacroError around this call; anything else would
    escape the OK handler (a traceback in the terminal, dialog stuck).
    """
    gui = _gui_with_helpers()
    for row in ("!", "Ctrl+", "+a", "Ctrl++t", "a+a", "F13", "Bogus",
                '"Hi', '"é"', "Ctrl+Num+Shift", "Num+Shift"):
        expect_macro_error(gui.parse_editor_entries, [row])
    # Mixed numpad-plus text is legal now: the first '+' is part of "Num+".
    assert gui.parse_editor_entries(["Ctrl+Num+"]) == [
        ["Control_L", "KP_Add"]
    ]
    assert gui.parse_editor_entries(["Num++a"]) == [["KP_Add", "a"]]
    # Deterministic fuzz: random junk rows must either parse (non-empty chords
    # only) or raise MacroError prefixed with the row number.
    rng = random.Random(7)
    alphabet = "abCTRL+ ,;!\"_é\t\n-."
    for _ in range(400):
        rows = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
            for _ in range(rng.randint(0, 3))
        ]
        try:
            steps = gui.parse_editor_entries(rows)
        except codec.MacroError as exc:
            assert str(exc).startswith("step "), (rows, str(exc))
            continue
        assert all(isinstance(chord, list) and chord for chord in steps), (
            rows, steps
        )


def test_gui_editor_preview_normal():
    gui = _gui_with_helpers()
    steps = [["h"], ["e"], ["Control_L", "c"]]
    text = gui.editor_preview(steps, 20)
    assert "3 steps" in text, text
    assert "254" in text, text                      # the EC's per-slot limit
    payload = len(codec.encode_entry(steps, 20)) - 1  # minus the length byte
    assert f"{payload}/{codec.MAX_ENTRY_PAYLOAD} bytes" in text, text
    assert "Ctrl+c" in text, text                   # the macro, described
    assert "too long" not in text, text
    # Singular, plural and the empty case.
    assert gui.editor_preview([["h"]], 20).startswith("1 step,")
    assert gui.editor_preview([["h"], ["e"]], 20).startswith("2 steps,")
    empty = gui.editor_preview([], 20)
    assert "0 steps" in empty and "clear" in empty.lower(), empty
    # The spacing really is accounted for: 2 one-byte keys + 20ms delay.
    assert "6/254 bytes" in gui.editor_preview([["a"], ["b"]], 20)
    assert "4/254 bytes" in gui.editor_preview([["a"], ["b"]], 0)


def test_gui_editor_preview_matches_encoder():
    gui = _gui_with_helpers()
    rng = random.Random(31337)
    keys = list(codec.SCANCODES)
    for _ in range(100):
        steps = []
        for _step in range(rng.randint(1, 8)):
            size = rng.randint(1, 2)
            chord = rng.sample(keys, size)
            if size > 1 and "KP_Add" in chord:
                continue
            steps.append(chord)
        if not steps:
            continue
        spacing = rng.choice((0, 20, 255, 300))
        try:
            payload = len(codec.encode_entry(steps, spacing)) - 1
        except codec.MacroError:
            continue  # over the limit: covered by the boundary test
        text = gui.editor_preview(steps, spacing)
        assert f"{payload}/{codec.MAX_ENTRY_PAYLOAD} bytes" in text, (
            steps, spacing, text
        )
    # Every canonical key: payload is twice its scan-code length.
    for key in codec.SCANCODES:
        text = gui.editor_preview([[key]], 0)
        want = 2 * len(codec.SCANCODES[key])
        assert f"1 step, {want}/254 bytes" in text, (key, text)


def test_gui_editor_preview_limit_boundary():
    gui = _gui_with_helpers()
    # 127 single-byte keys = 254 payload bytes: exactly at the limit.
    at_limit = [["a"]] * 127
    assert len(codec.encode_entry(at_limit, 0)) - 1 == codec.MAX_ENTRY_PAYLOAD
    text = gui.editor_preview(at_limit, 0)
    assert f"127 steps, {codec.MAX_ENTRY_PAYLOAD}/{codec.MAX_ENTRY_PAYLOAD} " \
           "bytes" in text, text
    assert "too long" not in text, text
    # One step more is 2 bytes over: the preview must say so, and the encoder
    # must agree.
    over = [["a"]] * 128
    expect_macro_error(codec.encode_entry, over, 0)
    text = gui.editor_preview(over, 0)
    assert "128 steps, 256/254 bytes" in text, text
    assert "too long by 2 bytes" in text, text
    # Delays count too: 80 one-byte steps with 20ms spacing = 80*2 + 79*2.
    spaced = [["a"]] * 80
    expect_macro_error(codec.encode_entry, spaced, 20)
    text = gui.editor_preview(spaced, 20)
    assert "318/254 bytes" in text, text
    assert "too long by 64 bytes" in text, text


def test_gui_editor_preview_never_raises():
    gui = _gui_with_helpers()
    # A key the EC cannot replay: described, not raised (the dialog calls this
    # on every keystroke, including while the text is still invalid).
    text = gui.editor_preview([["Bogus"]], 20)
    assert "Bogus" in text, text
    # Odd but in-contract values.
    for steps, spacing in (
        ([], 0),
        ([], 2040),
        ([["Control_L", "KP_Add"]], 255),
        ([["space"]], 255 * 8),
    ):
        assert isinstance(gui.editor_preview(steps, spacing), str)


def test_gui_editor_row_text_every_chord():
    """The dialog's row prefill must always be readable back by the parser.

    This is the stronger property than format_editor_entry() alone: it covers
    chords that mix KP_Add (pretty name "Num+") with other keys, where the
    pretty spelling is ambiguous.
    """
    gui = _gui_with_helpers()
    row_text = _gui_row_text()
    assert row_text([]) == ""
    # Every canonical key, alone.
    failures = []
    for key in codec.SCANCODES:
        text = row_text([key])
        try:
            got = gui.parse_editor_entries([text])
        except codec.MacroError as exc:
            failures.append(([key], text, f"MacroError: {exc}"))
            continue
        if got != [[key]]:
            failures.append(([key], text, got))
    assert not failures, failures
    # Chords of 1..4 keys, drawn deterministically, numpad-plus included.
    rng = random.Random(9090)
    keys = list(codec.SCANCODES)
    for _ in range(400):
        chord = rng.sample(keys, rng.randint(1, 4))
        text = row_text(chord)
        assert gui.parse_editor_entries([text]) == [chord], (chord, text)


def test_gui_numpad_plus_chord_prefills_parseable_text():
    """A saved chord mixing KP_Add must prefill with text OK can read back.

    codec.format_chord() already falls back to canonical names for such a
    chord, so both format_editor_entry() and the dialog's row helper spell it
    "Control_L+KP_Add"; a lone numpad-plus keeps the pretty "Num+".
    (The Lead plans to drop editor_row_text() as redundant and use
    format_editor_entry() directly; until then it must agree with it.)
    """
    gui = _gui_with_helpers()
    row_text = _gui_row_text()
    assert gui.format_editor_entry(["Control_L", "KP_Add"]) == (
        "Control_L+KP_Add"
    )
    assert row_text(["Control_L", "KP_Add"]) == "Control_L+KP_Add"
    assert gui.parse_editor_entries(["Control_L+KP_Add"]) == [
        ["Control_L", "KP_Add"]
    ]
    # The pretty hand spelling parses too (first '+' belongs to "Num+").
    assert gui.parse_editor_entries(["Ctrl+Num+"]) == [
        ["Control_L", "KP_Add"]
    ]
    # A lone numpad-plus keeps the pretty spelling everywhere.
    assert gui.format_editor_entry(["KP_Add"]) == "Num+"
    assert row_text(["KP_Add"]) == "Num+"
    assert gui.parse_editor_entries(["Num+"]) == [["KP_Add"]]
    # Order is preserved, and every valid chord's prefill parses back.
    assert row_text(["KP_Add", "Control_L"]) == "KP_Add+Control_L"
    assert gui.parse_editor_entries(["KP_Add+Control_L"]) == [
        ["KP_Add", "Control_L"]
    ]
    assert row_text(["Control_L", "Shift_L", "t"]) == "Ctrl+Shift+t"
    assert row_text(["KP_Subtract", "a"]) == "Num-+a"
    # The dialog's prefill must agree with format_editor_entry() on valid chords.
    rng = random.Random(4242)
    keys = list(codec.SCANCODES)
    for _ in range(200):
        chord = rng.sample(keys, rng.randint(1, 4))
        assert row_text(chord) == gui.format_editor_entry(chord), chord


def test_gui_dialog_roundtrip_saved_macro():
    """The dialog's prefill -> OK path must not change a saved macro."""
    gui = _gui_with_helpers()
    row_text = _gui_row_text()
    macros = [
        [],
        [["a"]],
        [["Control_L", "c"], ["v"]],
        [["Shift_L", "t"]],
        [["KP_1"], ["KP_Add"]],
        [["h"], ["e"], ["space"]],
        [["Control_L", "Shift_L", "t"]],
        [["Control_L", "KP_Add"], ["v"]],      # mixed numpad-plus chord
        [["KP_Add", "Alt_L"], ["KP_Decimal"]],
    ]
    for macro in macros:
        rows = [row_text(chord) for chord in macro]
        assert gui.parse_editor_entries(rows) == macro, (macro, rows)


if __name__ == "__main__":
    failures = 0
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            ran += 1
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{ran - failures}/{ran} checks passed")
    raise SystemExit(1 if failures else 0)
