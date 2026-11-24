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
    profiles live in    <root>/profiles/<name>.json, active pointer in
                        <root>/active-profile (root is /etc/omen-macro, or
                        ~/.config/omen-macro before install; OMEN_MACRO_ROOT
                        overrides). Every edit saves the current profile and
                        sends it to the EC immediately - there is no separate
                        save step.

    The OMEN logo key is not an EC slot: it always reports KEY_PROG2
    (XF86Launch2). Any key (OMEN or P1..P6) can instead run a command through
    one GNOME custom shortcut per key (see omen_key.py); a command-bound
    P-key's typing macro is left out of the EC upload so the two do not fight.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

# Allow both "python3 -m omen_macro.gui" and "python3 omen_macro/gui.py".
_PKG_PARENT = Path(__file__).resolve().parent.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from omen_macro import auto_switch  # noqa: E402
from omen_macro import boot_diag  # noqa: E402
from omen_macro import codec  # noqa: E402
from omen_macro import omen_key  # noqa: E402
from omen_macro import profiles  # noqa: E402
from omen_macro.ec import (  # noqa: E402
    DEFAULT_SYSFS,
    PROFILE_ATTR,
    STATUS_ATTR,
    SYSFS_ENV,
    read_status,
    sysfs_dir,
    upload_config,
    write_profile,
)

APP_ID = "io.github.omenmacro.Gui"
WINDOW_TITLE = "OMEN Macro Keys"

DEFAULT_SPACING_MS = 20
MAX_SPACING_MS = 2040  # codec.build_profile() ceiling

# The bare fact; boot_diag supplies the reason (Secure Boot, missing module, ...).
MSG_NO_HW = "hardware interface not found"
MSG_NO_PERM = "permission denied - run install.sh to grant omen-hw write access"

