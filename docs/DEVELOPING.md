# Developing

## Tests

Run from the repository root. No extra packages and no hardware needed.

```bash
python3 tests/test_codec.py        # encoder and decoder
python3 tests/test_editor.py       # manual editor syntax (text <-> steps)
python3 tests/test_profiles.py     # profile store and migration
python3 tests/test_omen_key.py     # GNOME shortcut helpers
python3 tests/test_auto_switch.py  # focus matcher and rules file
python3 tests/test_boot_diag.py    # missing-interface diagnosis
python3 tests/test_style.py        # no em-dashes in docs or code
python3 -m omen_macro.gui --selftest   # recorder, encoder, profiles, editor
```

Two more tests need a graphical session. Neither shows a window on screen.

```bash
DISPLAY=:0 python3 tests/gui_integration.py   # drives the real editor dialog
DISPLAY=:0 python3 tests/render_ui.py --out /tmp/omen-ui
```

`render_ui.py` paints the real window and every dialog into PNG files
(offscreen, via `Gtk.WidgetPaintable`), runs palette/contrast/layout checks and
writes `report.json`. It is the quickest way to see the effect of a UI change:

```bash
DISPLAY=:0 python3 tests/render_ui.py --only macros,editor
# PNGs and report.json end up in /tmp/omen-ui
```

Without a display it prints `SKIPPED` and exits 0.

## UI style

The window is styled after HP's OMEN Gaming Hub. All colours come from the
`THEME` dict at the top of `omen_macro/gui.py`; the stylesheet is generated
from it, so a palette change is a one-place edit. The app pins an Adwaita dark
base for its own process (skipped for high-contrast themes) so the design does
not change with the desktop theme.

Do not use `opacity` for text in the app: the offscreen renderer paints
opacity nodes incorrectly (the text looks double-struck). Use a colour token
instead.

## Adding a key

The key names and their PS/2 scan codes live in `omen_macro/codec.py`:

* `_PLAIN`: normal keys (scan code without a prefix);
* `_EXTENDED`: keys that need the `0xE0` prefix;
* `ALIASES`: alternative spellings;
* `PRETTY`: names shown in the app.

A key that has no entry there cannot be replayed by the keyboard chip, so the
app will refuse it rather than silently dropping it.

## Table format and internals

See [HOW-IT-WORKS.md](HOW-IT-WORKS.md) for the keyboard table format, the
kernel sysfs interface and the module layout.
