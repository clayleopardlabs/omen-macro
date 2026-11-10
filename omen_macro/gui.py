#!/usr/bin/env python3
"""GTK4 configuration UI for the HP OMEN macro keys (P1..P6).

Launch
------
    python3 -m omen_macro.gui              # from the project root
    python3 omen_macro/gui.py              # equivalent
    python3 -m omen_macro.gui --selftest   # headless logic check, no display

Why a GUI: the six macro keys left of the keyboard are dead until the embedded
controller (EC) is handed a 4096 byte macro table.  The patched hp-wmi module
exposes that table as a write-only binary sysfs file, so the only thing this
program does to the hardware is build the table (``codec.config_to_profile``)
and write it in one syscall.

``import omen_macro.gui`` is safe on a headless machine: no GTK object is
constructed until ``main()`` runs, so the module can be imported for testing or
by tooling without a display.

Recording model
---------------
A macro is an ordered list of *chords*; a chord is the set of keys held at the
same time, in press order.  Recording stores key order only, never timing:
holding Ctrl and pressing C is one chord ``["Control_L", "c"]``; after both are
released the next press starts a new chord.  Escape ends recording (and is not
stored), Backspace removes the last chord.

Files
-----
    profile written to  /sys/devices/platform/hp-wmi/macro_profile
                        (override the directory with OMEN_MACRO_SYSFS)
    status read from    /sys/devices/platform/hp-wmi/macro_status
    config saved to     /etc/omen-macro/profile.json (the file apply.py and the
                        boot unit read); falls back to ~/.config/omen-macro/
                        profile.json only while /etc/omen-macro does not exist.
                        OMEN_MACRO_CONFIG overrides the full file path.

    The OMEN logo key is not an EC slot: it always reports KEY_PROG2
    (XF86Launch2). The GUI configures it by owning one GNOME custom shortcut
    that runs a command of your choice (see omen_key.py).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gtk, Pango  # noqa: E402

# Allow both "python3 -m omen_macro.gui" and "python3 omen_macro/gui.py".
_PKG_PARENT = Path(__file__).resolve().parent.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from omen_macro import codec  # noqa: E402
from omen_macro import omen_key  # noqa: E402

APP_ID = "io.github.omenmacro.Gui"
WINDOW_TITLE = "OMEN Macro Keys"

DEFAULT_SYSFS = "/sys/devices/platform/hp-wmi"
PROFILE_ATTR = "macro_profile"
STATUS_ATTR = "macro_status"
SYSFS_ENV = "OMEN_MACRO_SYSFS"

DEFAULT_SPACING_MS = 20
MAX_SPACING_MS = 2040  # codec.build_profile() ceiling

DEFAULT_CONFIG = Path("/etc/omen-macro/profile.json")
FALLBACK_CONFIG = Path.home() / ".config" / "omen-macro" / "profile.json"
CONFIG_ENV = "OMEN_MACRO_CONFIG"

MSG_NO_HW = "hardware interface not found - run install.sh"
MSG_NO_PERM = "permission denied - run install.sh to grant omen-hw write access"

USAGE = """\
OMEN Macro Keys - configure P1..P6 on an HP OMEN laptop

Usage:
  python3 -m omen_macro.gui              launch the GTK4 window
  python3 -m omen_macro.gui --selftest   run the headless recording self-check
  python3 -m omen_macro.gui --help       show this text

Environment:
  OMEN_MACRO_SYSFS   directory holding macro_profile / macro_status
                     (default /sys/devices/platform/hp-wmi)
  OMEN_MACRO_CONFIG  full path of the saved profile.json
                     (default /etc/omen-macro/profile.json, or
                     ~/.config/omen-macro/profile.json while /etc/omen-macro
                     does not exist)
