#!/usr/bin/env python3
"""Offscreen render + visual-QA harness for the OMEN Macro Keys window.

Why this exists
---------------
This machine runs a Wayland session, has no screenshot tool and the GNOME
screenshot D-Bus is access-denied, so the only trustworthy way to look at the
real GTK window is to make GTK paint it into a texture.  That is what this
harness does: the actual ``gui.MacroWindow`` is built, laid out, painted with
``Gtk.WidgetPaintable`` + ``Gsk.CairoRenderer`` and written as PNG.  Nothing is
visible on the desktop and nothing on disk/hardware is touched.

How it works (verified recipe, GTK 4.22 / PyGObject / pycairo)
--------------------------------------------------------------
* ``win.set_opacity(0.01)`` then ``win.present()`` - a window must be mapped or
  ``Gtk.WidgetPaintable`` yields no node, and opacity 0.0 yields no node too.
  0.01 is effectively invisible.  Everything is destroyed right after capture.
* Pump ``GLib.MainContext.default()`` for ~250 ms so layout/CSS settle.
* Snapshot the widget's content child (the window itself paints through the
  0.01 opacity node, which would darken every pixel by 100x) and rasterise the
  render node with ``Gsk.CairoRenderer.render_texture(node, None)``.  Do NOT
  use ``renderer.render()`` with a cairo context: this GTK build raises
  ``KeyError: 'could not find foreign type Region'``.
* Composite the capture over the window background colour resolved from the
  real theme (``window_bg_color``) so the PNG looks like the window on screen.
  The CSD titlebar is painted by the window, not by a child widget, so it is
  not part of the widget capture; dialogs are captured as their own window.

Capture-only CSS adjustment (not a product change)
--------------------------------------------------
Adwaita's ``.dim-label`` uses ``opacity: 0.55``; text painted through an
opacity node comes out double-struck in this offscreen path.  The harness
therefore injects ``.dim-label { opacity: 1.0; }`` (plus neutralisers for any
other selector in ``gui.CSS`` that sets ``opacity``) for the capture only, so
dim text in the PNGs is brighter than on screen.  Contrast is measured from
``gui.THEME`` tokens, never from the (neutralised) pixels.  ``--keep-opacity``
disables the adjustment to reproduce the artefact.

Safety
------
``OMEN_MACRO_ROOT`` / ``OMEN_MACRO_SYSFS`` point at a throw-away temp tree, the
EC dummy ``macro_profile``/``macro_status`` are created there, and every
window's ``_persist()`` is stubbed, so the real profile store, GNOME shortcuts
and the embedded controller are never touched.  The harness never presents an
opaque window and cleans its temp tree up at exit.

Usage
-----
    DISPLAY=:0 python3 tests/render_ui.py                 # /tmp/omen-ui
    DISPLAY=:0 python3 tests/render_ui.py --out DIR --only macros,editor,narrow
    DISPLAY=:0 python3 tests/render_ui.py --out DIR --only dialogs,editor-single
    DISPLAY=:0 python3 tests/render_ui.py --width 1280 --height 900
    DISPLAY=:0 python3 tests/render_ui.py --theme Adwaita:dark   # comparison
    python3 tests/render_ui.py                           # no display: SKIPPED

Scenarios: macros, states (command-bound + empty row), editor dialog (mixed
h+e chord), editor-single (disabled Split), editor-error (inline error),
dialogs (keys / command / new-profile shells), applied (EC pill green),
no-hardware (macro_profile removed: diagnostic banner + EC pill state),
recording, error banner, auto-switch page, advanced expanded, every stack
page, narrow 1024x700.

Automated checks: WCAG table from gui.THEME (body >= 4.5:1, large >= 3.0:1),
dark-palette sampling, panel/background separation, OMEN-red/amber/green
accent pixels, desktop-theme blue leak and off-palette large areas, clipping
(natural vs allocation, window grew), empty/icon-only button labels,
ellipsized labels, sidebar width, window-control visibility (pixel ink),
dead CSS selectors by GTK4 node name, pill/state text, uppercase headers.

Exit status: 0 = all hard checks passed (PNG paths on stdout), 1 = a hard
check failed (details in the report JSON), 0 with "SKIPPED" = no display.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Assigned in main() once the display is known to exist; module-level so the
# pure helpers (widget_checks, scenarios) can use them without re-importing.
Gtk = Gdk = Gsk = GLib = Pango = None

OPACITY = 0.01            # mapped but effectively invisible
SETTLE_SECONDS = 0.28     # how long to pump the main loop after each change
DEFAULT_OUT = "/tmp/omen-ui"

# Capture-only overrides; see "Capture-only CSS adjustment" above.
BASE_CAPTURE_CSS = ".dim-label { opacity: 1.0; }\n"
OPACITY_RULE = re.compile(r"([^{}]+)\{[^{}]*\bopacity\s*:", re.S)


# --------------------------------------------------------------------------- #
# WCAG / colour helpers (pure Python, unit-testable without a display)
# --------------------------------------------------------------------------- #
def parse_color(text):
    """Parse '#rgb', '#rrggbb', 'rgb(...)' or 'rgba(...)' -> (r, g, b, a).

    Components are 0..255 floats and alpha is 0..1.  Returns None when the
    string is not a colour this harness understands.
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) != 6:
            return None
        try:
            return (int(digits[0:2], 16), int(digits[2:4], 16),
                    int(digits[4:6], 16), 1.0)
        except ValueError:
            return None
    match = re.fullmatch(
        r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)",
        value,
    )
    if not match:
        return None
    red, green, blue = (float(match.group(i)) for i in (1, 2, 3))
    alpha = float(match.group(4)) if match.group(4) is not None else 1.0
    return (red, green, blue, max(0.0, min(1.0, alpha)))


def _to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb[:3])


def composite_over(fg, bg):
    """Flatten fg (with alpha) over an opaque bg -> (r, g, b)."""
    alpha = fg[3] if len(fg) > 3 else 1.0
    return tuple(fg[i] * alpha + bg[i] * (1.0 - alpha) for i in range(3))


def relative_luminance(rgb) -> float:
    """WCAG 2.1 relative luminance of an sRGB colour."""
    def channel(value):
        value = max(0.0, min(1.0, value / 255.0))
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(c) for c in rgb[:3])
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(fg, bg) -> float:
    """WCAG contrast ratio between two opaque colours (1.0 .. 21.0)."""
    lighter = max(relative_luminance(fg), relative_luminance(bg))
    darker = min(relative_luminance(fg), relative_luminance(bg))
    return (lighter + 0.05) / (darker + 0.05)


def theme_tokens(gui) -> dict:
    """Resolved design tokens: gui.THEME plus gui._CSS_VARS derived colours."""
    tokens: dict = {}
    derived = getattr(gui, "_CSS_VARS", None)
    if isinstance(derived, dict):
        tokens.update(derived)
    theme = getattr(gui, "THEME", None)
    if isinstance(theme, dict):
        tokens.update(theme)
    return tokens


# CSS subnodes that are not GtkWidget subclasses; a leading token from this
# set cannot be validated by instantiating a widget class.
CSS_INTERNAL_NODES = {
    "window", "headerbar", "windowcontrols", "csd", "solid-csd", "decoration",
    "background", "contents", "title", "subtitle", "slider", "arrow", "check",
    "trough", "text", "image", "label", "button", "row", "list", "stack",
    "frame", "popover", "entry", "switch", "separator", "scrolledwindow",
    "viewport", "expander-widget", "checkbutton", "spinbutton", "combobox",
    "listview", "calendar", "spinner", "progressbar", "menu", "box", "overlay",
    "tooltip", "undershoot", "dnd", "rubberband", "grid", "paned", "notebook",
    "treeview", "columnview", "dropdown", "levelbar", "scale", "scrollbar",
    "searchbar", "sidebar", "infobar", "actionbar", "menubar", "placessidebar",
    "shortcuts", "flowbox", "fixed", "revealer", "handle",
}

# Widget classes whose CSS node names this harness knows how to verify.
CSS_NODE_CLASSES = [
    "Window", "ApplicationWindow", "HeaderBar", "WindowControls", "Box",
    "Label", "Button", "ToggleButton", "Entry", "SpinButton", "ComboBox",
    "ComboBoxText", "Switch", "CheckButton", "Expander", "ListBox",
    "ListBoxRow", "Stack", "ScrolledWindow", "Viewport", "Separator", "Frame",
    "Image", "Popover", "ListView", "Scrollbar", "ProgressBar", "Spinner",
    "Text", "Calendar", "MenuButton", "Overlay", "Paned", "Notebook",
    "TreeView", "ColumnView", "DropDown", "Scale", "LevelBar", "SearchEntry",
    "TextView",
]


def known_css_node_names(gtk) -> set:
    """CSS node names that exist in this GTK build (derived from real classes)."""
    names = set(CSS_INTERNAL_NODES)
    for class_name in CSS_NODE_CLASSES:
        widget_class = getattr(gtk, class_name, None)
        if widget_class is None:
            continue
        try:
            instance = widget_class()
            names.add(instance.get_css_name())
        except Exception:
            continue
    return names


def css_selector_checks(gui, gtk) -> list[dict]:
    """WARN about stylesheet selectors whose element name GTK never matches."""
    checks: list[dict] = []
    css = getattr(gui, "CSS", "")
    if not isinstance(css, str) or not css:
        return [{"name": "css:selectors", "status": "WARN",
                 "detail": "gui.CSS not a string; dead-selector check skipped"}]
    known = known_css_node_names(gtk)
    dead, internal = [], []
    for match in re.finditer(r"(?m)^\s*([^\n{}/*][^{}\n]*?)\s*\{", css):
        for selector in match.group(1).split(","):
            selector = " ".join(selector.split())
            if not selector or selector.startswith("@"):
                continue
            token = re.match(r"^([A-Za-z][\w-]*)", selector)
            if not token:
                continue
            name = token.group(1)
            if name in known:
                continue
            # A selector that starts at a GTK-internal subnode (e.g. a
            # textview child) is valid styling of an internal node; only a
            # leading token that is not a node at all is certainly dead.
            if name in CSS_INTERNAL_NODES:
                internal.append(selector)
            else:
                dead.append(selector)
    if dead:
        checks.append({
            "name": "css:selectors",
            "status": "WARN",
            "detail": (f"{len(dead)} selector(s) never match a GTK CSS node "
                       f"(dead rules): {sorted(set(dead))[:12]}"),
        })
    else:
        checks.append({
            "name": "css:selectors", "status": "PASS",
            "detail": "every element selector matches a GTK CSS node",
        })
    if internal:
        checks.append({
            "name": "css:internal-nodes", "status": "PASS",
            "detail": (f"informational: {len(internal)} selector(s) target "
                       f"GTK-internal subnodes: {sorted(set(internal))[:12]}"),
        })
    return checks


