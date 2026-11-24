#!/usr/bin/env python3
"""Integration smoke test for the macro editor dialog (no window is shown).

Unlike tests/test_editor.py this drives the REAL GTK widgets: it builds the
real MacroWindow, replays a recording-shaped macro, opens the real editor,
clicks its real Split/Insert/Delete/Add step/Reset/Cancel/OK buttons and types
into its real Gtk.Entry rows, then checks the in-memory slots and the persist
calls.  Windows are never presented (Gtk.Window.present() is stubbed) so
nothing appears on the desktop, and _persist() is stubbed so the profile file
and the EC are not touched.

Needs a display/session, so it is not part of the plain test list:

    DISPLAY=:0 python3 tests/gui_integration.py     # X11 or Wayland session
    python3 tests/gui_integration.py                # prints SKIPPED, exit 0

Run it after touching the dialog or the row buttons; it covers what the pure
selftest cannot: widget wiring, focus/idle handlers, button callbacks and the
capture-before-rerender data-safety path.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tmp = Path(tempfile.mkdtemp(prefix="omen-editor-it-"))
cfg = tmp / "cfg"
sysfs = tmp / "sysfs"
sysfs.mkdir(parents=True)
(sysfs / "macro_profile").write_bytes(b"")
(sysfs / "macro_status").write_text("profile_write_ret=0\nmode_set_ret=0\napplied=1\n")
os.environ["OMEN_MACRO_ROOT"] = str(cfg)
os.environ["OMEN_MACRO_SYSFS"] = str(sysfs)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from omen_macro import gui  # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")


def walk(widget, out=None):
    out = [] if out is None else out
    out.append(widget)
    child = widget.get_first_child()
    while child is not None:
        walk(child, out)
        child = child.get_next_sibling()
    return out


def by_label(widgets, label, cls=Gtk.Button):
    return [w for w in widgets if isinstance(w, cls) and w.get_label() == label]


def rows_of(dialog):
    """The step rows: Gtk.Box children of the scrolled list."""
    widgets = walk(dialog)
    scroller = next(w for w in widgets if isinstance(w, Gtk.ScrolledWindow))
    steps_box = scroller.get_child()
    if isinstance(steps_box, Gtk.Viewport):  # GTK4 wraps non-scrollable children
        steps_box = steps_box.get_child()
    rows, child = [], steps_box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Box):
            rows.append(child)
        child = child.get_next_sibling()
    return rows


def row_parts(row):
    entries = [w for w in walk(row) if isinstance(w, Gtk.Entry)]
    buttons = {b.get_label(): b for b in walk(row) if isinstance(b, Gtk.Button)}
    return entries[0], buttons


def entry_texts(dialog):
    return [row_parts(r)[0].get_text() for r in rows_of(dialog)]


def preview_text(dialog):
    for w in walk(dialog):
        if isinstance(w, Gtk.Label):
            text = w.get_text() or ""
            if "step" in text and "bytes" in text or text.startswith("0 step"):
                return text
    return ""


app = Gtk.Application(application_id="org.omen.macrotests")
try:
    app.register(None)
except Exception as exc:  # no display / no session bus: nothing to test
    print(f"SKIPPED: cannot open a GTK display ({exc})")
    sys.exit(0)

created = []
RealWindow = Gtk.Window


class Recorder(RealWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        created.append(self)

    def present(self):
        pass  # never map a window on the user's desktop


gui.Gtk.Window = Recorder

win = gui.MacroWindow(app)
persisted = []
win._persist = lambda note="": (persisted.append(note), True)[1]
win._refresh_row = lambda slot: None
win.slots["P1"] = [["h", "e"], ["Control_L", "c"]]

# ---------------------------------------------------------------- open editor
win._open_macro_editor("P1")
dlg = created[-1]
check("dialog title", dlg.get_title(), "Edit P1 macro")
check("rows start from the saved macro", entry_texts(dlg), ["h+e", "Ctrl+c"])
check("split enabled for every multi-key chord",
      [row_parts(r)[1]["Split"].get_sensitive() for r in rows_of(dlg)],
      [True, True])
check("preview counts steps and bytes", "2 steps" in preview_text(dlg), True)

# --------------------------------------------------- fix the mis-detected chord
row_parts(rows_of(dlg)[0])[1]["Split"].emit("clicked")
check("Split turns h+e into two steps", entry_texts(dlg), ["h", "e", "Ctrl+c"])
check("preview follows the split", "3 steps" in preview_text(dlg), True)

# typed but not applied text survives a structural edit
row_parts(rows_of(dlg)[0])[0].set_text("H")
row_parts(rows_of(dlg)[1])[1]["Insert"].emit("clicked")
check("typed text survives Insert", entry_texts(dlg), ["H", "e", "", "Ctrl+c"])
row_parts(rows_of(dlg)[2])[0].set_text("i")
row_parts(rows_of(dlg)[3])[1]["Delete"].emit("clicked")
check("Insert lands after the clicked row, Delete removes its own row",
      entry_texts(dlg), ["H", "e", "i"])
row_parts(rows_of(dlg)[0])[0].set_text("h")  # H means the h key in a step
row_parts(rows_of(dlg)[2])[1]["Delete"].emit("clicked")

# one row may hold several steps
row_parts(rows_of(dlg)[1])[0].set_text("i l l o")
check("multi-step row in the editor", entry_texts(dlg), ["h", "i l l o"])

# ---------------------------------------------------------------- OK persists
by_label(walk(dlg), "OK")[0].emit("clicked")
check("OK wrote the edited macro", win.slots.get("P1"),
      [["h"], ["i"], ["l"], ["l"], ["o"]])
check("OK persisted once", len(persisted), 1)
check("persist note names the macro", persisted[0].startswith("P1 = "), True)
check("dialog closed after OK", dlg.get_visible(), False)

# ------------------------------------------------------- invalid input blocks
created.clear()
win.slots["P1"] = [["a"]]
win._open_macro_editor("P1")
dlg = created[-1]
row_parts(rows_of(dlg)[0])[0].set_text("Bogus")
by_label(walk(dlg), "OK")[0].emit("clicked")
check("bad step keeps the dialog open", dlg.get_visible(), False)  # never presented
errors = [w.get_text() for w in walk(dlg)
          if isinstance(w, Gtk.Label) and "step 1:" in (w.get_text() or "")]
check("bad step is reported with the row number", bool(errors), True)
check("bad step did not change the macro", win.slots.get("P1"), [["a"]])
check("bad step did not persist", len(persisted), 1)


def error_labels(dialog):
    return [w for w in walk(dialog)
            if isinstance(w, Gtk.Label) and "banner-error" in w.get_css_classes()]


# fixing the row clears the stale red banner
row_parts(rows_of(dlg)[0])[0].set_text("a")
check("fixing the row hides the stale error",
      [w.get_visible() for w in error_labels(dlg)], [False])
# ...and a fresh failure shows it again
row_parts(rows_of(dlg)[0])[0].set_text("Bogus again")
by_label(walk(dlg), "OK")[0].emit("clicked")
check("error comes back for the new bad text",
      [w.get_visible() for w in error_labels(dlg)], [True])
by_label(walk(dlg), "Cancel")[0].emit("clicked")

# ------------------------------------------- a failed save is reported in-dialog
created.clear()
win.slots["P5"] = [["a"]]
win._open_macro_editor("P5")
dlg = created[-1]
row_parts(rows_of(dlg)[0])[0].set_text("b")
win._persist = lambda note="": False
by_label(walk(dlg), "OK")[0].emit("clicked")
check("failed save keeps the dialog open",
      len(by_label(walk(dlg), "OK")), 1)
check("failed save rolls the in-memory macro back",
      win.slots.get("P5"), [["a"]])
check("failed save is reported inside the dialog",
      any("could not save" in (w.get_text() or "")
          for w in walk(dlg) if isinstance(w, Gtk.Label)), True)
win._persist = lambda note="": (persisted.append(note), True)[1]
by_label(walk(dlg), "Cancel")[0].emit("clicked")

# ------------------------------------------------------------- quoted text row
created.clear()
win.slots["P2"] = [["KP_1"]]
win._open_macro_editor("P2")
dlg = created[-1]
check("single key keeps the pretty spelling", entry_texts(dlg), ["KP_1"])
row_parts(rows_of(dlg)[0])[0].set_text('"Hi, you"')
ok = by_label(walk(dlg), "OK")[0]
ok.emit("clicked")
check("quoted text expands to steps", win.slots.get("P2"),
      [["Shift_L", "h"], ["i"], ["comma"], ["space"], ["y"], ["o"], ["u"]])

# ------------------------------------------------ clear a macro from the editor
created.clear()
win.slots["P3"] = [["a"], ["b"]]
win._open_macro_editor("P3")
dlg = created[-1]
for _ in range(2):
    row_parts(rows_of(dlg)[0])[1]["Delete"].emit("clicked")
check("deleting every row empties the editor", entry_texts(dlg), [])
by_label(walk(dlg), "OK")[0].emit("clicked")
check("empty editor clears the slot", "P3" in win.slots, False)
check("clear note", persisted[-1].startswith("P3 cleared"), True)

# ---------------------------------------------------------------- Reset/Cancel
created.clear()
win.slots["P4"] = [["x"], ["y"]]
win._open_macro_editor("P4")
dlg = created[-1]
row_parts(rows_of(dlg)[0])[0].set_text("zzz")
by_label(walk(dlg), "Reset")[0].emit("clicked")
check("Reset restores the saved steps", entry_texts(dlg), ["x", "y"])
by_label(walk(dlg), "Cancel")[0].emit("clicked")
check("Cancel leaves the macro unchanged", win.slots.get("P4"), [["x"], ["y"]])
check("Cancel does not persist", len(persisted), 3)

# ------------------------------------------------------------- real MainLoop run
# Pump one main-loop iteration so deferred focus/idle handlers cannot explode.
ctx = __import__("gi.repository", fromlist=["GLib"]).GLib.MainContext.default()
while ctx.pending():
    ctx.iteration(False)

print()
if failures:
    print(f"INTEGRATION FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("INTEGRATION PASSED: all editor checks")