USAGE = """\
OMEN Macro Keys - configure P1..P6 on an HP OMEN laptop

Usage:
  python3 -m omen_macro.gui              launch the GTK4 window
  python3 -m omen_macro.gui --selftest   run the headless self-check
  python3 -m omen_macro.gui --help       show this text

Environment:
  OMEN_MACRO_SYSFS   directory holding macro_profile / macro_status
                     (default /sys/devices/platform/hp-wmi)
  OMEN_MACRO_ROOT    config root holding profiles/ and active-profile
                     (default /etc/omen-macro, or ~/.config/omen-macro
                     while /etc/omen-macro does not exist)
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


KEY_CHOICES = sorted(codec.SCANCODES.keys(), key=str.lower)


def build_chord(modifiers: list[str], key: str) -> list[str]:
    """Compose one chord from modifier names plus a main key, validated.

    Raises codec.MacroError for an empty key, a duplicate of a held modifier,
    or anything the EC cannot replay. Callers pass modifiers in press order
    (Ctrl, Alt, Shift, Super). Pure: the selftest and the Keys dialog share it.
    """
    mods = [canonical_name(m) for m in modifiers]
    main = canonical_name((key or "").strip()) if (key or "").strip() else ""
    if not main:
        raise codec.MacroError("pick a key for the chord")
    if main in mods:
        raise codec.MacroError(f"{main} is already held as a modifier")
    return mods + [main]


# --------------------------------------------------------------------------- #
# Step editor helpers (no GTK needed - reused by the dialog and the selftest)
#
# Recording turns every key that is down at the same moment into one chord, so
# two keys pressed one after the other are sometimes stored together ("h+e").
# The editor shows one step per row as text, which makes that bad step
# retypeable: pressing Split turns it into the two steps "h" and "e" without
# touching the rest of the macro.
# --------------------------------------------------------------------------- #
def format_editor_entry(chord: list[str]) -> str:
    """Editor spelling of one step ("Ctrl+c"), or "" for an empty step.

    Uses codec.format_chord() so parse_editor_entries() reads the text back to
    exactly the same chord; the dialog round-trips a saved macro through it.
    """
    if not chord:
        return ""
    return codec.format_chord(chord)


def parse_editor_entries(texts: list[str]) -> list[list[str]]:
    """Flatten the editor's per-row text into macro steps, in order.

    A row may hold more than one step ("h e" or '"Hi!"'), so rows are parsed
    with codec.parse_steps() and concatenated.  Blank rows (the "Add step"
    placeholder) are skipped, so an editor with only blank rows means "clear
    the slot".  Errors name the 1-based row to fix, e.g.
    "step 3: unsupported key 'Bogus': ...", instead of a position in the
    flattened list.
    """
    steps: list[list[str]] = []
    for index, text in enumerate(texts, start=1):
        try:
            steps.extend(codec.parse_steps(text))
        except codec.MacroError as exc:
            raise codec.MacroError(f"step {index}: {exc}") from None
    return steps


def _payload_size(steps: list[list[str]], spacing_ms: int) -> int:
    """Payload bytes codec.encode_entry() would write, without the size limit.

    Kept separate so editor_preview() can still show how far over the limit a
    macro is; codec.encode_entry() only raises, it does not report the size.
    """
    size = 0
    for index, chord in enumerate(steps):
        size += len(codec.encode_chord(chord))
        if index != len(steps) - 1:
            size += len(codec.encode_delay(int(spacing_ms)))
    return size


def editor_preview(steps: list[list[str]], spacing_ms: int) -> str:
    """One-line status for the editor: step count, payload budget and steps.

    Example: "3 steps, 10/254 bytes: h, e, Ctrl+c".  Never raises: an unknown
    key or an over-long macro is described in the text so the dialog can show
    the problem while the user is still typing.
    """
    count = len(steps)
    label = f"{count} step" if count == 1 else f"{count} steps"
    if not steps:
        return f"{label} - OK clears the slot"
    try:
        payload = _payload_size(steps, spacing_ms)
    except codec.MacroError as exc:
        return f"{label}: {exc}"
    summary = codec.describe_entry(steps)
    text = f"{label}, {payload}/{codec.MAX_ENTRY_PAYLOAD} bytes: {summary}"
    if payload > codec.MAX_ENTRY_PAYLOAD:
        over = payload - codec.MAX_ENTRY_PAYLOAD
        return (f"{text} - macro too long by {over} bytes, remove some "
                "keystrokes")
    return text


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
    """Profile switcher, one row per slot, auto-save/apply and status."""

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title=WINDOW_TITLE)
        self.set_default_size(960, 760)

        self.slots: dict[str, list[list[str]]] = {}
        self.rows: dict[str, dict[str, Gtk.Widget]] = {}
        self.commands: dict[str, str] = {}
        self.recording: Recording | None = None
        self.recording_slot: str | None = None
        self._root = profiles.config_root()
        self._profile_name = ""
        self._loading = False
        self._combo_quiet = False
        self._spacing_timer = 0
        self._auto_quiet = False
        self._auto_rules: list[dict] = []
        self._auto_fallback: str | None = None

        self._build_ui()
        self._add_key_controller()
        if Gtk.Window.find_property("is-active") is not None:
            self.connect("notify::is-active", self._on_active_changed)

        self._refresh_all()
        self._load_store()
        self._refresh_omen_status()
        self._refresh_auto_status()
        diagnosis = self._hardware_diagnosis()
        lines = [
            self._device_line(diagnosis),
            self._profile_line(),
            "Nothing has been applied to the EC yet.",
        ]
        if diagnosis is not None:
            lines += ["", diagnosis.hint]
            self._set_banner(f"{MSG_NO_HW} - {diagnosis.summary}", "banner-error")
        self._set_status("\n".join(lines))

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
        content.append(self._build_profile_row())

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
            self._add_row(main_list, slot, with_command=True)
        self._add_omen_row(main_list)
        content.append(main_list)

        content.append(self._build_spacing_row())
        content.append(self._build_advanced())
        content.append(self._build_auto_section())
        return content

    def _add_row(self, listbox: Gtk.ListBox, slot: str,
                 with_command: bool = False) -> None:
        """Append one [name | summary | Record | Keys | Edit | Cmd? | Clear] row."""
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
        record.set_tooltip_text(f"Record the keystrokes the EC replays for {slot}")
        record.connect("clicked", lambda _btn, s=slot: self._on_record_clicked(s))

        keys = Gtk.Button(label="Keys")
        keys.set_tooltip_text(
            f"Type {slot}'s keys in by hand (for shortcuts the system "
            "grabs before recording can see them, like Ctrl+Alt+T)"
        )
        keys.connect("clicked", lambda _btn, s=slot: self._open_keys_dialog(s))

        edit = Gtk.Button(label="Edit")
        edit.set_tooltip_text(
            f"Edit {slot} step by step - fix a mis-recorded chord, "
            "insert/delete steps, retype a step"
        )
        edit.connect("clicked", lambda _btn, s=slot: self._open_macro_editor(s))

        widgets: list[Gtk.Widget] = [name_label, summary, record, keys, edit]

        cmd_btn = None
        if with_command:
            cmd_btn = Gtk.Button(label="Cmd")
            cmd_btn.set_tooltip_text(
                f"Run a command when {slot} is pressed instead of typing "
                f"(uses the {omen_key.key_accel(slot)} shortcut)"
            )
            cmd_btn.connect("clicked", lambda _btn, s=slot: self._on_cmd_clicked(s))
            widgets.append(cmd_btn)

        clear = Gtk.Button(label="Clear")
        clear.set_tooltip_text(f"Remove the macro for {slot}")
        if with_command:
            clear.set_tooltip_text(
                f"Remove the macro and the command for {slot} (key does nothing)"
            )
        clear.connect("clicked", lambda _btn, s=slot: self._on_clear_clicked(s))
        widgets.append(clear)

        for widget in widgets:
            box.append(widget)
        row.set_child(box)
        listbox.append(row)
        self.rows[slot] = {
            "row": row,
            "summary": summary,
            "record": record,
            "clear": clear,
            "cmd": cmd_btn,
            "edit": edit,
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
            f"(0-{MAX_SPACING_MS} ms). Saves and applies as you change it."
        )
        self.spacing.connect("value-changed", self._on_spacing_changed)

        box.append(label)
        box.append(self.spacing)
        return box

    def _on_spacing_changed(self, _spin) -> None:
        if self._loading:
            return
        if self._spacing_timer:
            GLib.source_remove(self._spacing_timer)
        self._spacing_timer = GLib.timeout_add(500, self._on_spacing_settled)

    def _on_spacing_settled(self) -> bool:
        self._spacing_timer = 0
        self._persist()
        return False

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

    # -- profiles ----------------------------------------------------------
    def _build_profile_row(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        label = Gtk.Label(label="Profile:", xalign=0)
        box.append(label)

        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.set_hexpand(True)
        self.profile_combo.set_tooltip_text(
            "Switching profiles applies that profile immediately."
        )
        self.profile_combo.connect("changed", self._on_profile_selected)
        box.append(self.profile_combo)

        new_btn = Gtk.Button(label="New")
        new_btn.set_tooltip_text("Create a profile as a copy of the current one")
        new_btn.connect("clicked", self._on_profile_new)
        rename_btn = Gtk.Button(label="Rename")
        rename_btn.set_tooltip_text("Rename the current profile")
        rename_btn.connect("clicked", self._on_profile_rename)
        delete_btn = Gtk.Button(label="Delete")
        delete_btn.set_tooltip_text("Delete the current profile")
        delete_btn.connect("clicked", self._on_profile_delete)
        for btn in (new_btn, rename_btn, delete_btn):
            box.append(btn)
        return box

    def _profile_line(self) -> str:
        return f"profile: {self._profile_name or '(none)'} ({self._root})"

    def _refresh_profile_combo(self) -> None:
        names = profiles.list_profiles(self._root)
        self._combo_quiet = True
        try:
            self.profile_combo.remove_all()
            for name in names:
                self.profile_combo.append_text(name)
            try:
                idx = names.index(self._profile_name)
            except ValueError:
                idx = 0
            self.profile_combo.set_active(idx)
        finally:
            self._combo_quiet = False

    def _on_profile_selected(self, _combo) -> None:
        if self._combo_quiet or self._loading:
            return
        name = self.profile_combo.get_active_text()
        if name and name != self._profile_name:
            self._switch_profile(name)

    def _switch_profile(self, name: str) -> None:
        try:
            cfg = profiles.read_profile(self._root, name)
        except (OSError, ValueError) as exc:
            self._set_banner(
                f"Could not read profile {name!r}: {exc}", "banner-error"
            )
            self._refresh_profile_combo()
            return
        try:
            profiles.set_active(self._root, name)
        except (OSError, ValueError) as exc:
            self._set_banner(
                f"Could not activate profile {name!r}: {exc}", "banner-error"
            )
            self._refresh_profile_combo()
            return
        self._profile_name = name
        self._finish_recording()
        self._loading = True
        try:
            self._apply_config_dict(cfg)
        finally:
            self._loading = False
        self._refresh_profile_combo()
        self._persist(f"Switched to profile {name}.")

    def _profile_dialog(self, title: str, initial: str, ok_label: str,
                        on_ok) -> None:
        """Small modal text dialog; on_ok(text) runs on confirmation."""
        dialog = Gtk.Window(transient_for=self, modal=True, title=title)
        dialog.set_default_size(360, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_text(initial)
        box.append(entry)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        ok = Gtk.Button(label=ok_label)
        ok.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        dialog.set_child(box)

        def done(confirmed: bool) -> None:
            if confirmed:
                try:
                    on_ok(entry.get_text())
                except (OSError, ValueError) as exc:
                    self._set_banner(f"{title}: {exc}", "banner-error")
                    return
            dialog.close()

        cancel.connect("clicked", lambda _b: done(False))
        ok.connect("clicked", lambda _b: done(True))
        entry.connect("activate", lambda _e: done(True))
        dialog.present()

    def _on_profile_new(self, _button) -> None:
        self._finish_recording()

        def create(name: str) -> None:
            clean = profiles.validate_name(name)
            if clean in profiles.list_profiles(self._root):
                raise ValueError(f"a profile named {clean!r} already exists")
            profiles.save_profile(self._root, clean, self._config())
            profiles.set_active(self._root, clean)
            self._profile_name = clean
            self._refresh_profile_combo()
            self._refresh_auto_status()
            self._persist(f"Created profile {clean}.")

        self._profile_dialog("New profile", "", "Create", create)

    def _on_profile_rename(self, _button) -> None:
        self._finish_recording()
        old = self._profile_name

        def rename(name: str) -> None:
            clean = profiles.validate_name(name)
            profiles.rename_profile(self._root, old, clean)
            if profiles.get_active(self._root) == old:
                profiles.set_active(self._root, clean)
            self._profile_name = clean
            self._refresh_profile_combo()
            self._refresh_auto_status()
            self._set_status(f"Renamed profile to {clean}.")

        self._profile_dialog("Rename profile", old, "Rename", rename)

    def _on_profile_delete(self, _button) -> None:
        self._finish_recording()
        names = profiles.list_profiles(self._root)
        if len(names) <= 1:
            self._set_banner("Cannot delete the only profile.", "banner-error")
            return
        old = self._profile_name
        try:
            if profiles.get_active(self._root) == old:
                self._switch_profile([n for n in names if n != old][0])
            profiles.delete_profile(self._root, old)
            self._auto_prune_profile(old)
        except (OSError, ValueError) as exc:
            self._set_banner(
                f"Could not delete profile {old!r}: {exc}", "banner-error"
            )
            return
        self._refresh_profile_combo()
        self._refresh_auto_status()
        if self._profile_name not in profiles.list_profiles(self._root):
            # The daemon may have moved the pointer while we were open;
            # never leave the window showing a profile that no longer exists.
            active = profiles.get_active(self._root)
            if active and active != self._profile_name:
                self._switch_profile(active)
                self._set_status(
                    f"Deleted {old}. Now showing active profile {active}.")
                return
        self._set_status(f"Deleted profile {old}.")

    # -- auto-switch -----------------------------------------------------
    AUTO_UNIT = "omen-macro-auto.service"

    def _build_auto_section(self) -> Gtk.Widget:
        self.auto_expander = Gtk.Expander(label="Auto-switch profiles (optional)")
        self.auto_expander.set_expanded(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(6)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        hint.set_text(
            "When on, a background service watches the focused app and switches "
            "to the mapped profile. Your manual switches always win until "
            "focus moves again. Matching is a case-insensitive substring of "
            "the app name and window title; the first rule wins."
        )
        inner.append(hint)

        enable_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        enable_row.append(Gtk.Label(label="Auto-switch", xalign=0))
        self.auto_switch = Gtk.Switch()
        self.auto_switch.set_tooltip_text(
            "Enable the per-user auto-switch service "
            f"({self.AUTO_UNIT}). Off by default."
        )
        self.auto_switch.connect("notify::active", self._on_auto_toggled)
        enable_row.append(self.auto_switch)
        self.auto_state = Gtk.Label(xalign=0)
        self.auto_state.add_css_class("dim-label")
        self.auto_state.set_hexpand(True)
        enable_row.append(self.auto_state)
        inner.append(enable_row)

        fallback_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fallback_row.append(Gtk.Label(label="When nothing matches, use:", xalign=0))
        self.auto_fallback = Gtk.ComboBoxText()
        self.auto_fallback.set_tooltip_text(
            "Profile applied when no rule matches the focused app."
        )
        self.auto_fallback.connect("changed", self._on_auto_fallback_changed)
        fallback_row.append(self.auto_fallback)
        inner.append(fallback_row)

        inner.append(Gtk.Label(label="Rules (first match wins):", xalign=0))
        self.auto_rules_list = Gtk.ListBox()
        self.auto_rules_list.set_selection_mode(Gtk.SelectionMode.NONE)
        inner.append(self.auto_rules_list)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.auto_match_entry = Gtk.Entry()
        self.auto_match_entry.set_hexpand(True)
        self.auto_match_entry.set_placeholder_text("app text, e.g. cs2")
        self.auto_match_entry.set_tooltip_text(
            "Substring matched against the focused app name and window title"
        )
        self.auto_rule_profile = Gtk.ComboBoxText()
        self.auto_rule_profile.set_tooltip_text("Profile to switch to on match")
        add_btn = Gtk.Button(label="Add rule")
        add_btn.connect("clicked", self._on_auto_rule_add)
        self.auto_match_entry.connect("activate", lambda _e: self._on_auto_rule_add(None))
        add_row.append(self.auto_match_entry)
        add_row.append(self.auto_rule_profile)
        add_row.append(add_btn)
        inner.append(add_row)

        detect_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.auto_detect = Gtk.Label(label="Focused now: (press Refresh)", xalign=0)
        self.auto_detect.set_hexpand(True)
        self.auto_detect.set_ellipsize(Pango.EllipsizeMode.END)
        detect_btn = Gtk.Button(label="Refresh")
        detect_btn.set_tooltip_text("Show what the daemon currently sees focused")
        detect_btn.connect("clicked", lambda _b: self._on_auto_detect())
        detect_row.append(self.auto_detect)
        detect_row.append(detect_btn)
        inner.append(detect_row)

        self.auto_expander.set_child(inner)
        return self.auto_expander

    def _auto_enabled(self) -> bool | None:
        """Whether the user unit is enabled; None when systemctl is unusable."""
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-enabled", self.AUTO_UNIT],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() == "enabled"

    def _auto_running(self) -> str:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-active", self.AUTO_UNIT],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return proc.stdout.strip() or "unknown"

    def _auto_set_enabled(self, on: bool) -> None:
        cmd = ["systemctl", "--user", "enable" if on else "disable",
               "--now", self.AUTO_UNIT]
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, timeout=15)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(f"could not run systemctl: {exc}") from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "systemctl failed").strip()
            raise OSError(detail)

    def _on_auto_toggled(self, switch: Gtk.Switch, _param) -> None:
        if self._auto_quiet:
            return
        # systemctl can block (we have seen the user bus stall for 25s+), so
        # run it off the UI thread; the switch goes insensitive meanwhile.
        # GTK is only touched from the idle callback below, never the worker.
        want = switch.get_active()
        switch.set_sensitive(False)
        import threading

        def work() -> None:
            try:
                self._auto_set_enabled(want)
            except OSError as exc:
                GLib.idle_add(self._auto_toggle_done, want, str(exc))
            else:
                GLib.idle_add(self._auto_toggle_done, want, None)

        threading.Thread(target=work, daemon=True).start()

    def _auto_toggle_done(self, want: bool, error: str | None) -> bool:
        self.auto_switch.set_sensitive(True)
        if error is not None:
            self._auto_quiet = True
            try:
                self.auto_switch.set_active(not want)
            finally:
                self._auto_quiet = False
            self._set_banner(f"Auto-switch: {error}", "banner-error")
        else:
            self._set_banner("", "")
            self._set_status(f"Auto-switch {'enabled' if want else 'disabled'}.")
        self._refresh_auto_status()
        return False

    def _on_auto_fallback_changed(self, _combo) -> None:
        if self._auto_quiet:
            return
        text = self.auto_fallback.get_active_text()
        fallback = None if text == "(stay on current)" else text
        try:
            auto_switch.save_rules(
                self._root, self._auto_rules, fallback)
        except (OSError, ValueError) as exc:
            self._set_banner(f"Auto-switch: {exc}", "banner-error")
            return
        self._auto_fallback = fallback

    def _on_auto_rule_add(self, _button) -> None:
        match = self.auto_match_entry.get_text().strip()
        profile = self.auto_rule_profile.get_active_text()
        if not match:
            self._set_banner("Auto-switch: type the app text to match.",
                             "banner-error")
            return
        if not profile:
            self._set_banner("Auto-switch: pick a profile for the rule.",
                             "banner-error")
            return
        self._auto_rules.append({"match": match, "profile": profile})
        try:
            auto_switch.save_rules(self._root, self._auto_rules,
                                   self._auto_fallback)
        except (OSError, ValueError) as exc:
            self._auto_rules.pop()
            self._set_banner(f"Auto-switch: {exc}", "banner-error")
            return
        self.auto_match_entry.set_text("")
        self._set_banner("", "")
        self._refresh_auto_rules()
        self._set_status(f"Auto-switch: when {match!r} is focused, use {profile}.")

    def _on_auto_rule_remove(self, index: int) -> None:
        try:
            removed = self._auto_rules.pop(index)
        except IndexError:
            return
        try:
            auto_switch.save_rules(self._root, self._auto_rules,
                                   self._auto_fallback)
        except (OSError, ValueError) as exc:
            self._auto_rules.insert(index, removed)
            self._set_banner(f"Auto-switch: {exc}", "banner-error")
            return
        self._refresh_auto_rules()

    def _on_auto_rule_move(self, index: int, direction: int) -> None:
        other = index + direction
        if not 0 <= other < len(self._auto_rules):
            return
        self._auto_rules[index], self._auto_rules[other] = (
            self._auto_rules[other], self._auto_rules[index])
        try:
            auto_switch.save_rules(self._root, self._auto_rules,
                                   self._auto_fallback)
        except (OSError, ValueError) as exc:
            self._auto_rules[other], self._auto_rules[index] = (
                self._auto_rules[index], self._auto_rules[other])
            self._set_banner(f"Auto-switch: {exc}", "banner-error")
            return
        self._refresh_auto_rules()

    def _on_auto_detect(self) -> None:
        detected = auto_switch.active_window()
        if detected is None:
            self.auto_detect.set_text("Focused now: unknown (is the desktop running?)")
        else:
            app_name, win_title = detected
            self.auto_detect.set_text(f"Focused now: {app_name} / {win_title}")

    def _auto_prune_profile(self, name: str) -> None:
        """Drop rules/fallback referencing a deleted profile, best effort."""
        rules = [r for r in self._auto_rules if r.get("profile") != name]
        fallback = None if self._auto_fallback == name else self._auto_fallback
        if len(rules) == len(self._auto_rules) and fallback == self._auto_fallback:
            return
        try:
            auto_switch.save_rules(self._root, rules, fallback)
        except (OSError, ValueError):
            return
        self._auto_rules, self._auto_fallback = rules, fallback

    def _refresh_auto_rules(self) -> None:
        while True:
            row = self.auto_rules_list.get_first_child()
            if row is None:
                break
            self.auto_rules_list.remove(row)
        for index, rule in enumerate(self._auto_rules):
            list_row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{side}")(2)
            text = Gtk.Label(
                label=f"\"{rule['match']}\" -> {rule['profile']}", xalign=0)
            text.set_hexpand(True)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            up = Gtk.Button(label="^")
            up.set_tooltip_text("Move rule up (earlier rules win)")
            up.connect("clicked", lambda _b, i=index: self._on_auto_rule_move(i, -1))
            down = Gtk.Button(label="v")
            down.set_tooltip_text("Move rule down")
            down.connect("clicked", lambda _b, i=index: self._on_auto_rule_move(i, 1))
            remove = Gtk.Button(label="Remove")
            remove.connect("clicked",
                           lambda _b, i=index: self._on_auto_rule_remove(i))
            for widget in (text, up, down, remove):
                box.append(widget)
            list_row.set_child(box)
            self.auto_rules_list.append(list_row)

    def _refresh_auto_status(self) -> None:
        try:
            rules, fallback = auto_switch.load_rules(self._root)
        except (OSError, ValueError):
            rules, fallback = [], None
        self._auto_rules = rules
        self._auto_fallback = fallback

        enabled = self._auto_enabled()
        running = self._auto_running() if enabled is not None else "unknown"
        self._auto_quiet = True
        try:
            self.auto_switch.set_active(bool(enabled))
        finally:
            self._auto_quiet = False
        if enabled is None:
            state = "systemctl unavailable"
        elif enabled:
            state = f"on ({running})"
        else:
            state = "off"
        self.auto_state.set_text(f"service {state}")
        self.auto_expander.set_label(f"Auto-switch profiles ({state})")

        names = profiles.list_profiles(self._root)
        self._auto_quiet = True
        try:
            self.auto_fallback.remove_all()
            self.auto_fallback.append_text("(stay on current)")
            for name in names:
                self.auto_fallback.append_text(name)
            if fallback in names:
                self.auto_fallback.set_active(names.index(fallback) + 1)
            else:
                self.auto_fallback.set_active(0)
            self.auto_rule_profile.remove_all()
            for name in names:
                self.auto_rule_profile.append_text(name)
            if names:
                self.auto_rule_profile.set_active(0)
        finally:
            self._auto_quiet = False
        self._refresh_auto_rules()

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

    def _open_command_dialog(self, key: str) -> None:
        """Dialog to set the command run when key is pressed.

        key is "OMEN" or "P1".."P6". The command is stored in this window and
        written to GNOME shortcuts on Bind; the persist step saves it to the
        current profile.
        """
        is_omen = key == "OMEN"
        title = "OMEN key command" if is_omen else f"{key} command"
        dialog = Gtk.Window(transient_for=self, modal=True, title=title)
        dialog.set_default_size(420, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)

        label = Gtk.Label(xalign=0)
        label.set_wrap(True)
        if is_omen:
            label.set_text(
                "Command run when the OMEN logo key is pressed "
                "(it sends XF86Launch2)."
            )
        else:
            label.set_text(
                f"Command run when {key} is pressed (it sends "
                f"{omen_key.key_accel(key)}). While a command is set, the "
                "typing macro for this key is left out of the EC upload: "
                "press Apply to EC to make the switch take effect."
            )
        box.append(label)

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        current = (self.commands.get(key) or "").strip()
        if not current:
            try:
                data = omen_key.get_key_binding(key)
                if data and data.get("command"):
                    current = data["command"]
            except OSError:
                pass
        entry.set_text(current or
                       (omen_key.OMEN_DEFAULT_COMMAND if is_omen else ""))
        entry.set_placeholder_text("e.g. firefox" if not is_omen else "e.g. omen-macro")
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
                    self._set_banner(
                        f"{key}: type a command first.", "banner-error"
                    )
                    return
                old = self.commands.get(key)
                self.commands[key] = command
                if key == "OMEN":
                    label = f"OMEN key now runs {command!r}."
                else:
                    label = (f"{key} now runs {command!r}; its typing macro "
                             "is left out of the upload.")
                if not self._persist(label):
                    if old is None:
                        del self.commands[key]
                    else:
                        self.commands[key] = old
                    return
            dialog.close()

        cancel.connect("clicked", lambda _b: done(False))
        ok.connect("clicked", lambda _b: done(True))
        entry.connect("activate", lambda _e: done(True))
        dialog.present()

    def _open_keys_dialog(self, slot: str) -> None:
        """Manually compose the chords for slot.

        Live recording cannot capture shortcuts the desktop grabs itself
        (Ctrl+Alt+T opens a terminal before our window ever sees the T), so
        this dialog writes exactly what the EC should replay: tick the
        modifiers, pick the key, Add chord. OK commits and persists.
        """
        self._finish_recording()
        dialog = Gtk.Window(transient_for=self, modal=True, title=f"{slot} keys")
        dialog.set_default_size(480, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        hint.set_text(
            "Tick the modifiers, pick the key, Add chord. Use this for "
            "shortcuts the system grabs before recording sees them "
            "(for example Ctrl+Alt+T, which opens a terminal)."
        )
        box.append(hint)

        working: list[list[str]] = [list(c) for c in self.slots.get(slot, [])]
        saved: list[list[str]] = [list(c) for c in working]

        chords_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(chords_box)

        def refresh_chords() -> None:
            while True:
                child = chords_box.get_first_child()
                if child is None:
                    break
                chords_box.remove(child)
            if not working:
                empty = Gtk.Label(label="no chords yet", xalign=0)
                empty.add_css_class("dim-label")
                chords_box.append(empty)
            for i, chord in enumerate(working):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                text = Gtk.Label(label=codec.describe_chord(chord), xalign=0)
                text.set_hexpand(True)
                text.set_ellipsize(Pango.EllipsizeMode.END)
                remove = Gtk.Button(label="Remove")
                remove.connect(
                    "clicked",
                    lambda _b, idx=i: (working.pop(idx), refresh_chords()),
                )
                row.append(text)
                row.append(remove)
                chords_box.append(row)

        refresh_chords()

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mod_checks: dict[str, Gtk.CheckButton] = {}
        for pretty, canonical in (
            ("Ctrl", "Control_L"),
            ("Alt", "Alt_L"),
            ("Shift", "Shift_L"),
            ("Super", "Super_L"),
        ):
            check = Gtk.CheckButton(label=pretty)
            mod_checks[canonical] = check
            add_row.append(check)
        key_combo = Gtk.ComboBoxText.new_with_entry()
        for name in KEY_CHOICES:
            key_combo.append_text(name)
        key_combo.set_active(-1)
        add_row.append(key_combo)
        add_btn = Gtk.Button(label="Add chord")
        add_row.append(add_btn)
        box.append(add_row)

        def key_text() -> str:
            entry = key_combo.get_child()
            return entry.get_text() if isinstance(entry, Gtk.Entry) else ""

        def on_add(_btn) -> None:
            mods = [m for m, check in mod_checks.items() if check.get_active()]
            try:
                chord = build_chord(mods, key_text())
            except codec.MacroError as exc:
                self._set_banner(f"{slot}: {exc}", "banner-error")
                return
            self._set_banner("", "")
            working.append(chord)
            for check in mod_checks.values():
                check.set_active(False)
            entry = key_combo.get_child()
            if isinstance(entry, Gtk.Entry):
                entry.set_text("")
            refresh_chords()

        add_btn.connect("clicked", on_add)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        reset = Gtk.Button(label="Reset")
        reset.set_tooltip_text("Throw away dialog edits, back to the saved chords")
        cancel = Gtk.Button(label="Cancel")
        ok = Gtk.Button(label="OK")
        ok.add_css_class("suggested-action")
        buttons.append(reset)
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        dialog.set_child(box)

        def done(confirmed: bool) -> None:
            if confirmed:
                old = self.slots.get(slot)
                if working:
                    self.slots[slot] = [list(c) for c in working]
                    note = f"{slot} = {codec.describe_entry(working)}."
                else:
                    self.slots.pop(slot, None)
                    note = f"{slot} cleared."
                if not self._persist(note):
                    if old is None:
                        self.slots.pop(slot, None)
                    else:
                        self.slots[slot] = old
                    return
            dialog.close()

        reset.connect(
            "clicked",
            lambda _b: (working.clear(), working.extend([list(c) for c in saved]),
                        refresh_chords(), self._set_banner("", "")),
        )
        cancel.connect("clicked", lambda _b: done(False))
        ok.connect("clicked", lambda _b: done(True))
        dialog.present()

    def _open_macro_editor(self, slot: str) -> None:
        """Edit slot's macro one step at a time, in place.

        A recorded chord is every key the window saw held at once; when two
        keys were really pressed one after the other the EC replays them
        together and the macro is wrong.  This dialog shows the saved chords
        as one text row per step, so the bad step can be retyped or split
        without re-recording the rest of the macro.  Structural edits
        (Split/Insert/Delete/Add step) capture what is currently typed before
        re-rendering, so half-edited text is never lost; OK commits through
        the same persist path as the Keys dialog (save profile + upload).
        """
        self._finish_recording()
        dialog = Gtk.Window(transient_for=self, modal=True,
                            title=f"Edit {slot} macro")
        dialog.set_default_size(620, 520)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        hint.set_text(
            "One row is one step: the keys the EC presses together. Join keys "
            "with + (Ctrl+Shift+t); a space or comma separates steps "
            "(h e = h then e). Wrap text in \"double quotes\" to type it "
            "literally (\"Hi!\" = Shift+h, i, Shift+1). Key names ignore "
            "case: Ctrl, Shift, Alt, Super, Enter, Space, KP_1 ... Recorded "
            "two keys as one chord by mistake? Press Split to turn h+e into "
            "two steps."
        )
        outer.append(hint)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(steps_box)
        outer.append(scroller)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_btn = Gtk.Button(label="Add step")
        add_btn.set_tooltip_text("Append an empty step at the end of the macro")
        add_row.append(add_btn)
        outer.append(add_row)

        preview = Gtk.Label(xalign=0)
        preview.set_wrap(True)
        preview.add_css_class("dim-label")
        outer.append(preview)

        error_label = Gtk.Label(xalign=0)
        error_label.set_wrap(True)
        error_label.add_css_class("banner")
        error_label.add_css_class("banner-error")
        error_label.set_visible(False)
        outer.append(error_label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        reset = Gtk.Button(label="Reset")
        reset.set_tooltip_text("Throw away dialog edits, back to the saved steps")
        cancel = Gtk.Button(label="Cancel")
        ok = Gtk.Button(label="OK")
        ok.add_css_class("suggested-action")
        buttons.append(reset)
        buttons.append(cancel)
        buttons.append(ok)
        outer.append(buttons)
        dialog.set_child(outer)

        saved: list[list[str]] = [
            list(chord) for chord in (self.slots.get(slot) or [])
        ]
        texts: list[str] = [format_editor_entry(chord) for chord in saved]
        entries: list[Gtk.Entry] = []
        split_buttons: list[Gtk.Button] = []

        def capture() -> None:
            """Copy typed text into ``texts`` so a re-render cannot lose it."""
            for index, entry in enumerate(entries):
                if index < len(texts):
                    texts[index] = entry.get_text()

        def parsed_row(index: int) -> list[list[str]] | None:
            """Steps of one row, or None while it is empty/invalid."""
            if not 0 <= index < len(texts):
                return None
            try:
                return codec.parse_steps(texts[index])
            except codec.MacroError:
                return None

        def show_error(text: str) -> None:
            if text:
                error_label.set_text(text)
                error_label.set_visible(True)
            else:
                error_label.set_text("")
                error_label.set_visible(False)

        def update_preview() -> None:
            for index, button in enumerate(split_buttons):
                steps = parsed_row(index)
                button.set_sensitive(
                    steps is not None and len(steps) == 1 and len(steps[0]) > 1
                )
            try:
                steps = parse_editor_entries(texts)
            except codec.MacroError as exc:
                preview.set_text(f"cannot preview yet - {exc}")
                return
            # The rows parse again: drop an error left over from a failed OK,
            # otherwise the red banner contradicts the green preview.
            show_error("")
            preview.set_text(
                editor_preview(steps, int(self.spacing.get_value()))
            )

        def focus_entry(index: int) -> None:
            def grab() -> bool:
                if 0 <= index < len(entries):
                    entries[index].grab_focus()
                return False

            GLib.idle_add(grab)

        def focus_first(_window=None) -> None:
            """Put the cursor in the first step (or Add step) when the dialog maps."""
            if entries:
                entries[0].grab_focus()
            else:
                add_btn.grab_focus()

        def on_entry_changed(entry: Gtk.Entry, index: int) -> None:
            if index < len(texts):
                texts[index] = entry.get_text()
            update_preview()

        def render(focus: int | None = None) -> None:
            while True:
                child = steps_box.get_first_child()
                if child is None:
                    break
                steps_box.remove(child)
            entries.clear()
            split_buttons.clear()
            if not texts:
                empty = Gtk.Label(
                    label="no steps yet - use Add step to start a macro",
                    xalign=0,
                )
                empty.add_css_class("dim-label")
                steps_box.append(empty)
            for index, text in enumerate(texts):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                number = Gtk.Label(label=f"{index + 1}.", xalign=1)
                number.add_css_class("dim-label")
                number.set_size_request(32, -1)

                entry = Gtk.Entry()
                entry.set_text(text)
                entry.set_hexpand(True)
                entry.set_placeholder_text("one step, e.g. Ctrl+c or h")
                entry.connect("changed", on_entry_changed, index)

                split = Gtk.Button(label="Split")
                split.set_tooltip_text(
                    "Turn this chord into one step per key (h+e -> h, then e)"
                )
                split.connect("clicked", lambda _b, i=index: do_split(i))

                insert = Gtk.Button(label="Insert")
                insert.set_tooltip_text("Insert a new empty step after this one")
                insert.connect("clicked", lambda _b, i=index: do_insert(i))

                delete = Gtk.Button(label="Delete")
                delete.set_tooltip_text("Remove this step from the macro")
                delete.connect("clicked", lambda _b, i=index: do_delete(i))

                row.append(number)
                row.append(entry)
                row.append(split)
                row.append(insert)
                row.append(delete)
                steps_box.append(row)
                entries.append(entry)
                split_buttons.append(split)
            update_preview()
            if focus is not None:
                focus_entry(focus)

        def do_split(index: int) -> None:
            """Replace this chord with one single-key step per key."""
            capture()
            steps = parsed_row(index)
            if steps is None or len(steps) != 1 or len(steps[0]) < 2:
                return
            texts[index:index + 1] = [
                format_editor_entry(step) for step in codec.split_chord(steps[0])
            ]
            show_error("")
            render(focus=index)

        def do_insert(index: int) -> None:
            capture()
            texts.insert(index + 1, "")
            show_error("")
            render(focus=index + 1)

        def do_delete(index: int) -> None:
            capture()
            if not 0 <= index < len(texts):
                return
            texts.pop(index)
            show_error("")
            render(focus=min(index, len(texts) - 1) if texts else None)

        def add_step(_button) -> None:
            capture()
            texts.append("")
            show_error("")
            render(focus=len(texts) - 1)

        def reset_editor(_button) -> None:
            texts[:] = [format_editor_entry(chord) for chord in saved]
            show_error("")
            render(focus=0 if texts else None)

        def done(confirmed: bool) -> None:
            if not confirmed:
                dialog.close()
                return
            capture()
            try:
                steps = parse_editor_entries(texts)
            except codec.MacroError as exc:
                show_error(str(exc))
                return
            try:
                # The dialog must report this itself: _persist() would only
                # show it in the main window's banner, behind the modal.
                codec.encode_entry(steps, int(self.spacing.get_value()))
            except codec.MacroError as exc:
                show_error(str(exc))
                return
            show_error("")
            old = self.slots.get(slot)
            if steps:
                self.slots[slot] = [list(chord) for chord in steps]
                note = f"{slot} = {codec.describe_entry(steps)}."
            else:
                self.slots.pop(slot, None)
                note = f"{slot} cleared."
            command = self.commands.get(slot)
            if command:
                note += (f" {slot} runs {command!r}, so its typing macro is "
                         "still left out of the upload.")
            if not self._persist(note):
                # Roll the in-memory value back so the window does not show a
                # macro that was not applied.  (_persist() saves the profile
                # before uploading, so the file may already carry it; the main
                # window banner says what failed.)  Report it here too: the
                # main window is behind this modal dialog.
                if old is None:
                    self.slots.pop(slot, None)
                else:
                    self.slots[slot] = old
                self._refresh_row(slot)
                show_error("could not save or send the macro - see the "
                           "message in the main window")
                return
            dialog.close()

        add_btn.connect("clicked", add_step)
        reset.connect("clicked", reset_editor)
        cancel.connect("clicked", lambda _b: done(False))
        ok.connect("clicked", lambda _b: done(True))

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_key(_controller, keyval, _keycode, _state) -> bool:
            if keyval == Gdk.KEY_Escape:
                done(False)
                return True
            return False

        keys.connect("key-pressed", on_key)
        dialog.add_controller(keys)

        dialog.connect("map", focus_first)
        render(focus=0 if texts else None)
        dialog.present()

    def _on_cmd_clicked(self, slot: str) -> None:
        self._finish_recording()
        self._open_command_dialog(slot)

    def _on_omen_set_clicked(self, _button) -> None:
        self._finish_recording()
        self._open_command_dialog("OMEN")

    def _on_omen_remove_clicked(self, _button) -> None:
        self._finish_recording()
        old = self.commands.pop("OMEN", None)
        if old is None:
            self._set_status("OMEN key was already unbound.")
            return
        if not self._persist("OMEN key binding removed."):
            self.commands["OMEN"] = old

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
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        hint.set_text(
            "Every change saves the current profile and sends it to the "
            "keyboard automatically. Switching profiles applies that profile."
        )
        footer.append(hint)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.reapply_btn = Gtk.Button(label="Re-apply")
        self.reapply_btn.set_tooltip_text(
            "Re-send the current profile to the keyboard and shortcuts "
            "(recovery if the keys stop responding)."
        )
        self.reapply_btn.connect("clicked", lambda _b: self._persist("Re-applied."))

        self.defaults_btn = Gtk.Button(label="Load defaults")
        self.defaults_btn.set_tooltip_text(
            "Replace this window with the stock numpad layout, saved and "
            "sent immediately."
        )
        self.defaults_btn.connect("clicked", self._on_defaults_clicked)

        self.stop_btn = Gtk.Button(label="Stop recording")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_tooltip_text("Finish recording the current row")
        self.stop_btn.set_visible(False)
        self.stop_btn.connect("clicked", lambda _btn: self._finish_recording())

        for widget in (self.reapply_btn, self.defaults_btn):
            bar.append(widget)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        bar.append(self.stop_btn)
        footer.append(bar)
        return footer

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
        # This is also what happens when the shortcut being recorded fires
        # system-wide (Ctrl+Alt+T opens a terminal and takes focus): the rest
        # of the chord never reaches us, so point at the manual editor.
        if window.props.is_active or self.recording is None:
            return
        self.recording.drop_held()
        self._sync_recording_ui()
        self._set_banner(
            f"{self.recording_slot}: focus left the window. Shortcuts the "
            "system grabs itself (like Ctrl+Alt+T opening a terminal) cannot "
            "be recorded live - use the Keys button to type them in.",
            "banner-warning",
        )

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
            self._persist(f"{slot} = {codec.describe_entry(chords)}.")
        else:
            self._set_status(f"{slot}: no keys captured, nothing changed.")
            self._refresh_row(slot)

    def _on_clear_clicked(self, slot: str) -> None:
        if self.recording_slot == slot:
            self._finish_recording()
        macro = self.slots.pop(slot, None)
        command = self.commands.pop(slot, None)
        if macro is None and command is None:
            self._set_status(f"{slot} was already empty.")
            self._refresh_row(slot)
            return
        detail = f"{slot} cleared"
        if command is not None:
            detail += f": macro and command {command!r} removed"
        detail += "."
        if self._persist(detail):
            return
        # Roll back so the window still matches the file and shortcuts.
        if macro is not None:
            self.slots[slot] = macro
        if command is not None:
            self.commands[slot] = command
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
        command = self.commands.get(slot)
        if command:
            text = f"runs {command!r} (typing macro off)"
            tip = (f"{slot} runs {command!r} instead of typing. "
                   "Clear removes both the command and the macro.")
        else:
            text = codec.describe_entry(self.slots.get(slot) or [])
            tip = f"{slot} is empty" if text == "empty" else text
        row["summary"].set_text(text)
        row["summary"].set_tooltip_text(tip)

    # -- config ------------------------------------------------------------
    def _config(self) -> dict:
        slots = {
            name: [list(chord) for chord in chords]
            for name, chords in self.slots.items()
            if chords
        }
        cfg: dict = {"spacing_ms": int(self.spacing.get_value()), "slots": slots}
        if self.commands:
            cfg["commands"] = dict(self.commands)
        return cfg

    def _profile_config(self) -> dict:
        """Config for the EC upload: command-bound P-keys are left out.

        A key that runs a command must not also type, so its slot is uploaded
        empty. The saved config keeps both; only the upload is filtered.
        """
        cfg = self._config()
        cmds = cfg.get("commands", {})
        if cmds:
            slots = {
                k: v
                for k, v in cfg.get("slots", {}).items()
                if not (k in cmds and k in omen_key.COMMANDABLE_PKEYS)
            }
            cfg = {**cfg, "slots": slots}
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

        self.commands, cmd_problems = omen_key.normalize_commands(cfg)
        problems.extend(cmd_problems)

        self._refresh_all()
        self._refresh_omen_status()
        if problems:
            self._set_banner(
                "Ignored invalid config entries: " + "; ".join(problems),
                "banner-error",
            )
        else:
            self._set_banner("", "")

    def _profile_line(self) -> str:
        return f"profile: {self._profile_name or '(none)'} ({self._root})"

    def _load_store(self) -> None:
        """Load the store, then the active profile, into the window."""
        self._loading = True
        try:
            self._root = profiles.config_root()
            self._profile_name = profiles.ensure_store(self._root)
            cfg, _, _ = profiles.resolve_config(self._root)
            self._apply_config_dict(cfg)
            self._refresh_profile_combo()
        except (OSError, ValueError) as exc:
            self._set_banner(f"Could not load profiles: {exc}", "banner-error")
        finally:
            self._loading = False

    # -- hardware / saving -------------------------------------------------
    def _hardware_diagnosis(self) -> boot_diag.Diagnosis | None:
        """Explain a missing macro_profile, or None when the interface is there."""
        if (sysfs_dir() / PROFILE_ATTR).exists():
            return None
        return boot_diag.diagnose_missing_interface(sysfs_dir())

    def _device_line(self, diagnosis: boot_diag.Diagnosis | None = None) -> str:
        path = sysfs_dir() / PROFILE_ATTR
        if diagnosis is None and not path.exists():
            diagnosis = boot_diag.diagnose_missing_interface(sysfs_dir())
        if diagnosis is None:
            return f"device: {path} (present)"
        return f"device: {path} (missing - {diagnosis.summary})"

    def _upload_ec(self) -> tuple[bool, str]:
        """Send the current window (minus command-bound slots) to the EC."""
        sysfs = sysfs_dir()
        path = sysfs / PROFILE_ATTR
        try:
            profile = codec.config_to_profile(self._profile_config())
        except codec.MacroError as exc:
            self._set_banner(f"Cannot encode the macro table: {exc}", "banner-error")
            self._set_status("Profile not written.")
            return False, ""
        try:
            written = write_profile(profile, sysfs)
        except (FileNotFoundError, NotADirectoryError):
            diagnosis = boot_diag.diagnose_missing_interface(sysfs)
            self._set_banner(f"{MSG_NO_HW} - {diagnosis.summary}", "banner-error")
            self._set_status(f"{path} does not exist.\n\n{diagnosis.hint}")
            return False, ""
        except PermissionError:
            self._set_banner(MSG_NO_PERM, "banner-error")
            self._set_status(f"Permission denied writing {path}.")
            return False, ""
        except OSError as exc:
            self._set_banner(f"EC rejected the profile: {exc}", "banner-error")
            self._set_status(f"macro_status: {read_status(sysfs)}")
            return False, ""
        status = (f"wrote {written} bytes to {path}\n"
                  f"macro_status: {read_status(sysfs)}")
        skipped = [k for k in omen_key.COMMANDABLE_PKEYS if k in self.commands]
        if skipped:
            status += (f"\n{', '.join(skipped)} run commands, "
                       "so their typing macros were not uploaded.")
        return True, status

    def _persist(self, note: str = "") -> bool:
        """Save the window to the current profile, sync shortcuts, upload.

        This is the only write path: every mutation funnels through here, so
        the file, the shortcuts and the EC can never disagree for long.
        """
        try:
            profiles.save_profile(self._root, self._profile_name, self._config())
        except (OSError, ValueError) as exc:
            self._set_banner(f"Could not save profile: {exc}", "banner-error")
            return False
        try:
            omen_key.sync_commands(self.commands)
        except (OSError, ValueError) as exc:
            self._set_banner(f"Shortcuts out of sync: {exc}", "banner-error")
            return False
        ok, upload_status = self._upload_ec()
        if not ok:
            return False
        self._set_banner("", "")
        self._refresh_all()
        self._refresh_omen_status()
        head = f"Saved to {self._profile_name}, applied to EC."
        if note:
            head += f" {note}"
        self._set_status(f"{head}\n{upload_status}")
        return True

    def _on_defaults_clicked(self, _button) -> None:
        self._finish_recording()
        self._loading = True
        try:
            self.slots = {
                name: [list(chord) for chord in chords]
                for name, chords in codec.NUMPAD_DEFAULT.items()
            }
            self.spacing.set_value(DEFAULT_SPACING_MS)
        finally:
            self._loading = False
        self._persist("Loaded the stock numpad layout.")

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
    check("P-key accel mapping",
          [omen_key.key_accel(k) for k in ("P1", "P6")], ["KP_1", "KP_6"])
    check("P-key shortcut paths are unique",
          len({omen_key.key_path(k) for k in omen_key.COMMANDABLE_KEYS}), 7)
    migrated, _ = omen_key.normalize_commands(
        {"omen_key": {"command": "omen-macro"}})
    check("legacy omen_key migrates", migrated, {"OMEN": "omen-macro"})

    # 10. Profiles: seed + resolve in a temp root (no touch of real config).
    import tempfile
    troot = Path(tempfile.mkdtemp(prefix="omen-selftest-"))
    check("profiles seed Default",
          (profiles.ensure_store(troot), profiles.list_profiles(troot)),
          ("Default", ["Default"]))
    cfg, name, _ = profiles.resolve_config(troot)
    check("profiles resolve seeded",
          (name, cfg["slots"]["P1"]), ("Default", [["KP_1"]]))
    profiles.save_profile(troot, "Gaming", {"spacing_ms": 0, "slots": {}})
    profiles.set_active(troot, "Gaming")
    check("profiles switch", profiles.get_active(troot), "Gaming")

    # 11. Auto-switch matcher: first rule wins, case-insensitive.
    check("auto match first wins",
          auto_switch.match_app(
              "Firefox", "t",
              [{"match": "fire", "profile": "A"},
               {"match": "firefox", "profile": "B"}]),
          "A")
    check("auto match none", auto_switch.match_app("a", "b", []), None)

    # 12. Manual chord builder: the Ctrl+Alt+T case from the bug report.
    check("manual chord Ctrl+Alt+T",
          build_chord(["Control_L", "Alt_L"], "t"),
          ["Control_L", "Alt_L", "t"])
    check("manual chord single key", build_chord([], "F5"), ["F5"])
    for bad_mods, bad_key in (([], ""), ([], "F13"),
                              (["Control_L"], "Control_L")):
        try:
            build_chord(bad_mods, bad_key)
            check(f"manual chord rejects {bad_mods!r}/{bad_key!r}",
                  "no error", "MacroError")
        except codec.MacroError:
            check(f"manual chord rejects {bad_mods!r}/{bad_key!r}",
                  "MacroError", "MacroError")

    # 13. Step editor: fix a mis-recorded chord without re-recording.
    check("editor entry formats a chord",
          format_editor_entry(["Control_L", "c"]), "Ctrl+c")
    check("editor entry of an empty step is blank",
          format_editor_entry([]), "")

    roundtrip = [
        ["h"], ["e"], ["Control_L", "c"], ["Shift_L", "t"],
        ["KP_Add"], ["space"], ["Control_L", "Shift_L", "t"],
    ]
    check("editor entry round-trips through the parser",
          [parse_editor_entries([format_editor_entry(chord)])
           for chord in roundtrip],
          [[chord] for chord in roundtrip])

    # codec.format_chord() spells a chord that mixes KP_Add with another key
    # with canonical names ("Control_L+KP_Add") because "Ctrl+Num+" would read
    # back as the key "Num"; a lone KP_Add keeps the pretty "Num+".
    mixed_chord = ["Control_L", "KP_Add"]
    check("mixed numpad-plus chord uses the canonical spelling",
          format_editor_entry(mixed_chord), "Control_L+KP_Add")
    check("mixed numpad-plus chord still round-trips",
          parse_editor_entries([format_editor_entry(mixed_chord)]), [mixed_chord])
    check("unambiguous row keeps the pretty spelling",
          format_editor_entry(["KP_Add"]), "Num+")
    check("the canonical spelling can be typed by hand",
          parse_editor_entries(["Ctrl+KP_Add"]), [mixed_chord])
    check("hand-typed Num+ still means KP_Add",
          parse_editor_entries(["Ctrl+Num+"]), [mixed_chord])
    check("every key has an editable row",
          [parse_editor_entries([format_editor_entry([key])])
           for key in codec.SCANCODES],
          [[[key]] for key in codec.SCANCODES])

    check("editor entries flatten in order",
          parse_editor_entries(["h", "", "e", "Ctrl+c"]),
          [["h"], ["e"], ["Control_L", "c"]])
    check("one row may hold several steps",
          parse_editor_entries(["h e", "l"]),
          [["h"], ["e"], ["l"]])
    check("blank rows mean an empty macro",
          parse_editor_entries(["", "  "]), [])
    check("quoted text becomes sequential steps",
          parse_editor_entries(['"Hi!"']),
          [["Shift_L", "h"], ["i"], ["Shift_L", "1"]])
    try:
        parse_editor_entries(["h", "e", "Bogus"])
        check("editor error names the row", "no error", "MacroError")
    except codec.MacroError as exc:
        check("editor error names the row", str(exc).startswith("step 3:"), True)

    # The bug report: recording stored h and e as one chord. The editor shows
    # "h+e"; Split rewrites it as the two steps "h" and "e"; OK must encode
    # exactly the bytes a hand-built [["h"], ["e"]] macro would produce.
    misrecorded = [["h", "e"]]
    check("mis-recorded chord shows as one row",
          [format_editor_entry(chord) for chord in misrecorded], ["h+e"])
    split_rows = [format_editor_entry(chord)
                  for chord in codec.split_chord(codec.parse_chord("h+e"))]
    check("Split turns h+e into two rows", split_rows, ["h", "e"])
    fixed_steps = parse_editor_entries(split_rows)
    check("fixed macro is two sequential steps", fixed_steps, [["h"], ["e"]])
    check("fixed macro encodes like the hand-built one",
          codec.encode_entry(fixed_steps, DEFAULT_SPACING_MS),
          codec.encode_entry([["h"], ["e"]], DEFAULT_SPACING_MS))

    # 14. Live preview: step count + payload budget, and never raises.
    preview_text = editor_preview([["h"], ["e"], ["Control_L", "c"]],
                                  DEFAULT_SPACING_MS)
    check("preview counts the steps", "3 steps" in preview_text, True)
    check("preview lists the steps", "h, e, Ctrl+c" in preview_text, True)
    check("preview shows the byte budget", "/254 bytes" in preview_text, True)
    check("preview of an empty editor mentions clearing",
          "clears the slot" in editor_preview([], DEFAULT_SPACING_MS), True)
    check("preview flags an over-long macro",
          "macro too long" in editor_preview([["a"]] * 120, DEFAULT_SPACING_MS),
          True)
    check("preview survives an unencodable key",
          editor_preview([["Bogus"]], DEFAULT_SPACING_MS).startswith("1 step:"),
          True)

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