def contrast_report(gui) -> list[dict]:
    """WCAG table for the pairs the UI actually paints, from gui.THEME.

    Body text must reach 4.5:1 and large text (>=18.7px bold / >=24px) 3:1.
    Pairs whose token is missing are reported as WARN ("unmeasured") instead of
    failing, so the harness also works against the pre-restyle UI.
    """
    tokens = theme_tokens(gui)
    has_theme = isinstance(getattr(gui, "THEME", None), dict) and bool(
        getattr(gui, "THEME", None))

    def token(name, fallback=None):
        value = tokens.get(name) or fallback
        return parse_color(value) if isinstance(value, str) else None

    bg = token("bg")
    bg_raised = token("bg_raised", "bg")
    surface = token("surface", "bg_raised")
    surface_active = token("surface_active", "surface")
    text = token("text")
    text_dim = token("text_dim", "text")
    text_faint = token("text_faint", "text_dim")
    omen = token("omen")
    omen_hot = token("omen_hot", "omen")
    amber = token("amber")
    green = token("green")
    error = token("error")
    border = token("border")
    border_strong = token("border_strong", "border")

    def banner_bg(colour, fallback_alpha):
        if colour is None or bg is None:
            return None
        tinted = (colour[0], colour[1], colour[2], fallback_alpha)
        return composite_over(tinted, bg)

    def add(label, fg, background, required, kind, note=""):
        entry = {"pair": label, "kind": kind, "required": required, "note": note}
        if fg is None or background is None:
            entry.update(ratio=None, status="WARN", detail="token missing")
            return
        alpha = background[3] if len(background) > 3 else 1.0
        background_rgb = (composite_over(background, bg) if alpha < 1.0
                          else background)
        ratio = contrast_ratio(fg, background_rgb)
        entry.update(
            fg=_to_hex(fg),
            bg=_to_hex(background_rgb),
            ratio=round(ratio, 2),
        )
        if ratio >= required:
            entry["status"] = "PASS"
        elif kind in ("body", "large"):
            entry["status"] = "FAIL"
            entry["detail"] = f"{ratio:.2f} < {required}"
        else:
            entry["status"] = "WARN"
            entry["detail"] = (f"{ratio:.2f} < {required} "
                               "(non-text/disabled, advisory)")
        rows.append(entry)

    rows: list[dict] = []
    add("body text on chassis", text, bg, 4.5, "body")
    add("body text on card", text, surface, 4.5, "body")
    add("secondary text on chassis", text_dim, bg, 4.5, "body",
        "12-13px hints/labels: .text-dim/.hint/.card-title")
    add("secondary text on card", text_dim, surface, 4.5, "body")
    add("nav label on sidebar", text_dim, bg_raised, 4.5, "body",
        "11px bold nav items")
    add("selected nav label", text, surface_active, 4.5, "body")
    add("button label on brand red", (255, 255, 255, 1.0), omen, 4.5, "body",
        "Stop/Save 11-12px bold")
    add("record outline label", omen_hot, bg, 4.5, "body",
        ".record label (.record rules)")
    add("ghost button label on card", text_dim, surface, 4.5, "body")
    add("error banner text", text, banner_bg(error, 0.14), 4.5, "body")
    add("warning banner text", text, banner_bg(amber, 0.12), 4.5, "body")
    add("ok pill text", green, banner_bg(green, 0.10), 4.5, "body",
        "9px bold state pill")
    add("warn pill text", amber, banner_bg(amber, 0.12), 4.5, "body",
        "9px bold state pill")
    add("brand mark (large)", omen_hot, bg, 3.0, "large", "20px brand mark")
    add("page title (large)", text, bg, 3.0, "large", "19px bold page title")
    add("focus ring on chassis", token("focus", "omen"), bg, 3.0, "ui",
        "keyboard focus outline")
    add("mono status text on raised bar", text_dim, bg_raised, 4.5, "body")
    add("recording summary on tint", text_dim,
        (omen[0], omen[1], omen[2], 0.16) if omen else None, 4.5, "body",
        "recording row (omen tint over chassis)")
    add("input border on chassis", text_faint, bg, 3.0, "ui",
        "idle entry/spin/combo/check border (WCAG 1.4.11)")
    add("input border on card", text_faint, surface, 3.0, "ui")
    add("card border on chassis", border, bg, 3.0, "decorative",
        "card edge, adjacent surface also differs")
    add("disabled button text", text_faint, bg, 3.0, "exempt",
        "disabled text is exempt from WCAG 1.4.3")
    add("faint icons on chassis", text_faint, bg, 3.0, "decorative")
    if not has_theme:
        for row in rows:
            if row["status"] == "FAIL":
                row["status"] = "WARN"
                row["detail"] = "advisory: gui.THEME missing, used theme defaults"
    return rows


# --------------------------------------------------------------------------- #
# PNG analysis (palette, contrast by sampling, accent detection)
# --------------------------------------------------------------------------- #
def _cairo():
    try:
        import cairo  # noqa: PLC0415 - optional dependency, reported when absent
    except Exception:  # pragma: no cover - pycairo is installed on this host
        return None
    return cairo