"""

CSS = """
.macro-key {
    font-weight: bold;
}
.macro-row.recording {
    background-color: rgba(255, 163, 72, 0.22);
    border: 2px solid rgba(255, 163, 72, 0.85);
    border-radius: 6px;
}
.banner {
    padding: 6px 10px;
    border-radius: 6px;
}
.banner.banner-error {
    background-color: rgba(224, 27, 36, 0.18);
}
.banner.banner-warning {
    background-color: rgba(229, 165, 10, 0.20);
}
.status {
    font-family: monospace;
}
"""


# --------------------------------------------------------------------------- #
# sysfs / config helpers (no GTK needed - reused by the selftest)
# --------------------------------------------------------------------------- #
def sysfs_dir() -> Path:
    """Directory holding macro_profile / macro_status, honouring OMEN_MACRO_SYSFS."""
    override = os.environ.get(SYSFS_ENV, "").strip()
    return Path(override) if override else Path(DEFAULT_SYSFS)


def key_name(keyval: int) -> str:
    """GDK keyval -> the key names codec understands ("a", "Control_L", "KP_1").

    The GTK documentation spells this ``Gdk.Keyval.name()``; this PyGObject
    build exposes the same C function (gdk_keyval_name) as the module-level
    ``Gdk.keyval_name()``.  Unknown keyvals come back as hex ("0x1b"), which
    codec.normalise() rejects with a readable message.
    """
    return Gdk.keyval_name(keyval) or ""


def canonical_name(name: str) -> str:
    """codec.normalise() with one practical fallback for Shift+letter.

    GDK reports Shift+A as the keyval "A", but the scan-code tables are keyed by
    the unshifted symbol; with Shift_L already in the chord, encoding "a" gives
    exactly the "A" the EC should replay.  Everything else is passed through so
    the real MacroError (F13, media keys, ...) reaches the user.
    """
    try:
        return codec.normalise(name)
    except codec.MacroError:
        if len(name) == 1 and name.isascii() and name.isupper():
            return codec.normalise(name.lower())
        raise


def write_profile(profile: bytes, sysfs: Path | None = None) -> int:
    """Write the whole table to macro_profile with a single write() syscall.

    The kernel attribute parses the buffer it is handed, so the 4096 bytes must
    arrive in one call - hence buffering=0 and a length check.
    """
    if len(profile) != codec.PROFILE_SIZE:
        raise ValueError(
            f"profile must be {codec.PROFILE_SIZE} bytes, got {len(profile)}"
        )
    path = (sysfs or sysfs_dir()) / PROFILE_ATTR
    with open(path, "wb", buffering=0) as handle:
        written = handle.write(profile)
    if written != len(profile):
        raise OSError(f"short write to {path}: {written} of {len(profile)} bytes")
    return written


def read_status(sysfs: Path | None = None) -> str:
    """Read macro_status; failures are reported as text, never raised."""
    path = (sysfs or sysfs_dir()) / STATUS_ATTR
    try:
        return path.read_text().strip()
    except OSError as exc:
        return f"(cannot read {path}: {exc})"


def config_path() -> Path:
    """Where Save/Load keeps profile.json.

    1. $OMEN_MACRO_CONFIG when set - a full file path, for testing or a
       non-standard install.
    2. /etc/omen-macro/profile.json - install.sh creates that directory
       group-writable by omen-hw, and both apply.py and the boot unit read it.
    3. ~/.config/omen-macro/profile.json, but only while /etc/omen-macro does
       not exist at all (running from a dev checkout before install.sh).

    A PermissionError is deliberately *not* a fallback case: it means the
    install is broken, so the caller shows the real error instead of silently
    saving somewhere else.
    """
    override = os.environ.get(CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if DEFAULT_CONFIG.parent.is_dir():
        return DEFAULT_CONFIG
    return FALLBACK_CONFIG


def load_config(path: Path | None = None) -> tuple[dict, bool]:
    """Return (config, found).  Raises OSError/ValueError if the file is unusable."""
    path = path or config_path()
    if not path.is_file():
        return {"spacing_ms": DEFAULT_SPACING_MS, "slots": {}}, False
    cfg = json.loads(path.read_text())
    if not isinstance(cfg, dict):
        raise ValueError("top level JSON value is not an object")
    return cfg, True


def save_config(cfg: dict, path: Path | None = None) -> Path:
    """Write {"spacing_ms": int, "slots": {name: [[key, ...], ...]}} as JSON.

    Parent directories are not created here: install.sh owns /etc/omen-macro and
    only the ~/.config fallback needs mkdir, which the window does explicitly.
    """
    path = path or config_path()
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Recording state machine - pure Python so it can be tested without a display
# --------------------------------------------------------------------------- #
class Recording:
    """Chord state machine for one macro key.

    State:
        chords  completed chords, in order
        held    raw key names currently down, in press order
        _repeat raw names that arrived while already down (auto-repeat)

    Rules:
        * press of a key already in ``held`` is auto-repeat -> ignored
        * press while nothing is held starts a new (empty) chord
        * press while something is held appends to the current chord, so
          Ctrl-then-C becomes the single chord ["Control_L", "c"]
        * release removes the key from ``held``; when ``held`` becomes empty the
          chord is complete and the next press starts the next chord
        * Escape ends the recording and is never stored
        * Backspace with nothing held drops the last chord
        * a key codec.normalise() rejects is not stored; ``error`` carries the
          MacroError text and recording continues

    GTK callbacks only translate key events into press()/release(); the logic
    above is what --selftest exercises.
    """

    def __init__(self) -> None:
        self.chords: list[list[str]] = []
        self.held: list[str] = []
        self._repeat: set[str] = set()
        self.error = ""
        self.finished = False

    # -- key events --------------------------------------------------------
    def press(self, name: str) -> None:
        if self.finished:
            return
        if name == "Escape":
            self.finished = True
            self.held.clear()
            self._repeat.clear()
            return
        if name in self._repeat:  # auto-repeat of a key we refused
            return
        if name in self.held:  # auto-repeat of a key we recorded
            return
        if name == "BackSpace" and not self.held:
            if self.chords:
                self.chords.pop()
                self.error = ""
            return
        try:
            canonical = canonical_name(name)
        except codec.MacroError as exc:
            self.error = str(exc)
            self._repeat.add(name)
            return
        self.error = ""
        if not self.held:
            self.chords.append([])
        self.chords[-1].append(canonical)
        self.held.append(name)

    def release(self, name: str) -> None:
        self._repeat.discard(name)
        if name in self.held:
            self.held.remove(name)

    def drop_held(self) -> None:
        """Forget held keys, e.g. when the window loses focus mid-chord."""
        self.held.clear()
        self._repeat.clear()

    # -- presentation ------------------------------------------------------
    def summary(self) -> str:
        """Live text for the row, e.g. "Recording… Ctrl+c, v"."""
        if not self.chords:
            return "Recording… (waiting for keys)"
        parts = [codec.describe_chord(chord) for chord in self.chords if chord]
        return "Recording… " + ", ".join(parts)


# --------------------------------------------------------------------------- #
# GTK4 window
# --------------------------------------------------------------------------- #
class MacroWindow(Gtk.ApplicationWindow):
    """One row per slot, a spacing spin button, apply/save controls and status."""

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title=WINDOW_TITLE)
        self.set_default_size(960, 720)

        self.slots: dict[str, list[list[str]]] = {}
        self.rows: dict[str, dict[str, Gtk.Widget]] = {}
        self.omen_command: str = omen_key.OMEN_DEFAULT_COMMAND
        self.recording: Recording | None = None
        self.recording_slot: str | None = None
        self._config_line = f"config: {config_path()}"

        self._build_ui()
        self._add_key_controller()
        if Gtk.Window.find_property("is-active") is not None:
            self.connect("notify::is-active", self._on_active_changed)

        self._refresh_all()
        self._load_config()
        self._refresh_omen_status()
        self._set_status(
            "\n".join(
                [
                    self._device_line(),
                    self._config_line,
                    "Nothing has been applied to the EC yet.",
                ]
            )
        )

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(12)
        self.set_child(root)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._build_content())
        root.append(scroller)

        self.banner = Gtk.Label(label="", xalign=0)
        self.banner.set_wrap(True)
        self.banner.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.banner.set_selectable(True)
        self.banner.add_css_class("banner")
        self.banner.set_visible(False)
        root.append(self.banner)

        root.append(self._build_status_area())
        root.append(self._build_footer())

    def _build_content(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        title = Gtk.Label(label=WINDOW_TITLE, xalign=0)
        title.add_css_class("title-2")
        content.append(title)

        subtitle = Gtk.Label(xalign=0)
        subtitle.set_wrap(True)
        subtitle.add_css_class("dim-label")
        subtitle.set_text(
            "Each macro key replays a macro stored in the embedded controller. "
            "Recording keeps key order only; keys held together count as one chord."
        )
        content.append(subtitle)

        warning = Gtk.Label(xalign=0)
        warning.set_wrap(True)
        warning.add_css_class("banner")
        warning.add_css_class("banner-warning")
        warning.set_text(
            "The EC replays PS/2 scan codes, so it cannot emit F13-F24 or "
            "media/ACPI keys. Use the modifier slots for Ctrl/Alt/Shift/Fn "
            "combinations (they are not recorded as part of a chord)."
        )
        content.append(warning)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        main_list = Gtk.ListBox()
        main_list.set_selection_mode(Gtk.SelectionMode.NONE)
        main_list.set_show_separators(True)
        for slot in codec.PRIMARY_SLOTS:
            self._add_row(main_list, slot)
        self._add_omen_row(main_list)
        content.append(main_list)

        content.append(self._build_spacing_row())
        content.append(self._build_advanced())
        return content

    def _add_row(self, listbox: Gtk.ListBox, slot: str) -> None:
        """Append one [name | summary | Record | Clear] row to ``listbox``."""
        row = Gtk.ListBoxRow()
        row.add_css_class("macro-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(4)

        name_label = Gtk.Label(label=slot, xalign=0)
        name_label.add_css_class("macro-key")
        name_label.set_size_request(96, -1)

        summary = Gtk.Label(label="empty", xalign=0)
        summary.set_hexpand(True)
        summary.set_ellipsize(Pango.EllipsizeMode.END)

        record = Gtk.Button(label="Record")
        record.set_tooltip_text(f"Record the macro for {slot}")
        record.connect("clicked", lambda _btn, s=slot: self._on_record_clicked(s))

        clear = Gtk.Button(label="Clear")
        clear.set_tooltip_text(f"Remove the macro for {slot}")
        clear.connect("clicked", lambda _btn, s=slot: self._on_clear_clicked(s))

        for widget in (name_label, summary, record, clear):
            box.append(widget)
        row.set_child(box)
        listbox.append(row)
        self.rows[slot] = {
            "row": row,
            "summary": summary,
            "record": record,
            "clear": clear,
        }

    def _build_spacing_row(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)

        label = Gtk.Label(label="Spacing between key presses (ms)", xalign=0)
        label.set_hexpand(True)

        self.spacing = Gtk.SpinButton.new_with_range(0, MAX_SPACING_MS, 10)
        self.spacing.set_digits(0)
        self.spacing.set_numeric(True)
        self.spacing.set_value(DEFAULT_SPACING_MS)
        self.spacing.set_tooltip_text(
            "Delay the EC inserts between chords when it replays the macro "
            f"(0-{MAX_SPACING_MS} ms)."
        )

        box.append(label)
        box.append(self.spacing)
        return box

    def _build_advanced(self) -> Gtk.Widget:
        expander = Gtk.Expander(label="Advanced: Ctrl / Alt / Shift / Fn + P1-P6")
        expander.set_expanded(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(6)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        hint.set_text(
            "These slots fire when the macro key is pressed together with the "
            "modifier. They are independent of the Ctrl/Alt/Shift/Fn keys "
            "recorded inside a chord."
        )
        inner.append(hint)

        slots_list = Gtk.ListBox()
        slots_list.set_selection_mode(Gtk.SelectionMode.NONE)
        for slot in codec.SLOT_NAMES[6:]:
            self._add_row(slots_list, slot)
        inner.append(slots_list)

        expander.set_child(inner)
        return expander

    def _add_omen_row(self, listbox: Gtk.ListBox) -> None:
        """Append the OMEN logo-key row, same layout as the P-key rows.

        The logo key is not an EC slot (it always sends XF86Launch2), so
        instead of recording a macro, Set opens a dialog for the command the
        key should run. Clear removes the GNOME shortcut.
        """
        row = Gtk.ListBoxRow()
        row.add_css_class("macro-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(4)

        name_label = Gtk.Label(label="OMEN", xalign=0)
        name_label.add_css_class("macro-key")
        name_label.set_size_request(96, -1)

        summary = Gtk.Label(label="no binding", xalign=0)
        summary.set_hexpand(True)
        summary.set_ellipsize(Pango.EllipsizeMode.END)

        set_btn = Gtk.Button(label="Set")
        set_btn.set_tooltip_text("Set the command the OMEN logo key runs")
        set_btn.connect("clicked", self._on_omen_set_clicked)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.set_tooltip_text("Remove the OMEN key shortcut")
        clear_btn.connect("clicked", self._on_omen_remove_clicked)

        for widget in (name_label, summary, set_btn, clear_btn):
            box.append(widget)
        row.set_child(box)
        listbox.append(row)
        self.omen_row = {
            "row": row,
            "summary": summary,
            "set": set_btn,
            "clear": clear_btn,
        }

    def _refresh_omen_status(self) -> None:
        row = getattr(self, "omen_row", None)
        if row is None:
            return
        try:
            data = omen_key.get_omen_binding()
            paths = omen_key.get_custom_paths()
        except OSError as exc:
            text = f"could not read shortcuts ({exc})"
            row["summary"].set_text(text)
            row["summary"].set_tooltip_text(text)
            return
        if omen_key.OMEN_PATH not in paths or not data or not data.get("command"):
            text = "no binding (pressing it does nothing)"
        else:
            text = omen_key.describe_binding(data)
        row["summary"].set_text(text)
        row["summary"].set_tooltip_text(text)

    def _on_omen_set_clicked(self, _button) -> None:
        dialog = Gtk.Window(transient_for=self, modal=True, title="OMEN key command")
        dialog.set_default_size(420, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)

        label = Gtk.Label(xalign=0)
        label.set_wrap(True)
        label.set_text(
            "Command run when the OMEN logo key is pressed (it sends XF86Launch2)."
        )
        box.append(label)

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        current = (self.omen_command or "").strip()
        if not current:
            try:
                data = omen_key.get_omen_binding()
                if data and data.get("command"):
                    current = data["command"]
            except OSError:
                pass
        entry.set_text(current or omen_key.OMEN_DEFAULT_COMMAND)
        box.append(entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        ok = Gtk.Button(label="Bind")
        ok.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        dialog.set_child(box)

        def done(bind: bool) -> None:
            if bind:
                command = entry.get_text().strip()
                if not command:
                    self._set_banner("OMEN key: type a command first.", "banner-error")
                    return
                try:
                    omen_key.set_omen_key(command)
                except (OSError, ValueError) as exc:
                    self._set_banner(
                        f"OMEN key: could not save the shortcut ({exc})", "banner-error"
                    )
                    return
                self.omen_command = command
                self._set_banner("", "")
                self._refresh_omen_status()
                self._set_status(
                    f"OMEN key now runs {command!r} (saved to GNOME shortcuts)."
                )
            dialog.close()

        cancel.connect("clicked", lambda _b: done(False))
        ok.connect("clicked", lambda _b: done(True))
        entry.connect("activate", lambda _e: done(True))
        dialog.present()

    def _on_omen_remove_clicked(self, _button) -> None:
        try:
            omen_key.clear_omen_key()
        except OSError as exc:
            self._set_banner(f"OMEN key: could not remove the shortcut ({exc})", "banner-error")
            return
        self._set_banner("", "")
        self._refresh_omen_status()
        self._set_status("OMEN key binding removed.")

    def _build_status_area(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="EC status")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)

        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_wrap(True)
        self.status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status.set_selectable(True)
        self.status.add_css_class("status")

        box.append(self.status)
        frame.set_child(box)
        return frame

    def _build_footer(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.apply_btn = Gtk.Button(label="Apply to EC")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_tooltip_text(
            "Encode the table and write all 4096 bytes to the EC now."
        )
        self.apply_btn.connect("clicked", self._on_apply_clicked)

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.set_tooltip_text(f"Save the configuration to {config_path()}")
        self.save_btn.connect("clicked", self._on_save_clicked)

        self.save_apply_btn = Gtk.Button(label="Save & Apply")
        self.save_apply_btn.set_tooltip_text("Save the configuration, then write it to the EC")
        self.save_apply_btn.connect("clicked", self._on_save_apply_clicked)

        self.defaults_btn = Gtk.Button(label="Load defaults")
        self.defaults_btn.set_tooltip_text("Load the stock numpad layout as a starting point")
        self.defaults_btn.connect("clicked", self._on_defaults_clicked)

        self.stop_btn = Gtk.Button(label="Stop recording")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_tooltip_text("Finish recording the current row")
        self.stop_btn.set_visible(False)
        self.stop_btn.connect("clicked", lambda _btn: self._finish_recording())

        for widget in (self.apply_btn, self.save_btn, self.save_apply_btn, self.defaults_btn):
            bar.append(widget)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        bar.append(self.stop_btn)
        return bar

    def _add_key_controller(self) -> None:
        """Capture every key before shortcuts/widgets see it (capture phase)."""
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        controller.connect("key-released", self._on_key_released)
        self.add_controller(controller)

    # -- key events --------------------------------------------------------
    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        rec = self.recording
        if rec is None:
            return False  # not recording: leave the event alone
        rec.press(key_name(keyval))
        if rec.finished:
            self._finish_recording()
            return True
        self._sync_recording_ui()
        return True

    def _on_key_released(self, _controller, keyval, _keycode, _state) -> bool:
        rec = self.recording
        if rec is None:
            return False
        rec.release(key_name(keyval))
        self._sync_recording_ui()
        return True

    def _on_active_changed(self, window: Gtk.Window, _param) -> None:
        # Key releases never arrive after focus loss, so close the open chord.
        if window.props.is_active or self.recording is None:
            return
        self.recording.drop_held()
        self._sync_recording_ui()

    # -- recording ---------------------------------------------------------
    def _on_record_clicked(self, slot: str) -> None:
        if self.recording_slot == slot:
            self._finish_recording()
        else:
            self._start_recording(slot)

    def _start_recording(self, slot: str) -> None:
        if self.recording_slot is not None:
            self._finish_recording()
        self.recording = Recording()
        self.recording_slot = slot

        row = self.rows[slot]
        row["row"].add_css_class("recording")
        row["record"].set_label("Stop")
        row["record"].add_css_class("destructive-action")
        self.stop_btn.set_visible(True)
        row["record"].grab_focus()

        self._sync_recording_ui()
        self._set_status(
            f"Recording {slot}: hold keys together for one chord, release them "
            "for the next. Escape or Stop ends recording."
        )

    def _finish_recording(self) -> None:
        if self.recording_slot is None or self.recording is None:
            return
        slot = self.recording_slot
        rec = self.recording
        self.recording = None
        self.recording_slot = None

        row = self.rows[slot]
        row["row"].remove_css_class("recording")
        row["record"].set_label("Record")
        row["record"].remove_css_class("destructive-action")
        row["record"].grab_focus()
        self.stop_btn.set_visible(False)
        self._set_banner("", "")

        chords = [list(chord) for chord in rec.chords if chord]
        if chords:
            self.slots[slot] = chords
            self._set_status(
                f"{slot} = {codec.describe_entry(chords)} "
                "(not applied yet - press Apply to EC)"
            )
        else:
            self._set_status(f"{slot}: no keys captured, nothing changed.")
        self._refresh_row(slot)

    def _on_clear_clicked(self, slot: str) -> None:
        if self.recording_slot == slot:
            self._finish_recording()
        if self.slots.pop(slot, None) is None:
            self._set_status(f"{slot} was already empty.")
        else:
            self._set_status(f"{slot} cleared (not applied yet - press Apply to EC)")
        self._refresh_row(slot)

    def _sync_recording_ui(self) -> None:
        slot, rec = self.recording_slot, self.recording
        if slot is None or rec is None:
            return
        text = rec.summary()
        row = self.rows[slot]
        row["summary"].set_text(text)
        row["summary"].set_tooltip_text(text)
        if rec.error:
            self._set_banner(f"{slot}: {rec.error}", "banner-error")
        else:
            self._set_banner("", "")

    # -- rows --------------------------------------------------------------
    def _refresh_all(self) -> None:
        for slot in self.rows:
            self._refresh_row(slot)

    def _refresh_row(self, slot: str) -> None:
        if self.recording_slot == slot:
            return  # the live recording text owns the label
        row = self.rows[slot]
        text = codec.describe_entry(self.slots.get(slot) or [])
        row["summary"].set_text(text)
        row["summary"].set_tooltip_text(
            f"{slot} is empty" if text == "empty" else text
        )

    # -- config ------------------------------------------------------------
    def _config(self) -> dict:
        slots = {
            name: [list(chord) for chord in chords]
            for name, chords in self.slots.items()
            if chords
        }
        cfg: dict = {"spacing_ms": int(self.spacing.get_value()), "slots": slots}
        command = (getattr(self, "omen_command", "") or "").strip()
        if command:
            cfg["omen_key"] = {"command": command, "name": omen_key.OMEN_DEFAULT_NAME}
        return cfg

    @staticmethod
    def _clean_chords(slot: str, chords) -> list[list[str]]:
        if not isinstance(chords, list):
            raise codec.MacroError(f"{slot}: expected a list of chords")
        out: list[list[str]] = []
        for chord in chords:
            if not isinstance(chord, (list, tuple)) or not chord:
                continue
            try:
                out.append([codec.normalise(str(key)) for key in chord])
            except codec.MacroError as exc:
                raise codec.MacroError(f"{slot}: {exc}") from None
        return out

    def _apply_config_dict(self, cfg: dict) -> None:
        problems: list[str] = []

        try:
            spacing = int(cfg.get("spacing_ms", DEFAULT_SPACING_MS))
        except (TypeError, ValueError):
            spacing = DEFAULT_SPACING_MS
            problems.append("spacing_ms is not an integer")
        self.spacing.set_value(max(0, min(MAX_SPACING_MS, spacing)))

        self.slots = {}
        raw_slots = cfg.get("slots")
        if not isinstance(raw_slots, dict):
            raw_slots = {}
        for name in codec.SLOT_NAMES:
            raw = raw_slots.get(name)
            if not raw:
                continue
            try:
                cleaned = self._clean_chords(name, raw)
            except codec.MacroError as exc:
                problems.append(str(exc))
                continue
            if cleaned:
                self.slots[name] = cleaned

        raw_omen = cfg.get("omen_key")
        if isinstance(raw_omen, dict) and isinstance(raw_omen.get("command"), str):
            command = raw_omen["command"].strip()
            if command:
                self.omen_command = command
        elif raw_omen is not None:
            problems.append("omen_key is not an object with a command string")

        self._refresh_all()
        self._refresh_omen_status()
        if problems:
            self._set_banner(
                "Ignored invalid config entries: " + "; ".join(problems),
                "banner-error",
            )
        else:
            self._set_banner("", "")

    def _config_status(self, path: Path, state: str) -> str:
        line = f"config: {path} ({state})"
        if path == FALLBACK_CONFIG:
            line += " - fallback, /etc/omen-macro is missing (run install.sh)"
        return line

    def _load_config(self) -> None:
        path = config_path()
        try:
            cfg, found = load_config(path)
        except PermissionError as exc:
            self._config_line = self._config_status(path, "permission denied")
            self._set_banner(
                f"permission denied reading {path}: {exc}", "banner-error"
            )
            return
        except (OSError, ValueError) as exc:
            self._config_line = self._config_status(path, "unreadable, starting empty")
            self._set_banner(f"Could not read {path}: {exc}", "banner-error")
            return
        if not found:
            self._config_line = self._config_status(path, "none yet, starting empty")
            return
        self._apply_config_dict(cfg)
        self._config_line = self._config_status(path, "loaded")

    # -- hardware / saving -------------------------------------------------
    def _device_line(self) -> str:
        path = sysfs_dir() / PROFILE_ATTR
        state = "present" if path.exists() else MSG_NO_HW
        return f"device: {path} ({state})"

    def _on_apply_clicked(self, _button) -> None:
        self._finish_recording()
        sysfs = sysfs_dir()
        path = sysfs / PROFILE_ATTR
        try:
            profile = codec.config_to_profile(self._config())
        except codec.MacroError as exc:
            self._set_banner(f"Cannot encode the macro table: {exc}", "banner-error")
            self._set_status("Profile not written.")
            return

        try:
            written = write_profile(profile, sysfs)
        except (FileNotFoundError, NotADirectoryError):
            self._set_banner(MSG_NO_HW, "banner-error")
            self._set_status(f"{path} does not exist.")
            return
        except PermissionError:
            self._set_banner(MSG_NO_PERM, "banner-error")
            self._set_status(f"Permission denied writing {path}.")
            return
        except OSError as exc:
            self._set_banner(f"EC rejected the profile: {exc}", "banner-error")
            self._set_status(f"macro_status: {read_status(sysfs)}")
            return

        self._set_banner("", "")
        self._set_status(
            f"wrote {written} bytes to {path}\nmacro_status: {read_status(sysfs)}"
        )

    def _on_save_clicked(self, _button) -> bool:
        self._finish_recording()
        cfg = self._config()
        path = config_path()
        try:
            if path == FALLBACK_CONFIG:
                # Only the dev-checkout fallback is ours to create;
                # /etc/omen-macro belongs to install.sh.
                path.parent.mkdir(parents=True, exist_ok=True)
            path = save_config(cfg, path)
        except PermissionError as exc:
            # Never fall back quietly here: a permission error means the
            # install is broken and the user must see the real error.
            self._set_banner(
                f"permission denied writing {path}: {exc}", "banner-error"
            )
            self._set_status(f"{path} was not written.")
            return False
        except OSError as exc:
            self._set_banner(f"Could not save {path}: {exc}", "banner-error")
            self._set_status(f"{path} was not written.")
            return False
        self._set_banner("", "")
        count = sum(len(chords) for chords in cfg["slots"].values())
        self._set_status(f"saved {count} chord(s) in {len(cfg['slots'])} slot(s) to {path}")
        return True

    def _on_save_apply_clicked(self, _button) -> None:
        if self._on_save_clicked(_button):
            self._on_apply_clicked(_button)

    def _on_defaults_clicked(self, _button) -> None:
        self._finish_recording()
        self.slots = {
            name: [list(chord) for chord in chords]
            for name, chords in codec.NUMPAD_DEFAULT.items()
        }
        self.spacing.set_value(DEFAULT_SPACING_MS)
        self._refresh_all()
        self._set_banner("", "")
        self._set_status(
            "Loaded the stock numpad layout (not applied yet - press Apply to EC)."
        )

    # -- status ------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _set_banner(self, text: str, css_class: str = "") -> None:
        if not text:
            self.banner.set_visible(False)
            self.banner.set_text("")
            return
        self.banner.remove_css_class("banner-error")
        self.banner.remove_css_class("banner-warning")
        if css_class:
            self.banner.add_css_class(css_class)
        self.banner.set_text(text)
        self.banner.set_visible(True)


class MacroApp(Gtk.Application):
    """Gtk.Application wrapper; all GTK construction happens here or below."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self._window: MacroWindow | None = None
        self._css_installed = False

    def do_activate(self) -> None:  # noqa: N802 - GTK vfunc name
        self._install_css()
        if self._window is None:
            self._window = MacroWindow(self)
        self._window.present()

    def _install_css(self) -> None:
        if self._css_installed:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self._css_installed = True