def analyze_png(path: str) -> dict | None:
    """Statistical view of one capture: palette, luminance, accent pixels."""
    cairo = _cairo()
    if cairo is None:
        return None
    try:
        surface = cairo.ImageSurface.create_from_png(path)
    except Exception as exc:
        return {"error": f"cannot read PNG: {exc}"}
    width, height = surface.get_width(), surface.get_height()
    data = surface.get_data()
    stride = surface.get_stride()

    def pixel(x, y):
        offset = y * stride + x * 4
        blue, green, red, alpha = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
        return (red, green, blue, alpha)

    step = max(1, min(width, height) // 240)
    from collections import Counter

    colours = Counter()
    opaque = 0
    total = 0
    luminances = []
    accent = {"omen": 0, "amber": 0, "green": 0, "blue": 0}
    for y in range(0, height, step):
        for x in range(0, width, step):
            red, green, blue, alpha = pixel(x, y)
            total += 1
            if alpha < 200:
                continue
            opaque += 1
            colours[(red, green, blue)] += 1
            luminances.append(relative_luminance((red, green, blue)))
            if red > 90 and red > 1.7 * green and red > 1.7 * blue:
                accent["omen"] += 1
            if red > 140 and 90 < green < 230 and blue < 110 and red > 1.2 * green:
                accent["amber"] += 1
            if green > 110 and green > 1.25 * red and green > 1.25 * blue:
                accent["green"] += 1
            if blue > 90 and blue > 1.45 * red and blue > 1.25 * green:
                accent["blue"] += 1

    border_pixels = Counter()
    for x in range(2, max(3, width - 2), max(1, width // 200)):
        for y in (2, 3, height - 3, height - 4):
            if 0 <= y < height:
                red, green, blue, alpha = pixel(x, y)
                if alpha >= 200:
                    border_pixels[(red, green, blue)] += 1
    for y in range(2, max(3, height - 2), max(1, height // 200)):
        for x in (2, 3, width - 3, width - 4):
            if 0 <= x < width:
                red, green, blue, alpha = pixel(x, y)
                if alpha >= 200:
                    border_pixels[(red, green, blue)] += 1

    def hexify(colour):
        return _to_hex(colour)

    bg_pixel = border_pixels.most_common(1)[0][0] if border_pixels else (
        colours.most_common(1)[0][0] if colours else (0, 0, 0))
    ordered = luminances or [0.0]
    ordered.sort()
    brightest = ordered[int(len(ordered) * 0.995)] if ordered else 0.0
    top = [
        {"color": hexify(colour), "share": round(count / max(1, opaque), 4)}
        for colour, count in colours.most_common(6)
    ]
    distinct = len(colours)
    dark_share = sum(1 for value in luminances if value < 0.05) / max(1, opaque)
    return {
        "image_size": [width, height],
        "opaque_share": round(opaque / max(1, total), 3),
        "distinct_colors": distinct,
        "background": hexify(bg_pixel),
        "background_luminance": round(relative_luminance(bg_pixel), 4),
        "mean_luminance": round(sum(luminances) / max(1, len(luminances)), 4),
        "brightest_1pct_luminance": round(brightest, 4),
        "max_luminance": round(ordered[-1] if ordered else 0.0, 4),
        "dark_pixel_share": round(dark_share, 3),
        "top_colors": top,
        "accent_pixels": {key: round(value / max(1, opaque), 4) for key, value in accent.items()},
        "blank": opaque == 0 or distinct <= 2,
    }


def composite_capture(texture_png: str, out_png: str, background) -> tuple[int, int]:
    """Flatten a widget capture over the window background colour."""
    cairo = _cairo()
    if cairo is None:
        shutil.copyfile(texture_png, out_png)
        return (0, 0)
    content = cairo.ImageSurface.create_from_png(texture_png)
    width, height = content.get_width(), content.get_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(*(component / 255.0 for component in background[:3]))
    context.paint()
    context.set_source_surface(content, 0, 0)
    context.paint()
    surface.write_to_png(out_png)
    return (width, height)


def region_ink(path: str, bounds, background: str | None = None,
               tolerance: int = 12) -> int:
    """Number of pixels inside bounds that differ from the background.

    Used to prove a widget actually painted something: an all-background
    rectangle means the control is invisible even though it exists in the
    widget tree (e.g. a theme asset removed by an app stylesheet).
    """
    cairo = _cairo()
    if cairo is None or not bounds:
        return -1
    try:
        surface = cairo.ImageSurface.create_from_png(path)
    except Exception:
        return -1
    data = surface.get_data()
    stride = surface.get_stride()
    width, height = surface.get_width(), surface.get_height()
    bg = parse_color(background or "")
    bx, by, bw, bh = (int(round(value)) for value in bounds)
    ink = 0
    for y in range(max(0, by), min(height, by + bh)):
        for x in range(max(0, bx), min(width, bx + bw)):
            offset = y * stride + x * 4
            if data[offset + 3] < 200:
                continue
            red, green, blue = data[offset + 2], data[offset + 1], data[offset]
            if bg is None:
                ink += 1
                continue
            if (abs(red - bg[0]) > tolerance
                    or abs(green - bg[1]) > tolerance
                    or abs(blue - bg[2]) > tolerance):
                ink += 1
    return ink


def near_palette_colour(colour, tokens: dict) -> bool:
    """Whether a rendered colour is explained by a THEME token or a tint of it."""
    bg = parse_color(tokens.get("bg", "")) or (11, 11, 14, 1.0)
    for value in tokens.values():
        token = parse_color(value) if isinstance(value, str) else None
        if token is None:
            continue
        flat = composite_over(token, bg) if token[3] < 1.0 else token
        if max(abs(colour[i] - flat[i]) for i in range(3)) <= 14:
            return True
        # translucent tints (banners, pills, recording row) are blends of a
        # token over the chassis; accept any alpha the stylesheet uses.
        for alpha in (0.10, 0.12, 0.14, 0.16, 0.20, 0.22):
            tinted = composite_over((token[0], token[1], token[2], alpha), bg)
            if max(abs(colour[i] - tinted[i]) for i in range(3)) <= 10:
                return True
    return False


# --------------------------------------------------------------------------- #
# GTK helpers
# --------------------------------------------------------------------------- #
def iter_widgets(root, include_self: bool = True):
    """Depth-first walk over a GTK widget tree."""
    if include_self:
        yield root
    child = root.get_first_child()
    while child is not None:
        yield from iter_widgets(child)
        child = child.get_next_sibling()


def widget_text(widget) -> str:
    try:
        if hasattr(widget, "get_text"):
            return widget.get_text() or ""
        if hasattr(widget, "get_label"):
            return widget.get_label() or ""
    except Exception:
        return ""
    return ""


def widget_css(widget) -> list[str]:
    try:
        return list(widget.get_css_classes())
    except Exception:
        return []


# Button internals we must not judge: the combobox/spinbutton implementations
# use icon-only buttons of their own, and popovers are not part of the page.
# Note: PyGObject exposes the GType name without the Gtk prefix
# ("Scrollbar", "ComboBoxText"), so compare both spellings.
SKIPPED_BUTTON_ANCESTORS = {
    "ComboBox", "ComboBoxText", "SpinButton", "Scrollbar", "Popover",
    "MenuButton", "DropDown", "TreeView", "Entry", "SearchEntry",
    "ColorButton", "FontButton", "FileChooserButton", "LinkButton",
    "WindowControls", "WindowHandle", "FileChooserWidget",
}


def _has_skipped_ancestor(widget) -> bool:
    parent = widget.get_parent()
    while parent is not None:
        name = type(parent).__name__
        if name in SKIPPED_BUTTON_ANCESTORS or (
                name.startswith("Gtk")
                and name[3:] in SKIPPED_BUTTON_ANCESTORS):
            return True
        parent = parent.get_parent()
    return False


def button_text(button) -> str:
    """Visible text of a button: its label or a descendant label (composite)."""
    try:
        label = button.get_label()
    except Exception:
        label = None
    if label and label.strip():
        return label.strip()
    for widget in iter_widgets(button):
        text = widget_text(widget)
        if text.strip():
            return text.strip()
    return ""


def widget_checks(win, requested_width: int, requested_height: int,
                  extra_roots=()) -> dict:
    """Automated widget-tree checks: labels, clipping, empty buttons, states.

    ``extra_roots`` are additional trees that belong to the same window but are
    not reachable from its content child - in GTK4 a window's titlebar
    (``set_titlebar``) is a separate widget, so the headerbar is passed here.
    """
    result = {
        "window_size": [win.get_width(), win.get_height()],
        "requested_size": [requested_width, requested_height],
        "grew_beyond_request": win.get_width() > requested_width + 1,
        "empty_buttons": [],
        "buttons": [],
        "ellipsized_labels": [],
        "clipped_labels": [],
        "oversized_widgets": [],
        "pills": [],
        "banners": [],
        "recording_rows": 0,
        "visible_focus_widgets": [],
        "status_text": "",
        "nav_items": [],
        "window_controls": [],
        "sidebar_width": None,
        "cards": 0,
        "uppercase_violations": [],
        "tree": [],
    }
    content_width = max(1, win.get_width())
    roots = [win] + [root for root in extra_roots if root is not None]
    for widget in (item for root in roots for item in iter_widgets(root)):
        try:
            if not widget.get_mapped():
                continue
        except Exception:
            continue
        kind = type(widget).__name__
        css = widget_css(widget)
        allocation = (widget.get_width(), widget.get_height())

        if isinstance(widget, Gtk.Button) and not _has_skipped_ancestor(widget):
            text = button_text(widget)
            tooltip = widget.get_tooltip_text() or ""
            record = {
                "text": text,
                "tooltip": tooltip,
                "css": css,
                "sensitive": widget.get_sensitive(),
                "size": list(allocation),
            }
            result["buttons"].append(record)
            if not text:
                if tooltip.strip():
                    result["empty_buttons"].append({**record, "severity": "warn"})
                else:
                    result["empty_buttons"].append({**record, "severity": "fail"})

        if isinstance(widget, Gtk.Label):
            text = widget.get_text() or ""
            if text.strip():
                layout = widget.get_layout()
                if layout is not None:
                    if layout.is_ellipsized():
                        result["ellipsized_labels"].append(
                            {"text": text[:100], "css": css,
                             "size": list(allocation)})
                    elif not widget.get_wrap():
                        natural_width = layout.get_pixel_size()[0]
                        if natural_width > allocation[0] + 2:
                            result["clipped_labels"].append(
                                {"text": text[:100], "css": css,
                                 "natural_width": natural_width,
                                 "allocated_width": allocation[0]})
                if "pill" in css:
                    result["pills"].append({"text": text, "css": css})
                if "banner" in css:
                    lines = 0
                    if layout is not None:
                        try:
                            lines = layout.get_line_count()
                        except Exception:
                            lines = 0
                    result["banners"].append(
                        {"text": text[:160], "css": css,
                         "visible": widget.get_visible(),
                         "lines": lines,
                         "size": list(allocation)})
                if "status" in css:
                    result["status_text"] = text[:400]

        if isinstance(widget, Gtk.ListBoxRow):
            if "recording" in css:
                result["recording_rows"] += 1

        if "card" in css and not isinstance(widget, Gtk.Label):
            result["cards"] += 1

        if isinstance(widget, Gtk.Button) and {"minimize", "maximize", "close"} & set(css):
            try:
                found, rect = widget.compute_bounds(win)
                bounds = ([rect.get_x(), rect.get_y(),
                           rect.get_width(), rect.get_height()] if found else None)
            except Exception:
                bounds = None
            local_bounds = None
            ancestor = widget.get_parent()
            while ancestor is not None and ancestor is not win:
                if "titlebar" in widget_css(ancestor):
                    try:
                        found, local = widget.compute_bounds(ancestor)
                        local_bounds = ([local.get_x(), local.get_y(),
                                         local.get_width(), local.get_height()]
                                        if found else None)
                    except Exception:
                        local_bounds = None
                    break
                ancestor = ancestor.get_parent()
            result["window_controls"].append({
                "name": next(name for name in ("minimize", "maximize", "close")
                             if name in css),
                "css": css,
                "bounds": bounds,
                "local_bounds": local_bounds,
                "tooltip": widget.get_tooltip_text() or "",
            })

        if "sidebar" in css and result["sidebar_width"] is None:
            result["sidebar_width"] = widget.get_width()

        if isinstance(widget, Gtk.ToggleButton) and "nav-item" in css:
            result["nav_items"].append({
                "text": button_text(widget),
                "active": widget.get_active(),
                "css": css,
            })

        if isinstance(widget, Gtk.Label) and text.strip():
            header_classes = {"eyebrow", "card-title", "card-tail",
                              "page-title", "brand-sub", "device-text",
                              "brand-omen", "side-device", "dialog-heading"}
            if header_classes.intersection(css):
                allowed = text.upper() == text
                if not allowed:
                    result["uppercase_violations"].append(
                        {"text": text[:80], "css": css})

        try:
            if widget.has_visible_focus():
                result["visible_focus_widgets"].append(
                    {"type": kind, "css": css, "text": widget_text(widget)[:60]})
        except Exception:
            pass

        try:
            if isinstance(widget, Gtk.Image):
                minimum = 0
            else:
                minimum = widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            if minimum > content_width + 1:
                result["oversized_widgets"].append(
                    {"type": kind, "css": css,
                     "text": widget_text(widget)[:80],
                     "minimum_width": minimum,
                     "window_width": content_width})
        except Exception:
            pass

        if len(result["tree"]) < 260 and (css or widget_text(widget)
                                          or isinstance(widget, (Gtk.Button, Gtk.ListBoxRow))):
            entry = {"type": kind, "css": css,
                     "size": list(allocation)}
            text = widget_text(widget)
            if text:
                entry["text"] = text[:100]
            result["tree"].append(entry)
    return result


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class Harness:
    """Builds, settles, captures and destroys the real GUI windows."""

    def __init__(self, out_dir: Path, gui, app, gtk, gdk, gsk, glib, options):
        # Publish the toolkit modules for the module-level helpers.
        global Gtk, Gdk, Gsk, GLib
        Gtk, Gdk, Gsk, GLib = gtk, gdk, gsk, glib
        self.out = out_dir
        self.gui = gui
        self.app = app
        self.Gtk = gtk
        self.Gdk = gdk
        self.Gsk = gsk
        self.GLib = glib
        self.options = options
        self.renderer = gsk.CairoRenderer.new()
        self.renderer.realize(None)
        self.windows: list = []
        self.dialogs: list = []
        self.temp_pngs: list[str] = []
        self.theme_name = ""
        self.css_installed = []
        self.dialog_recorder = None

    # -- setup -------------------------------------------------------------
    def install_css(self) -> None:
        """Install the app stylesheet, exactly like gui.MacroApp would."""
        display = self.Gdk.Display.get_default()
        installers = []
        if hasattr(self.gui, "install_css"):
            installers.append(("gui.install_css", getattr(self.gui, "install_css")))
        if hasattr(self.gui, "MacroApp") and hasattr(self.gui.MacroApp, "_install_css"):
            installers.append(("gui.MacroApp._install_css",
                               getattr(self.gui.MacroApp, "_install_css")))
        installed = False
        for label, installer in installers:
            try:
                installer(self.app) if label.startswith("gui.MacroApp") else installer()
                installed = True
                self.css_installed.append(label)
                break
            except Exception as exc:
                self.css_installed.append(f"{label}: {exc}")
        if not installed:
            css = getattr(self.gui, "CSS", "")
            if css and display is not None:
                provider = self.Gtk.CssProvider()
                provider.load_from_string(css)
                self.Gtk.StyleContext.add_provider_for_display(
                    display, provider,
                    self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                self.css_installed.append("gui.CSS via provider")
        if not self.options.keep_opacity and display is not None:
            provider = self.Gtk.CssProvider()
            provider.load_from_string(self._capture_css())
            self.Gtk.StyleContext.add_provider_for_display(
                display, provider,
                self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            self.css_installed.append("capture-only dim-label opacity neutraliser")
        settings = self.Gtk.Settings.get_default()
        if settings is not None:
            try:
                self.theme_name = settings.get_property("gtk-theme-name") or ""
            except Exception:
                self.theme_name = ""

    def _capture_css(self) -> str:
        rules = [BASE_CAPTURE_CSS]
        css = getattr(self.gui, "CSS", "")
        if isinstance(css, str) and css:
            for match in OPACITY_RULE.finditer(css):
                selector = " ".join(match.group(1).split())
                if not selector or selector.startswith("@"):
                    continue
                if "opacity" in selector:
                    continue
                rules.append(f"{selector} {{ opacity: 1.0; }}\n")
        return "".join(rules)

    def install_dialog_recorder(self) -> None:
        """Patch gui.Gtk.Window so dialogs are recorded and presented invisible."""
        gtk = self.Gtk
        dialogs = self.dialogs

        class RenderWindow(gtk.Window):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                dialogs.append(self)

            def present(self):
                self.set_opacity(OPACITY)
                super().present()

        self.gui.Gtk.Window = RenderWindow
        self.dialog_recorder = RenderWindow

    def remap(self, window) -> None:
        """Hide and re-present a window so GTK repaints its (invalidated) tree.

        After a label text change GTK invalidates the widget's cached paint
        node; on this transparent, never-composited window the node can stay
        NULL until the toplevel is mapped again.  The window stays at opacity
        0.01 throughout.  A recording in progress is briefly detached so the
        window's focus-loss handler cannot add its "focus left the window"
        banner during the remap.
        """
        saved_recording = getattr(window, "recording", None)
        if saved_recording is not None:
            try:
                window.recording = None
            except Exception:
                saved_recording = None
        try:
            window.set_visible(False)
            self.pump(0.06)
            window.present()
            self.pump(0.22)
            self.wait_mapped(window)
        finally:
            if saved_recording is not None:
                try:
                    window.recording = saved_recording
                except Exception:
                    pass

    def wait_mapped(self, window, timeout: float = 2.5) -> bool:
        """Pump until the toplevel and its content child are mapped.

        Pinning a toplevel size on a multi-monitor Wayland session is an async
        handshake; under load the resize can still be in flight after the
        normal settle time, and painting an unmapped child yields no node.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            child = None
            try:
                child = window.get_child()
            except Exception:
                child = None
            if (window.get_mapped() and child is not None
                    and child.get_mapped()):
                self.pump(0.12)
                return True
            self.pump(0.05)
        return False

    def pump(self, seconds: float | None = None) -> None:
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + (SETTLE_SECONDS if seconds is None else seconds)
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.008)

    # -- windows -----------------------------------------------------------
    def new_window(self, width: int, height: int):
        win = self.gui.MacroWindow(self.app)
        win._persist = lambda note="": True       # never touch profile/EC
        win.set_default_size(width, height)
        # Capture-only: a stack crossfade would capture a half-faded page.
        for stack in iter_widgets(win):
            if isinstance(stack, self.Gtk.Stack):
                stack.set_transition_type(self.Gtk.StackTransitionType.NONE)
                stack.set_transition_duration(0)
        win.set_opacity(OPACITY)
        win.present()
        self.windows.append(win)
        self._last_window = win
        self.pump()
        # A portrait/multi-monitor compositor clamps Gtk.Window.set_default_size
        # to the monitor width (this desktop has a 1080px-wide portrait
        # screen), which would make the capture size non-deterministic.  The
        # size request pins the toplevel to the requested logical size.
        forced = False
        if (win.get_width(), win.get_height()) != (width, height):
            # A portrait/multi-monitor compositor clamps set_default_size to
            # the monitor width; pin the toplevel so PNG sizes stay stable.
            win.set_size_request(width, height)
            self.pump()
            forced = True
        self.wait_mapped(win)
        win._render_requested_size = [width, height]
        win._render_size_forced = forced
        return win

    def default_profile(self, win) -> None:
        """Stock profile view: P1-P5 numpad, P6 the repo's text-macro fixture."""
        # The fixture is tests/gui_integration.py's '"Hi, you"' expansion: a
        # hand-edited text macro long enough to exercise the ellipsised summary.
        win.slots["P6"] = [
            ["Shift_L", "h"], ["i"], ["comma"], ["space"],
            ["y"], ["o"], ["u"],
        ]
        win._refresh_all()

    def _css_window_background(self):
        """Background colour the app CSS paints on `window`, if declared.

        The theme's `window_bg_color` named colour is not updated by an
        app-level ``window { background-color: ... }`` rule, so the stylesheet
        is the authoritative source for what the real window looks like.
        """
        if getattr(self, "_css_bg", False) is not False:
            return self._css_bg
        self._css_bg = None
        css = getattr(self.gui, "CSS", "") or ""
        if isinstance(css, str) and css:
            for match in re.finditer(r"(?:^|\})\s*window\s*\{([^}]*)\}", css, re.S):
                colour = re.search(r"background-color\s*:\s*([^;]+);", match.group(1))
                if colour:
                    parsed = parse_color(colour.group(1))
                    if parsed is not None:
                        self._css_bg = parsed[:3]
                        break
        return self._css_bg

    def window_background(self, window) -> tuple:
        css_background = self._css_window_background()
        if css_background is not None:
            return css_background
        tokens = theme_tokens(self.gui)
        token_background = parse_color(tokens.get("bg", ""))
        if token_background is not None:
            return token_background[:3]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                context = window.get_style_context()
                for name in ("window_bg_color", "theme_bg_color"):
                    found, colour = context.lookup_color(name)
                    if found and colour is not None:
                        return (colour.red * 255, colour.green * 255,
                                colour.blue * 255)
        except Exception:
            pass
        return (32, 32, 32)

    def _widget_png(self, widget) -> tuple[str, int, int]:
        """Snapshot a widget to a temp PNG; returns (path, width, height)."""
        width, height = widget.get_width(), widget.get_height()
        paintable = self.Gtk.WidgetPaintable.new(widget)
        snapshot = self.Gtk.Snapshot()
        paintable.snapshot(snapshot, float(width), float(height))
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError(f"{type(widget).__name__}: no render node "
                               "(widget not mapped?)")
        texture = self.renderer.render_texture(node, None)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".capture-", suffix=".png", dir=str(self.out))
        os.close(temp_fd)
        self.temp_pngs.append(temp_path)
        texture.save_to_png(temp_path)
        return temp_path, texture.get_width(), texture.get_height()

    def _compose_window(self, window, name: str, parts: list) -> dict:
        """Paint titlebar + content at their real window coordinates."""
        background = self.window_background(window)
        images = []
        for attempt in (0, 1):
            if attempt:
                self.remap(window)
            images = []
            failure = None
            for label, widget in parts:
                if widget is None:
                    continue
                try:
                    if not widget.get_mapped():
                        self.wait_mapped(window, timeout=1.2)
                    path, width, height = self._widget_png(widget)
                except RuntimeError as exc:
                    failure = (label, exc)
                    break
                try:
                    found, rect = widget.compute_bounds(window)
                    x = rect.get_x() if found else 0.0
                    y = rect.get_y() if found else 0.0
                except Exception:
                    x = y = 0.0
                images.append({"part": label, "path": path, "width": width,
                               "height": height, "x": x, "y": y})
            if failure is None:
                break
            if attempt:
                return {"error": str(failure[1]), "part": failure[0]}
        if not images:
            return {"error": "no capturable widget (window not mapped?)"}
        width = window.get_width()
        height = window.get_height()
        cairo = _cairo()
        final_path = self.out / f"{name}.png"
        if cairo is None:
            shutil.copyfile(images[0]["path"], str(final_path))
        else:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            context = cairo.Context(surface)
            context.set_source_rgb(*(component / 255.0 for component in background[:3]))
            context.paint()
            for image in images:
                content = cairo.ImageSurface.create_from_png(image["path"])
                context.set_source_surface(content, image["x"], image["y"])
                context.paint()
            surface.write_to_png(str(final_path))
        for image in images:
            try:
                os.remove(image["path"])
                self.temp_pngs.remove(image["path"])
            except (OSError, ValueError):
                pass
        return {
            "path": str(final_path),
            "image_size": [width, height],
            "parts": [
                {"part": image["part"],
                 "widget_size": [image["width"], image["height"]],
                 "position": [round(image["x"], 1), round(image["y"], 1)]}
                for image in images
            ],
            "window_size": [window.get_width(), window.get_height()],
            "requested_size": getattr(window, "_render_requested_size",
                                      [window.get_width(), window.get_height()]),
            "size_forced": bool(getattr(window, "_render_size_forced", False)),
            "opacity": window.get_opacity(),
            "background": _to_hex(background),
        }

    def checks(self, root, requested_width: int, requested_height: int) -> dict:
        """widget_checks() plus the root window's separate titlebar tree."""
        titlebar = None
        try:
            titlebar = root.get_titlebar()
        except Exception:
            titlebar = None
        if titlebar is not None:
            if not titlebar.get_mapped():
                titlebar = None
            else:
                # GTK4 usually makes the titlebar reachable from the window's
                # first child (the window handle); only add it when it is not.
                parent = titlebar.get_parent()
                reachable = False
                while parent is not None and parent is not root:
                    parent = parent.get_parent()
                reachable = parent is root
                if reachable:
                    titlebar = None
        return widget_checks(root, requested_width, requested_height,
                             extra_roots=(titlebar,))

    def window_parts(self, win) -> list:
        parts = []
        titlebar = win.get_titlebar()
        if titlebar is not None:
            parts.append(("titlebar", titlebar))
        child = win.get_child()
        parts.append(("content", child if child is not None else win))
        return parts

    def capture_window(self, win, name: str) -> dict:
        result = self._compose_window(win, name, self.window_parts(win))
        result["css_classes"] = widget_css(win)
        return result

    def capture_dialog(self, dialog, name: str) -> dict:
        self.wait_mapped(dialog)
        result = self._compose_window(dialog, name, self.window_parts(dialog))
        result["css_classes"] = widget_css(dialog)
        return result

    def close_windows(self) -> None:
        for dialog in self.dialogs:
            try:
                dialog.destroy()
            except Exception:
                pass
        self.dialogs.clear()
        for win in self.windows:
            try:
                win.destroy()
            except Exception:
                pass
        self.windows.clear()
        self.pump(0.06)

    # -- page/stack/scroll helpers ----------------------------------------
    def stacks(self, win) -> list:
        return [widget for widget in iter_widgets(win)
                if isinstance(widget, self.Gtk.Stack)]

    def stack_pages(self, win) -> list[dict]:
        pages: list[dict] = []
        for stack in self.stacks(win):
            try:
                if not stack.get_mapped():
                    continue
                for page in stack.get_pages():
                    pages.append({
                        "stack": stack,
                        "name": page.get_name() or "",
                        "title": page.get_title() or "",
                        "child": page.get_child(),
                    })
            except Exception:
                continue
        return pages

    def switch_page(self, page: dict) -> None:
        try:
            page["stack"].set_visible_child_name(page["name"])
        except Exception:
            try:
                page["stack"].set_visible_child(page["child"])
            except Exception:
                pass
        # Select the matching sidebar entry so the nav highlight is rendered.
        name = (page.get("name") or "").lower()
        nav = getattr(self, "_last_window", None)
        toggled = False
        if nav is not None:
            candidate = getattr(nav, f"nav_{name}", None)
            if candidate is not None:
                try:
                    candidate.set_active(True)
                    toggled = True
                except Exception:
                    toggled = False
            if not toggled:
                for widget in iter_widgets(nav):
                    if not isinstance(widget, self.Gtk.ToggleButton):
                        continue
                    if "nav-item" not in widget_css(widget):
                        continue
                    label = button_text(widget).strip().lower()
                    if label == name or name in label or label in name:
                        try:
                            widget.set_active(True)
                        except Exception:
                            pass
                        break
        self.pump()

    def expander(self, win, needle: str):
        for widget in iter_widgets(win):
            if isinstance(widget, self.Gtk.Expander):
                try:
                    label = widget.get_label() or ""
                except Exception:
                    label = ""
                if needle.lower() in label.lower() or needle.lower() in (
                        widget_text(widget).lower()):
                    return widget
        return None

    def scroll_to_bottom(self, win) -> bool:
        scroller = next((w for w in iter_widgets(win)
                         if isinstance(w, self.Gtk.ScrolledWindow)), None)
        if scroller is None:
            return False
        adjustment = scroller.get_vadjustment()
        if adjustment is None:
            return False
        adjustment.set_value(max(0.0, adjustment.get_upper()
                                 - adjustment.get_page_size()))
        self.pump()
        return True

    def find_row(self, win, slot: str):
        rows = getattr(win, "rows", {})
        return rows.get(slot, {}).get("row") if isinstance(rows, dict) else None

    def destroy_all(self) -> None:
        self.close_windows()
        try:
            self.renderer.unrealize()
        except Exception:
            pass
        for path in list(self.temp_pngs):
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_pngs.clear()


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def _scenario_macros(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    harness.pump()
    path = f"macros-{width}x{height}"
    capture = harness.capture_window(win, path)
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(win, width, height)
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": ["default profile view (P1-P5 numpad, P6 text-macro fixture)"],
    }


def _scenario_states(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    # One command-bound slot (typing macro off), one empty slot, one recording?
    # Recording has its own scenario; here show command-bound + empty + pills.
    win.commands["P2"] = "firefox"
    win.slots.pop("P3", None)
    win._refresh_all()
    harness.pump()
    capture = harness.capture_window(win, f"states-{width}x{height}")
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(win, width, height)
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": ["P2 command-bound, P3 empty, EC status as initialised"],
    }


def _scenario_editor(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    # A mixed h+e chord is the mis-recording the editor exists to fix.
    win.slots["P1"] = [["h", "e"], ["Control_L", "c"]]
    win._refresh_all()
    harness.pump()
    harness.dialogs.clear()
    win._open_macro_editor("P1")
    harness.pump()
    if not harness.dialogs:
        return {"error": "editor dialog was not created",
                "captures": [], "analysis": None,
                "widgets": harness.checks(win, width, height)}
    dialog = harness.dialogs[-1]
    capture = harness.capture_dialog(dialog, "editor-dialog")
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(dialog, dialog.get_width(), dialog.get_height())
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": ["step editor open on P1 with a mixed h+e chord"],
    }


def _scenario_editor_single(harness, width, height):
    """Editor on a single-key step: Split is disabled, row is not a chord."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    win.slots["P2"] = [["a"]]
    win._refresh_all()
    harness.pump()
    harness.dialogs.clear()
    win._open_macro_editor("P2")
    harness.pump()
    if not harness.dialogs:
        return {"error": "editor dialog was not created", "captures": [],
                "analysis": None, "widgets": harness.checks(win, width, height)}
    dialog = harness.dialogs[-1]
    capture = harness.capture_dialog(dialog, "editor-single")
    checks = harness.checks(dialog, dialog.get_width(), dialog.get_height())
    checks["disabled_buttons"] = [button for button in checks["buttons"]
                                  if not button.get("sensitive")]
    return {
        "captures": [capture],
        "analysis": analyze_png(capture.get("path", "")),
        "widgets": checks,
        "notes": ["editor on the single-key macro P2 = 'a' (Split disabled)"],
    }


def _scenario_editor_error(harness, width, height):
    """Editor with an invalid step: the dialog must show the error inline."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    win.slots["P1"] = [["h", "e"], ["Control_L", "c"]]
    win._refresh_all()
    harness.pump()
    harness.dialogs.clear()
    win._open_macro_editor("P1")
    harness.pump()
    if not harness.dialogs:
        return {"error": "editor dialog was not created", "captures": [],
                "analysis": None, "widgets": harness.checks(win, width, height)}
    dialog = harness.dialogs[-1]
    entries = [widget for widget in iter_widgets(dialog)
               if isinstance(widget, Gtk.Entry) and widget.get_mapped()]
    if entries:
        entries[0].set_text("Bogus")
    for widget in iter_widgets(dialog):
        if isinstance(widget, Gtk.Button) and widget.get_label() == "OK":
            widget.emit("clicked")
            break
    harness.pump()
    capture = harness.capture_dialog(dialog, "editor-error")
    checks = harness.checks(dialog, dialog.get_width(), dialog.get_height())
    checks["dialog_visible"] = dialog.get_visible()
    return {
        "captures": [capture],
        "analysis": analyze_png(capture.get("path", "")),
        "widgets": checks,
        "notes": ["invalid step text + OK: error must appear inside the dialog"],
    }


def _scenario_dialogs(harness, width, height):
    """Every other dialog shell: keys, command, new-profile."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    openers = [
        ("dialog-keys", lambda: win._open_keys_dialog("P1")),
        ("dialog-command", lambda: win._open_command_dialog("P2")),
        ("dialog-profile-new", lambda: win._profile_dialog(
            "New profile", "", "Create", lambda text: None)),
    ]
    dialogs = []
    captures = []
    notes = []
    for name, opener in openers:
        harness.dialogs.clear()
        try:
            opener()
            harness.pump()
        except Exception as exc:
            notes.append(f"WARN {name}: {exc}")
            continue
        if not harness.dialogs:
            notes.append(f"WARN {name}: dialog was not created")
            continue
        dialog = harness.dialogs[-1]
        capture = harness.capture_dialog(dialog, name)
        checks = harness.checks(dialog, dialog.get_width(), dialog.get_height())
        captures.append(capture)
        dialogs.append({"name": name, "capture": capture, "widgets": checks})
        notes.append(f"{name}: {dialog.get_width()}x{dialog.get_height()}")
        try:
            dialog.close()
        except Exception:
            pass
        harness.pump(0.08)
    if not dialogs:
        return {"error": "no dialog could be opened", "captures": [],
                "analysis": None, "widgets": harness.checks(win, width, height)}
    merged = {
        "window_size": dialogs[0]["widgets"]["window_size"],
        "requested_size": dialogs[0]["widgets"]["requested_size"],
        "grew_beyond_request": any(d["widgets"]["grew_beyond_request"]
                                   for d in dialogs),
        "buttons": sum((d["widgets"]["buttons"] for d in dialogs), []),
        "empty_buttons": sum((d["widgets"]["empty_buttons"] for d in dialogs), []),
        "clipped_labels": sum((d["widgets"]["clipped_labels"] for d in dialogs), []),
        "oversized_widgets": sum((d["widgets"]["oversized_widgets"] for d in dialogs), []),
        "ellipsized_labels": sum((d["widgets"]["ellipsized_labels"] for d in dialogs), []),
        "pills": sum((d["widgets"]["pills"] for d in dialogs), []),
        "banners": [],
        "recording_rows": 0,
        "visible_focus_widgets": sum((d["widgets"]["visible_focus_widgets"] for d in dialogs), []),
        "window_controls": [],
        "nav_items": [],
        "cards": 0,
        "uppercase_violations": sum((d["widgets"]["uppercase_violations"] for d in dialogs), []),
        "tree": sum((d["widgets"]["tree"] for d in dialogs), [])[:260],
    }
    return {
        "captures": captures,
        "analysis": analyze_png(captures[0].get("path", "")),
        "widgets": merged,
        "dialogs": dialogs,
        "notes": notes,
    }


def _scenario_applied(harness, width, height):
    """Success path: EC reports applied=1, so the sidebar pill is green."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    win._set_status(
        "wrote 4096 bytes to /sys/devices/platform/hp-wmi/macro_profile\n"
        "macro_status: profile_write_ret=0 mode_set_ret=0 applied=1")
    harness.pump()
    capture = harness.capture_window(win, f"applied-{width}x{height}")
    return {
        "captures": [capture],
        "analysis": analyze_png(capture.get("path", "")),
        "widgets": harness.checks(win, width, height),
        "notes": ["EC applied=1: sidebar EC pill should read APPLIED/green"],
    }


def _scenario_no_hardware(harness, width, height):
    """Missing macro_profile: the app must say the interface is unavailable."""
    sysfs = Path(os.environ["OMEN_MACRO_SYSFS"])
    profile = sysfs / "macro_profile"
    saved = profile.read_bytes() if profile.exists() else b""
    profile.unlink(missing_ok=True)
    try:
        win = harness.new_window(width, height)
        harness.default_profile(win)
        harness.pump()
        capture = harness.capture_window(win, f"no-hardware-{width}x{height}")
        checks = harness.checks(win, width, height)
        banner_text = " ".join(b.get("text", "") for b in checks.get("banners") or [])
        checks["hardware_banner"] = banner_text
    finally:
        profile.write_bytes(saved)
    return {
        "captures": [capture],
        "analysis": analyze_png(capture.get("path", "")),
        "widgets": checks,
        "notes": ["macro_profile removed: error banner + EC pill state"],
    }


def _scenario_dialog_errors(harness, width, height):
    """Invalid input in each modal dialog must show an inline error."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    cases = []

    def entries_of(dialog):
        return [widget for widget in iter_widgets(dialog)
                if isinstance(widget, Gtk.Entry) and widget.get_mapped()]

    def click(dialog, label):
        for widget in iter_widgets(dialog):
            if isinstance(widget, Gtk.Button) and widget.get_label() == label:
                widget.emit("clicked")
                return True
        return False

    # 1. new profile with an empty (invalid) name
    harness.dialogs.clear()
    win._on_profile_new(None)
    harness.pump()
    if harness.dialogs:
        dialog = harness.dialogs[-1]
        for entry in entries_of(dialog):
            entry.set_text("")
        click(dialog, "Create")
        harness.pump()
        cases.append(("dialog-error-profile", dialog))

    # 2. command dialog with an empty command
    harness.dialogs.clear()
    win._open_command_dialog("P2")
    harness.pump()
    if harness.dialogs:
        dialog = harness.dialogs[-1]
        for entry in entries_of(dialog):
            entry.set_text("")
        click(dialog, "Bind")
        harness.pump()
        cases.append(("dialog-error-command", dialog))

    # 3. keys dialog with an unencodable key name
    harness.dialogs.clear()
    win._open_keys_dialog("P1")
    harness.pump()
    if harness.dialogs:
        dialog = harness.dialogs[-1]
        entries = entries_of(dialog)
        if entries:
            entries[-1].set_text("Bogus")
        click(dialog, "Add chord")
        harness.pump()
        cases.append(("dialog-error-keys", dialog))

    captures = []
    dialogs = []
    for name, dialog in cases:
        capture = harness.capture_dialog(dialog, name)
        checks = harness.checks(dialog, dialog.get_width(), dialog.get_height())
        captures.append(capture)
        dialogs.append({"name": name, "capture": capture, "widgets": checks})
        try:
            dialog.close()
        except Exception:
            pass
        harness.pump(0.08)
    if not dialogs:
        return {"error": "no dialog error state could be created",
                "captures": [], "analysis": None,
                "widgets": harness.checks(win, width, height)}
    merged = dict(dialogs[0]["widgets"])
    for extra in dialogs[1:]:
        for key in ("buttons", "empty_buttons", "clipped_labels",
                    "oversized_widgets", "ellipsized_labels", "pills",
                    "uppercase_violations"):
            merged[key] = (merged.get(key) or []) + (extra["widgets"].get(key) or [])
    merged["banners"] = sum((d["widgets"].get("banners") or [] for d in dialogs), [])
    return {
        "captures": captures,
        "analysis": analyze_png(captures[0].get("path", "")),
        "widgets": merged,
        "dialogs": dialogs,
        "notes": [f"{d['name']}: inline error capture" for d in dialogs],
    }


def _scenario_recording(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    win._start_recording("P2")
    rec = win.recording
    if rec is not None:
        for chord in (["Control_L", "c"], ["v"]):
            for key in chord:
                rec.press(key)
            for key in chord:
                rec.release(key)
        win._sync_recording_ui()
    harness.pump()
    capture = harness.capture_window(win, f"recording-{width}x{height}")
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(win, width, height)
    notes = [f"recording {win.recording_slot}, "
             f"{len(rec.chords) if rec else 0} chord(s)"]
    row = harness.find_row(win, "P2")
    checks["recording_row_css"] = widget_css(row) if row is not None else []
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": notes,
    }


def _scenario_banner(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    long_error = (
        "Could not save profile 'Gaming night': permission denied writing "
        "/sys/devices/platform/hp-wmi/macro_profile - run install.sh to grant "
        "the omen-hw group write access, then Re-apply. The keys keep the last "
        "macro the embedded controller received."
    )
    win._set_banner(long_error, "banner-error")
    win._set_status(
        "wrote 0 bytes to /sys/devices/platform/hp-wmi/macro_profile\n"
        "macro_status: profile_write_ret=-13 mode_set_ret=0 applied=0\n"
        "P2 and P4 run commands, so their typing macros were not uploaded.")
    harness.pump()
    capture = harness.capture_window(win, f"banner-{width}x{height}")
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(win, width, height)
    banner = checks.get("banners") or []
    checks["banner_visible"] = bool(banner and banner[0].get("visible"))
    checks["banner_wrapped"] = bool(banner and banner[0].get("size", [0, 0])[1] > 25)
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": ["error banner with a long message, long EC status"],
    }


def _scenario_autoswitch(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    captures = []
    notes = []
    checks = harness.checks(win, width, height)
    pages = harness.stack_pages(win)
    page = None
    for candidate in pages:
        haystack = f"{candidate['name']} {candidate['title']}".lower()
        if any(word in haystack for word in ("auto", "switch", "behavio")):
            page = candidate
            break
    if page is not None:
        harness.switch_page(page)
        expander = harness.expander(win, "auto")
        if expander is not None:
            expander.set_expanded(True)
            harness.pump()
        target = "Default"
        try:
            names = harness.gui.profiles.list_profiles(win._root)
            target = names[0] if names else target
        except Exception:
            pass
        win._auto_rules = [
            {"match": "cs2", "profile": target},
            {"match": "firefox", "profile": target},
        ]
        try:
            win._refresh_auto_rules()
        except Exception:
            pass
        harness.pump()
        name = f"autoswitch-{width}x{height}"
        capture = harness.capture_window(win, name)
        capture["page"] = page["name"]
        captures.append(capture)
        notes.append(f"switched to stack page {page['name']!r}")
        checks = harness.checks(win, width, height)
    else:
        expander = harness.expander(win, "auto")
        if expander is not None:
            expander.set_expanded(True)
            harness.pump()
            # Give the rules list content so the section is worth looking at.
            profiles = []
            try:
                profiles = harness.gui.profiles.list_profiles(win._root)
            except Exception:
                pass
            target = profiles[0] if profiles else "Default"
            win._auto_rules = [
                {"match": "cs2", "profile": target},
                {"match": "firefox", "profile": target},
            ]
            try:
                win._refresh_auto_rules()
            except Exception:
                pass
            harness.scroll_to_bottom(win)
            notes.append("no page stack: expanded the auto-switch section")
        else:
            notes.append("WARN: no auto-switch page or section found")
        capture = harness.capture_window(win, f"autoswitch-{width}x{height}")
        captures.append(capture)
        checks = harness.checks(win, width, height)
    analysis = analyze_png(captures[-1].get("path", ""))
    return {
        "captures": captures,
        "analysis": analysis,
        "widgets": checks,
        "notes": notes,
    }


def _scenario_advanced(harness, width, height):
    win = harness.new_window(width, height)
    harness.default_profile(win)
    expander = harness.expander(win, "advanced")
    if expander is not None:
        expander.set_expanded(True)
        harness.pump()
        notes = ["advanced modifier-slot section expanded"]
    else:
        notes = ["WARN: advanced expander not found"]
    harness.scroll_to_bottom(win)
    capture = harness.capture_window(win, f"advanced-{width}x{height}")
    analysis = analyze_png(capture.get("path", ""))
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": harness.checks(win, width, height),
        "notes": notes,
    }


def _scenario_pages(harness, width, height):
    """Capture every page of every sidebar/stack the restyle exposes."""
    win = harness.new_window(width, height)
    harness.default_profile(win)
    pages = harness.stack_pages(win)
    captures = []
    notes = []
    if not pages:
        notes.append("WARN: no Gtk.Stack found (single-page layout)")
    for page in pages:
        harness.switch_page(page)
        slug = re.sub(r"[^a-z0-9]+", "-", page["name"].lower()).strip("-") or "page"
        capture = harness.capture_window(win, f"page-{slug}-{width}x{height}")
        capture["page"] = page["name"]
        capture["page_title"] = page["title"]
        captures.append(capture)
        notes.append(f"page {page['name']!r} captured")
    checks = harness.checks(win, width, height)
    return {
        "captures": captures,
        "analysis": (analyze_png(captures[0].get("path", "")) if captures else None),
        "widgets": checks,
        "notes": notes,
    }


def _scenario_narrow(harness, width, height):
    narrow_width, narrow_height = min(width, 1024), min(height, 700)
    win = harness.new_window(narrow_width, narrow_height)
    harness.default_profile(win)
    harness.pump()
    capture = harness.capture_window(win, "narrow-1024x700")
    analysis = analyze_png(capture.get("path", ""))
    checks = harness.checks(win, narrow_width, narrow_height)
    return {
        "captures": [capture],
        "analysis": analysis,
        "widgets": checks,
        "notes": [f"narrow layout requested {narrow_width}x{narrow_height}, "
                  f"actual {win.get_width()}x{win.get_height()}"],
    }


SCENARIOS = {
    "macros": _scenario_macros,
    "states": _scenario_states,
    "editor": _scenario_editor,
    "editor-single": _scenario_editor_single,
    "editor-error": _scenario_editor_error,
    "dialogs": _scenario_dialogs,
    "dialog-errors": _scenario_dialog_errors,
    "applied": _scenario_applied,
    "no-hardware": _scenario_no_hardware,
    "recording": _scenario_recording,
    "banner": _scenario_banner,
    "autoswitch": _scenario_autoswitch,
    "advanced": _scenario_advanced,
    "pages": _scenario_pages,
    "narrow": _scenario_narrow,
}
SCENARIO_ORDER = ["macros", "states", "editor", "editor-single", "editor-error",
                  "dialogs", "dialog-errors", "applied", "no-hardware",
                  "recording", "banner", "autoswitch", "advanced", "pages",
                  "narrow"]


# --------------------------------------------------------------------------- #
# Checks over a completed scenario
# --------------------------------------------------------------------------- #
def scenario_checks(name: str, result: dict, expect_dark: bool,
                    gui_module=None) -> list[dict]:
    """Hard/soft checks for one scenario's captures and widget tree."""
    checks: list[dict] = []
    captures = result.get("captures") or []
    if result.get("error"):
        checks.append({"name": f"{name}:render", "status": "FAIL",
                       "detail": result["error"]})
    for capture in captures:
        if capture.get("error"):
            checks.append({"name": f"{name}:capture", "status": "FAIL",
                           "detail": capture["error"]})
            continue
        if capture.get("opacity", OPACITY) > 0.02:
            checks.append({"name": f"{name}:invisible", "status": "FAIL",
                           "detail": f"window opacity {capture['opacity']}"})
        analysis = analyze_png(capture.get("path", ""))
        if analysis is None:
            checks.append({"name": f"{name}:analysis", "status": "WARN",
                           "detail": "pycairo missing, pixel checks skipped"})
            continue
        if analysis.get("error"):
            checks.append({"name": f"{name}:analysis", "status": "FAIL",
                           "detail": analysis["error"]})
            continue
        if analysis["blank"]:
            checks.append({"name": f"{name}:not-blank", "status": "FAIL",
                           "detail": f"capture is blank: {analysis}"})
        else:
            checks.append({"name": f"{name}:not-blank", "status": "PASS",
                           "detail": f"{analysis['distinct_colors']} colours"})
        if expect_dark:
            status = ("PASS" if analysis["background_luminance"] < 0.22
                      else "FAIL")
            checks.append({
                "name": f"{name}:palette-dark",
                "status": status,
                "detail": (f"background {analysis['background']} "
                           f"L={analysis['background_luminance']}"),
            })
        else:
            checks.append({
                "name": f"{name}:palette-dark",
                "status": "WARN",
                "detail": (f"background {analysis['background']} "
                           f"L={analysis['background_luminance']} "
                           "(dark palette not expected pre-restyle)"),
            })
        if expect_dark:
            omen_share = analysis["accent_pixels"].get("omen", 0.0)
            checks.append({
                "name": f"{name}:brand-accent",
                "status": "PASS" if omen_share >= 0.0002 else "WARN",
                "detail": f"OMEN red pixels {omen_share:.3%} of the capture",
            })
        cards = [
            colour for colour in analysis["top_colors"]
            if colour["color"] != analysis["background"]
            and colour["share"] >= 0.005
        ]
        checks.append({
            "name": f"{name}:panel-distinct",
            "status": "PASS" if cards else "WARN",
            "detail": (f"panels {cards}" if cards
                       else "no panel colour differs from the background"),
        })
        blue = analysis["accent_pixels"].get("blue", 0.0)
        checks.append({
            "name": f"{name}:palette-only",
            "status": "WARN" if blue >= 0.0002 else "PASS",
            "detail": (f"blue pixels {blue:.3%} (desktop-theme accent: focus "
                       "ring/dropdown arrow leak?)" if blue >= 0.0002
                       else "no desktop-theme blue accent pixels"),
        })
        tokens = theme_tokens(gui_module) if gui_module is not None else {}
        off = []
        for entry in analysis["top_colors"]:
            colour = parse_color(entry["color"])
            if colour is None or entry["share"] < 0.02:
                continue
            if not near_palette_colour(colour, tokens):
                off.append(entry)
        checks.append({
            "name": f"{name}:off-palette-area",
            "status": "WARN" if off else "PASS",
            "detail": (f"large areas outside the THEME palette (theme leak?): "
                       f"{off[:4]}" if off
                       else "every large area matches THEME tokens/tints"),
        })
        checks.append({
            "name": f"{name}:text-present",
            "status": ("PASS"
                       if (analysis.get("max_luminance", 0.0) >= 0.45
                           and analysis["brightest_1pct_luminance"]
                           > analysis["background_luminance"] + 0.05)
                       else "WARN"),
            "detail": (f"max L={analysis.get('max_luminance')} "
                       f"brightest 1% L={analysis['brightest_1pct_luminance']} "
                       f"vs bg L={analysis['background_luminance']}"),
        })

    widgets = result.get("widgets") or {}

    controls = widgets.get("window_controls") or []
    invisible = []
    capture_path = (captures[0].get("path") if captures else "") or ""
    capture_bg = (captures[0].get("background") if captures else "") or ""
    titlebar_position = None
    for part in (captures[0].get("parts") if captures else []) or []:
        if part.get("part") == "titlebar":
            titlebar_position = part.get("position") or [0, 0]
            break
    for control in controls:
        rect = None
        if control.get("local_bounds") and titlebar_position is not None:
            local = control["local_bounds"]
            rect = [local[0] + titlebar_position[0],
                    local[1] + titlebar_position[1], local[2], local[3]]
        if rect is None:
            rect = control.get("bounds")
        if not rect:
            continue
        ink = region_ink(capture_path, rect, capture_bg)
        if ink == 0:
            invisible.append(control["name"])
    if controls:
        checks.append({
            "name": f"{name}:window-controls-visible",
            "status": "FAIL" if invisible else "PASS",
            "detail": (f"invisible window controls: {sorted(set(invisible))} "
                       "(theme assets removed but no icon shown)" if invisible
                       else f"{len(controls)} window control(s) painted pixels"),
        })
    pills = widgets.get("pills") or []
    empty_pills = [pill for pill in pills if not (pill.get("text") or "").strip()]
    checks.append({
        "name": f"{name}:state-has-text",
        "status": "FAIL" if empty_pills else "PASS",
        "detail": (f"{len(empty_pills)} colour-only pill(s) with no text"
                   if empty_pills else
                   f"{len(pills)} pill(s), all carry text"),
    })

    sidebar_width = widgets.get("sidebar_width")
    if sidebar_width is not None and widgets.get("window_size"):
        window_width = widgets["window_size"][0]
        too_wide = sidebar_width > max(300, int(window_width * 0.3))
        checks.append({
            "name": f"{name}:sidebar-width",
            "status": "WARN" if too_wide else "PASS",
            "detail": (f"sidebar {sidebar_width}px of {window_width}px window"
                       + (" (>30%: a descendant likely hexpands, inflating the"
                          " sidebar)" if too_wide else "")),
        })
    if widgets.get("grew_beyond_request"):
        checks.append({
            "name": f"{name}:fits-requested-size",
            "status": "FAIL",
            "detail": (f"window grew to {widgets['window_size']} for a "
                       f"requested {widgets['requested_size']}"),
        })
    else:
        checks.append({"name": f"{name}:fits-requested-size", "status": "PASS",
                       "detail": str(widgets.get("window_size"))})

    clipped = widgets.get("clipped_labels") or []
    checks.append({
        "name": f"{name}:no-clipped-text",
        "status": "FAIL" if clipped else "PASS",
        "detail": (f"{len(clipped)} clipped label(s): "
                   f"{[item['text'] for item in clipped[:4]]}" if clipped
                   else "no hard-clipped labels"),
    })
    oversized = widgets.get("oversized_widgets") or []
    checks.append({
        "name": f"{name}:content-width",
        "status": "WARN" if oversized else "PASS",
        "detail": (f"{len(oversized)} widget(s) measure wider than the window: "
                   f"{[(item['type'], item['text'][:24], item['minimum_width']) for item in oversized[:4]]}"
                   if oversized else "content measures within the window"),
    })
    bad_buttons = [item for item in widgets.get("empty_buttons") or []
                   if item.get("severity") == "fail"]
    icon_only = [item for item in widgets.get("empty_buttons") or []
                 if item.get("severity") == "warn"]
    checks.append({
        "name": f"{name}:buttons-labelled",
        "status": "FAIL" if bad_buttons else ("WARN" if icon_only else "PASS"),
        "detail": (f"unlabelled: {[(b['css'], b['tooltip'][:30]) for b in bad_buttons[:4]]}"
                   if bad_buttons else
                   (f"icon-only but tooltipped: {[b['css'] for b in icon_only[:4]]}"
                    if icon_only else
                    f"{len(widgets.get('buttons') or [])} button(s) all labelled")),
    })

    for check in widgets.get("banners") or []:
        if not check.get("visible"):
            checks.append({"name": f"{name}:banner-visible", "status": "FAIL",
                           "detail": "banner widget exists but is hidden"})
    return checks


def recording_checks(result: dict) -> list[dict]:
    widgets = result.get("widgets") or {}
    checks = []
    css = widgets.get("recording_row_css") or []
    checks.append({
        "name": "recording:row-highlight",
        "status": "PASS" if "recording" in css else "FAIL",
        "detail": f"row css classes {css}",
    })
    checks.append({
        "name": "recording:row-summary",
        "status": "PASS" if widgets.get("recording_rows") == 1 else "FAIL",
        "detail": f"{widgets.get('recording_rows')} row(s) marked recording",
    })
    pills = widgets.get("pills") or []
    unknown = [pill for pill in pills if (pill.get("text") or "") == "UNKNOWN"]
    checks.append({
        "name": "recording:ec-pill-stable",
        "status": "WARN" if unknown else "PASS",
        "detail": ("EC pill fell back to UNKNOWN while recording (transient "
                   "status overwrote the EC state)" if unknown
                   else f"EC pill keeps its state: {[p['text'] for p in pills]}"),
    })
    return checks


EDITOR_BUTTONS = {"Split", "Insert", "Delete", "Add step", "Reset", "Cancel",
                  "OK"}


def editor_checks(result: dict) -> list[dict]:
    """The step editor dialog must keep its full button set and OEM shell."""
    widgets = result.get("widgets") or {}
    texts = {button.get("text") for button in widgets.get("buttons") or []}
    missing = sorted(EDITOR_BUTTONS - texts)
    checks = [{
        "name": "editor:buttons",
        "status": "FAIL" if missing else "PASS",
        "detail": (f"missing buttons: {missing}" if missing
                   else f"all {len(EDITOR_BUTTONS)} editor buttons present"),
    }]
    captures = result.get("captures") or []
    dialog = captures[0] if captures else {}
    classes = dialog.get("css_classes") or []
    parts = [part.get("part") for part in dialog.get("parts") or []]
    checks.append({
        "name": "editor:shell",
        "status": "PASS" if "omen-dialog" in classes and "titlebar" in parts
                  else "WARN",
        "detail": f"dialog css {classes}, captured parts {parts}",
    })
    if "disabled_buttons" in widgets:
        disabled = widgets.get("disabled_buttons") or []
        checks.append({
            "name": "editor:disabled-state",
            "status": "PASS" if disabled else "WARN",
            "detail": (f"disabled button(s) rendered: "
                       f"{[b['text'] for b in disabled]}" if disabled
                       else "no disabled button in this dialog"),
        })
    return checks


def states_checks(result: dict) -> list[dict]:
    """Command-bound row text + empty row text are the visible state markers."""
    widgets = result.get("widgets") or {}
    texts = [entry.get("text", "") for entry in widgets.get("tree") or []]
    joined = " | ".join(text for text in texts if text)
    checks = [{
        "name": "states:command-summary",
        "status": "PASS" if "runs 'firefox'" in joined else "WARN",
        "detail": "P2 command-bound row shows its command"
                  if "runs 'firefox'" in joined else "command row text not found",
    }]
    empty_rows = [text for text in texts if text == "empty"]
    checks.append({
        "name": "states:empty-summary",
        "status": "PASS" if empty_rows else "WARN",
        "detail": f"{len(empty_rows)} row(s) render the 'empty' summary",
    })
    return checks


DIALOG_BUTTONS = {
    "dialog-keys": {"Remove", "Add chord", "Reset", "Cancel", "OK"},
    "dialog-command": {"Cancel", "Bind"},
    "dialog-profile-new": {"Cancel", "Create"},
}


def _titlebar_position(capture: dict):
    for part in capture.get("parts") or []:
        if part.get("part") == "titlebar":
            return part.get("position") or [0, 0]
    return None


def _visible_controls(widgets: dict, capture: dict) -> list[str]:
    """Window-control names that painted no pixel in this dialog capture."""
    invisible = []
    position = _titlebar_position(capture)
    for control in widgets.get("window_controls") or []:
        rect = None
        if control.get("local_bounds") and position is not None:
            local = control["local_bounds"]
            rect = [local[0] + position[0], local[1] + position[1],
                    local[2], local[3]]
        if rect is None:
            rect = control.get("bounds")
        if not rect:
            continue
        if region_ink(capture.get("path", ""), rect,
                      capture.get("background")) == 0:
            invisible.append(control["name"])
    return invisible


def editor_error_checks(result: dict) -> list[dict]:
    """The editor must report a bad step inside the modal dialog."""
    widgets = result.get("widgets") or {}
    errors = [banner for banner in widgets.get("banners") or []
              if banner.get("visible") and "banner-error" in banner.get("css", [])
              and (banner.get("text") or "").strip()]
    checks = [{
        "name": "editor:inline-error",
        "status": "PASS" if errors else "FAIL",
        "detail": (f"inline error shown: {errors[0]['text'][:80]!r}"
                   if errors else "no visible error inside the dialog"),
    }]
    checks.append({
        "name": "editor:stays-open",
        "status": "PASS" if widgets.get("dialog_visible") else "FAIL",
        "detail": "dialog still open after invalid input"
                  if widgets.get("dialog_visible") else "dialog closed on invalid input",
    })
    return checks


def dialogs_checks(result: dict) -> list[dict]:
    """Per-dialog shell, button set and window-control visibility."""
    checks = []
    for entry in result.get("dialogs") or []:
        name = entry["name"]
        widgets = entry["widgets"]
        capture = entry["capture"]
        expected = DIALOG_BUTTONS.get(name, set())
        texts = {button.get("text") for button in widgets.get("buttons") or []}
        missing = sorted(expected - texts)
        checks.append({
            "name": f"{name}:buttons",
            "status": "FAIL" if missing else "PASS",
            "detail": (f"missing buttons: {missing}" if missing
                       else f"buttons {sorted(texts)}"),
        })
        classes = capture.get("css_classes") or []
        parts = [part.get("part") for part in capture.get("parts") or []]
        checks.append({
            "name": f"{name}:shell",
            "status": "PASS" if "omen-dialog" in classes and "titlebar" in parts
                      else "WARN",
            "detail": f"dialog css {classes}, captured parts {parts}",
        })
        invisible = _visible_controls(widgets, capture)
        checks.append({
            "name": f"{name}:window-controls-visible",
            "status": "FAIL" if invisible else "PASS",
            "detail": (f"invisible window controls: {sorted(set(invisible))}"
                       if invisible else "window controls painted pixels"),
        })
    return checks


def pill_state_checks(result: dict, expected_text: str,
                      expected_class: str) -> list[dict]:
    """EC pill must carry both the right word and the right state class."""
    widgets = result.get("widgets") or {}
    pills = widgets.get("pills") or []
    match = [pill for pill in pills
             if (pill.get("text") or "").strip() == expected_text]
    classes = match[0].get("css") if match else []
    checks = [{
        "name": f"ec-pill:{expected_text}",
        "status": "PASS" if match and expected_class in classes else "WARN",
        "detail": (f"pill {expected_text!r} classes {classes}" if match
                   else f"no pill text {expected_text!r}; pills {pills}"),
    }]
    return checks


def dialog_error_checks(result: dict) -> list[dict]:
    """Every dialog that rejects input must show its own visible error label."""
    checks = []
    for entry in result.get("dialogs") or []:
        name = entry["name"]
        visible_errors = [
            banner for banner in (entry["widgets"].get("banners") or [])
            if banner.get("visible") and "banner-error" in banner.get("css", [])
            and (banner.get("text") or "").strip()
        ]
        checks.append({
            "name": f"{name}:inline-error",
            "status": "PASS" if visible_errors else "FAIL",
            "detail": (f"inline error: {visible_errors[0]['text'][:90]!r}"
                       if visible_errors else "no visible error inside the dialog"),
        })
    return checks


def banner_checks(result: dict) -> list[dict]:
    widgets = result.get("widgets") or {}
    checks = []
    checks.append({
        "name": "banner:visible",
        "status": "PASS" if widgets.get("banner_visible") else "FAIL",
        "detail": f"banner widgets {widgets.get('banners')}",
    })
    banners = widgets.get("banners") or []
    lines = max([item.get("lines") or 0 for item in banners] or [0])
    height = max([(item.get("size") or [0, 0])[1] for item in banners] or [0])
    wrapped = lines >= 2 or height >= 25
    checks.append({
        "name": "banner:wraps",
        "status": "PASS" if wrapped else "WARN",
        "detail": (f"long message wraps over {lines} line(s), {height}px"
                   if wrapped else "banner may not wrap"),
    })
    return checks


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the OMEN Macro Keys GTK window offscreen and run "
                    "visual QA checks (no visible window, no hardware touched).")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"output directory for PNGs and report.json "
                             f"(default {DEFAULT_OUT})")
    parser.add_argument("--width", type=int, default=1280,
                        help="window width for the standard scenarios")
    parser.add_argument("--height", type=int, default=900,
                        help="window height for the standard scenarios")
    parser.add_argument("--only", default="",
                        help="comma-separated scenario names (default: all)")
    parser.add_argument("--theme", default="",
                        help="GTK_THEME for the render, e.g. Adwaita:dark "
                             "(default: the user's real theme)")
    parser.add_argument("--json", default="",
                        help="report path (default <out>/report.json)")
    parser.add_argument("--list", action="store_true",
                        help="list scenarios and exit")
    parser.add_argument("--report-only", action="store_true",
                        help="always exit 0, even when a hard check fails")
    parser.add_argument("--keep-opacity", action="store_true",
                        help="do not neutralise .dim-label opacity "
                             "(reproduces the offscreen doubling artefact)")
    parser.add_argument("--expect-dark", dest="expect_dark",
                        action="store_true", default=None,
                        help="require a dark chassis palette")
    parser.add_argument("--no-expect-dark", dest="expect_dark",
                        action="store_false",
                        help="do not require a dark chassis palette")
    return parser.parse_args(argv)


def selected_scenarios(only: str) -> list[str]:
    if not only.strip():
        return list(SCENARIO_ORDER)
    requested = [part.strip().lower() for part in only.split(",") if part.strip()]
    chosen = []
    for name in SCENARIO_ORDER:
        for wanted in requested:
            if name == wanted or name.startswith(wanted):
                chosen.append(name)
                break
    return chosen


def skipped(reason: str) -> int:
    print(f"SKIPPED: {reason}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        print("scenarios: " + ", ".join(SCENARIO_ORDER))
        return 0

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return skipped("no DISPLAY/WAYLAND_DISPLAY (offscreen GTK needs a display)")

    if args.theme:
        os.environ["GTK_THEME"] = args.theme

    # Temp config + sysfs tree: the real profile store and EC are never touched.
    temp_root = Path(tempfile.mkdtemp(prefix="omen-render-"))
    config_root = temp_root / "cfg"
    sysfs_root = temp_root / "sysfs"
    sysfs_root.mkdir(parents=True)
    (sysfs_root / "macro_profile").write_bytes(b"")
    (sysfs_root / "macro_status").write_text(
        "profile_write_ret=0\nmode_set_ret=0\napplied=1\n")
    os.environ["OMEN_MACRO_ROOT"] = str(config_root)
    os.environ["OMEN_MACRO_SYSFS"] = str(sysfs_root)

    try:
        import cairo as _pycairo  # noqa: F401 - registers pycairo before gi
    except Exception:
        _pycairo = None
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gsk", "4.0")
        gi.require_version("Pango", "1.0")
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        return skipped(f"PyGObject/GTK4 unavailable ({exc})")

    global Gtk, Gdk, Gsk, GLib, Pango
    try:
        from gi.repository import Gdk, GLib, Gsk, Gtk  # noqa: F401
        from gi.repository import Pango  # noqa: F401
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        return skipped(f"GTK4 typelib unavailable ({exc})")

    try:
        gi.require_foreign("cairo")
    except Exception:
        pass
    app = Gtk.Application(application_id="org.omen.renderui")
    try:
        app.register(None)
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        return skipped(f"cannot open a GTK display ({exc})")

    # Import after the display is known to exist: gui.py constructs no GTK
    # object at import time, but this keeps the SKIPPED path free of it.
    try:
        from omen_macro import gui
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        print(f"SKIPPED: cannot import omen_macro.gui ({exc})")
        return 0

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    harness = Harness(out_dir, gui, app, Gtk, Gdk, Gsk, GLib, args)
    harness.install_css()
    harness.install_dialog_recorder()

    expect_dark = args.expect_dark
    if expect_dark is None:
        tokens = theme_tokens(gui)
        bg = parse_color(tokens.get("bg", ""))
        expect_dark = bool(bg and relative_luminance(bg) < 0.2)

    started = time.time()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(out_dir),
        "gui_py": str(REPO_ROOT / "omen_macro" / "gui.py"),
        "gui_py_sha1": None,
        "gui_py_mtime": None,
        "git_head": None,
        "theme": harness.theme_name,
        "gtk_theme_env": args.theme or "",
        "window": {"requested": [args.width, args.height]},
        "safety": {
            "temp_config_root": str(config_root),
            "temp_sysfs_root": str(sysfs_root),
            "temp_profile": config_root != Path("/etc/omen-macro"),
            "persist_stubbed": True,
            "window_opacity": OPACITY,
            "visible_window": False,
            "capture_css": harness.css_installed,
        },
        "contrast": contrast_report(gui),
        "css_checks": css_selector_checks(gui, Gtk),
        "scenarios": [],
        "checks": [],
        "failures": [],
        "warnings": [],
        "skipped": [],
    }
    try:
        gui_path = REPO_ROOT / "omen_macro" / "gui.py"
        report["gui_py_sha1"] = hashlib.sha1(gui_path.read_bytes()).hexdigest()[:16]
        report["gui_py_mtime"] = datetime.fromtimestamp(
            gui_path.stat().st_mtime, timezone.utc).isoformat()
        report["git_head"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        pass

    chosen = selected_scenarios(args.only)
    if not chosen:
        harness.destroy_all()
        shutil.rmtree(temp_root, ignore_errors=True)
        print(f"no scenario matched --only {args.only!r}; "
              f"known: {', '.join(SCENARIO_ORDER)}")
        return 2

    for name in chosen:
        function = SCENARIOS[name]
        entry = {"name": name, "captures": [], "notes": [], "error": ""}
        try:
            result = function(harness, args.width, args.height) or {}
            entry.update({
                "captures": result.get("captures") or [],
                "notes": result.get("notes") or [],
                "analysis": result.get("analysis"),
                "widgets": result.get("widgets"),
                "error": result.get("error") or "",
            })
            report["scenarios"].append(entry)
            checks = scenario_checks(name, result, expect_dark, gui)
            if name == "recording":
                checks += recording_checks(result)
            if name == "banner":
                checks += banner_checks(result)
            if name in ("editor", "editor-single"):
                checks += editor_checks(result)
            if name == "dialogs":
                checks += dialogs_checks(result)
            if name == "editor-error":
                checks += editor_error_checks(result)
            if name == "applied":
                checks += pill_state_checks(result, "APPLIED", "ok")
            if name == "no-hardware":
                checks += pill_state_checks(result, "UNAVAILABLE", "warn")
            if name == "dialog-errors":
                checks += dialog_error_checks(result)
            if name == "states":
                checks += states_checks(result)
            report["checks"].extend(checks)
        except Exception:
            entry["error"] = traceback.format_exc()
            report["scenarios"].append(entry)
            report["checks"].append({
                "name": f"{name}:scenario", "status": "FAIL",
                "detail": entry["error"].strip().splitlines()[-1]})
        finally:
            harness.close_windows()

    harness.destroy_all()
    shutil.rmtree(temp_root, ignore_errors=True)

    report["checks"].extend(report.pop("css_checks", []))

    # ---- contrast table statuses feed the hard failures
    for row in report["contrast"]:
        if row["status"] == "FAIL":
            report["failures"].append(
                f"contrast {row['pair']}: {row['ratio']}:1 < {row['required']}:1")
        elif row["status"] == "WARN" and row.get("detail", "").startswith("advisory"):
            report["warnings"].append(f"contrast {row['pair']}: {row['detail']}")
    for check in report["checks"]:
        if check["status"] == "FAIL":
            report["failures"].append(f"{check['name']}: {check['detail']}")
        elif check["status"] == "WARN":
            report["warnings"].append(f"{check['name']}: {check['detail']}")

    report["elapsed_seconds"] = round(time.time() - started, 1)
    report["passed"] = not report["failures"]
    report_path = Path(args.json).expanduser() if args.json else out_dir / "report.json"
    try:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    except OSError as exc:
        print(f"WARN: could not write {report_path}: {exc}")

    # ---- compact human summary
    print("== OMEN Macro Keys offscreen render ==")
    print(f"out:      {out_dir}")
    print(f"theme:    {report['theme'] or '(unknown)'}"
          + (f" (GTK_THEME={args.theme})" if args.theme else "")
          + (f", gui.THEME tokens" if isinstance(getattr(gui, "THEME", None), dict)
             else ", no gui.THEME (advisory contrast)"))
    print(f"gui.py:   sha1 {report['gui_py_sha1']}  mtime {report['gui_py_mtime']}")
    print(f"safety:   opacity {OPACITY} (invisible), _persist stubbed, "
          f"root {config_root}, sysfs {sysfs_root}")
    print()
    for scenario in report["scenarios"]:
        state = "ERROR" if scenario["error"] else "ok"
        print(f"[{state}] {scenario['name']}")
        for capture in scenario["captures"]:
            if capture.get("error"):
                print(f"        capture failed: {capture['error']}")
                continue
            print(f"        {capture['path']}  "
                  f"window={capture.get('window_size')} "
                  f"image={capture.get('image_size')}"
                  + ("  (size pinned)" if capture.get("size_forced") else ""))
        for note in scenario["notes"]:
            print(f"        note: {note}")
    print()
    print("WCAG contrast (body >= 4.5:1, large >= 3.0:1)")
    for row in report["contrast"]:
        ratio = f"{row['ratio']:.2f}:1" if row.get("ratio") is not None else "n/a"
        print(f"  {row['status']:4} {row['pair']:<28} "
              f"{row.get('fg', '?'):>8} on {row.get('bg', '?'):>8}  "
              f"{ratio:>9}  ({row['kind']})")
    print()
    print("checks not passing:")
    for check in report["checks"]:
        if check["status"] != "PASS":
            print(f"  {check['status']:4} {check['name']}: {check['detail']}")
    passed = sum(1 for check in report["checks"] if check["status"] == "PASS")
    print(f"checks passed: {passed}/{len(report['checks'])}")
    if report["warnings"]:
        print(f"warnings ({len(report['warnings'])}):")
        for warning in report["warnings"][:12]:
            print(f"  - {warning}")
    if report["failures"]:
        print(f"FAILURES ({len(report['failures'])}):")
        for failure in report["failures"][:16]:
            print(f"  - {failure}")
        print(f"report: {report_path}")
        if args.report_only:
            print("--report-only: exiting 0 despite hard failures")
            return 0
        return 1
    print(f"PASS: all hard checks passed; report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