# --------------------------------------------------------------------------- #
# Headless self-check
# --------------------------------------------------------------------------- #
def selftest() -> int:
    """Exercise the recording state machine and compare it with the encoder.

    The important assertion: pressing Ctrl, then C (Ctrl still held), releasing
    both, then pressing v must produce exactly the profile bytes that
    codec.build_profile() yields for [["Control_L", "c"], ["v"]], i.e. the same
    bytes a hand-assembled table would contain.
    """
    failures: list[str] = []
    count = 0

    def check(label: str, got, want) -> None:
        nonlocal count
        count += 1
        if got == want:
            print(f"ok   {label}")
        else:
            failures.append(label)
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    # 1. Ctrl+C then v: Ctrl and C become one chord because Ctrl is still held.
    rec = Recording()
    for action, name in [
        ("press", "Control_L"),
        ("press", "c"),
        ("release", "c"),
        ("release", "Control_L"),
        ("press", "v"),
        ("release", "v"),
    ]:
        (rec.press if action == "press" else rec.release)(name)
    check("Ctrl+C then v -> one chord + one chord", rec.chords, [["Control_L", "c"], ["v"]])

    # 2. Auto-repeat of a held key must not duplicate it.
    repeat = Recording()
    repeat.press("a")
    repeat.press("a")
    repeat.press("a")
    check("auto-repeat ignored", repeat.chords, [["a"]])

    # 3. Unsupported key: refused, reported, recording continues.
    bad = Recording()
    bad.press("F13")
    check("F13 not recorded", bad.chords, [])
    check("F13 reported", "F13" in bad.error, True)
    bad.press("b")
    check("recording continues after F13", bad.chords, [["b"]])

    # 4. Backspace with nothing held drops the last chord.
    undo = Recording()
    for name in ("a", "b"):
        undo.press(name)
        undo.release(name)
    undo.press("BackSpace")
    check("Backspace drops last chord", undo.chords, [["a"]])

    # 5. Backspace with a modifier held is a normal key in the chord.
    combo = Recording()
    combo.press("Control_L")
    combo.press("BackSpace")
    check("Ctrl+Backspace is a chord", combo.chords, [["Control_L", "BackSpace"]])

    # 6. Escape stops recording and is never stored.
    esc = Recording()
    esc.press("a")
    esc.press("Escape")
    esc.press("b")
    check("Escape finishes, not recorded", (esc.finished, esc.chords), (True, [["a"]]))

    # 7. Same keys held in reverse order keep press order.
    order = Recording()
    order.press("Shift_L")
    order.press("Control_L")
    order.press("t")
    check("press order kept", order.chords, [["Shift_L", "Control_L", "t"]])

    # 8. The bytes: state machine output == explicit chord list == hand assembly.
    want_chords = [["Control_L", "c"], ["v"]]
    got_profile = codec.build_profile({"P1": rec.chords}, DEFAULT_SPACING_MS)
    want_profile = codec.build_profile({"P1": want_chords}, DEFAULT_SPACING_MS)
    check("profile from recording matches reference", got_profile, want_profile)

    chord1 = codec.encode_chord(["Control_L", "c"])
    chord2 = codec.encode_chord(["v"])
    print(f"     encode_chord(['Control_L','c']) = {chord1.hex()}")
    print(f"     encode_chord(['v'])            = {chord2.hex()}")
    print(f"     encode_entry(chords, 20)       = {codec.encode_entry(want_chords, 20).hex()}")
    print(f"     profile[0:39]                  = {got_profile[:39].hex()}")

    # Hand-assembled expectation: P1 entry, then 29 empty slots (0x01 each) plus
    # the trailing 0x01 HP also emits -> 30 x 0x01, then zero padding.
    expected_prefix = (
        "09"  # length byte: 8 payload bytes + itself
        "1d"  # make Control_L
        "2e"  # make c
        "ae"  # release c  (0x2E | 0x80)
        "9d"  # release Control_L (0x1D | 0x80)
        "ff14"  # delay marker, 20 ms
        "2f"  # make v
        "af"  # release v
    ) + "01" * 30
    check("profile prefix is hand-assembled bytes", got_profile[:39].hex(), expected_prefix)
    check("chord 1 bytes", chord1.hex(), "1d2eae9d")
    check("profile is 4096 bytes", len(got_profile), codec.PROFILE_SIZE)
    check("profile zero padded", set(got_profile[39:]), {0})

    # 9. OMEN logo key: shortcut list helpers never clobber other entries.
    others = ["/org/example/a/", "/org/example/b/"]
    check("omen path appended once",
          omen_key.ensure_omen_path(others), others + [omen_key.OMEN_PATH])
    check("omen path idempotent",
          omen_key.ensure_omen_path(others + [omen_key.OMEN_PATH]),
          others + [omen_key.OMEN_PATH])
    check("omen path removal keeps others",
          omen_key.remove_omen_path(others + [omen_key.OMEN_PATH]), others)
    check("omen payload binds XF86Launch2",
          omen_key.binding_payload("OMEN Key", "omen-macro"),
          {"name": "OMEN Key", "command": "omen-macro", "binding": "XF86Launch2"})

    print()
    if failures:
        print(f"SELFTEST FAILED ({len(failures)} of {count} checks): {', '.join(failures)}")
        return 1
    print(f"SELFTEST PASSED: {count} checks")
    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if args and args[0] == "--selftest":
        return selftest()
    if args:
        print(f"unknown argument: {args[0]}\n\n{USAGE}", file=sys.stderr)
        return 2
    return MacroApp().run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
