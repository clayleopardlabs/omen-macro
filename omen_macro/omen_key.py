"""User configuration for the OMEN logo key (the button with the OMEN mark).

Unlike P1..P6, the logo key is not an EC macro slot: the hp-wmi driver always
reports it as KEY_PROG2 (code 149), which XKB exposes as XF86Launch2. There is
nothing to upload to the EC, so "configuring" it means owning one GNOME custom
shortcut: binding XF86Launch2 to a command of the user's choice (default:
launching this GUI).

This module keeps all dconf access in one place. The pure list helpers have no
I/O and are covered by tests/test_omen_key.py; the Gio/gsettings calls are
thin wrappers around them so the GUI cannot clobber the user's other custom
shortcuts.

Layout in dconf:
    org.gnome.settings-daemon.plugins.media-keys custom-keybindings
        -> list of object paths, one of which is OMEN_PATH
    per-path schema org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:
        {name, command, binding}
"""

from __future__ import annotations

import shutil
import subprocess

SCHEMA_MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEY_CUSTOM = "custom-keybindings"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

OMEN_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/omen-macro-omen-key/"
)
OMEN_ACCEL = "XF86Launch2"
OMEN_DEFAULT_NAME = "OMEN Key"
OMEN_DEFAULT_COMMAND = "omen-macro"


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O, unit-tested)
# --------------------------------------------------------------------------- #
def normalize_paths(value) -> list[str]:
    """Coerce a GSettings string array (or anything list-like) to a str list."""
    if not value:
        return []
    try:
        items = list(value)
    except TypeError:
        return []
    return [str(p) for p in items if str(p)]


def ensure_omen_path(paths: list[str]) -> list[str]:
    """Return paths with OMEN_PATH present exactly once, order preserved."""
    out = [p for p in paths if p != OMEN_PATH]
    out.append(OMEN_PATH)
    return out


def remove_omen_path(paths: list[str]) -> list[str]:
    """Return paths with every OMEN_PATH entry removed, order preserved."""
    return [p for p in paths if p != OMEN_PATH]


def binding_payload(name: str, command: str) -> dict:
    """Exact dict written to the custom-keybinding object."""
    return {"name": name, "command": command, "binding": OMEN_ACCEL}


def describe_binding(data: dict | None) -> str:
    """One-line human summary for the GUI status label."""
    if not data:
        return "OMEN key: no binding (pressing it does nothing)"
    cmd = data.get("command", "")
    acc = data.get("binding", "")
    return f"OMEN key: {acc} runs {cmd!r}" if cmd else "OMEN key: binding is empty"


# --------------------------------------------------------------------------- #
# Settings backend: Gio when importable, else the gsettings CLI
# --------------------------------------------------------------------------- #
def _gio_settings(schema: str, path: str | None = None):
    try:
        from gi.repository import Gio
    except Exception:
        return None
    try:
        if path is not None:
            return Gio.Settings.new_with_path(schema, path)
        return Gio.Settings(schema=schema)
    except Exception:
        return None


def _gsettings_cli(*args: str) -> str | None:
    if shutil.which("gsettings") is None:
        return None
    try:
        out = subprocess.run(
            ["gsettings", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _parse_strv(text: str) -> list[str]:
    """Parse gsettings string-array output like "['a', 'b']" or "@as []"."""
    text = (text or "").strip()
    if text in ("", "@as []", "[]"):
        return []
    if text.startswith("@as"):
        text = text[3:].strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for part in inner.split(","):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] == "'":
            items.append(part[1:-1])
        elif part:
            items.append(part.strip("'"))
    return items


def _format_strv(paths: list[str]) -> str:
    return "[" + ", ".join(f"'{p}'" for p in paths) + "]"


def get_custom_paths() -> list[str]:
    settings = _gio_settings(SCHEMA_MEDIA_KEYS)
    if settings is not None:
        try:
            return normalize_paths(settings.get_strv(KEY_CUSTOM))
        except Exception:
            pass
    raw = _gsettings_cli("get", SCHEMA_MEDIA_KEYS, KEY_CUSTOM)
    if raw is None:
        raise OSError("cannot read GNOME custom shortcuts (no Gio, no gsettings)")
    return normalize_paths(_parse_strv(raw))


def set_custom_paths(paths: list[str]) -> None:
    settings = _gio_settings(SCHEMA_MEDIA_KEYS)
    if settings is not None:
        try:
            settings.set_strv(KEY_CUSTOM, paths)
            settings.apply() if hasattr(settings, "apply") else None
            return
        except Exception as exc:
            raise OSError(f"cannot write GNOME custom shortcuts: {exc}") from None
    if _gsettings_cli("set", SCHEMA_MEDIA_KEYS, KEY_CUSTOM, _format_strv(paths)) is None:
        raise OSError("cannot write GNOME custom shortcuts (no Gio, no gsettings)")


def get_omen_binding() -> dict | None:
    """Return the current {name, command, binding} for OMEN_PATH, or None."""
    settings = _gio_settings(CUSTOM_SCHEMA, OMEN_PATH)
    if settings is not None:
        try:
            name = settings.get_string("name")
            command = settings.get_string("command")
            binding = settings.get_string("binding")
        except Exception as exc:
            raise OSError(f"cannot read OMEN key shortcut: {exc}") from None
        if not name and not command and not binding:
            return None
        return {"name": name, "command": command, "binding": binding}
    name = _gsettings_cli("get", f"{CUSTOM_SCHEMA}:{OMEN_PATH}", "name")
    command = _gsettings_cli("get", f"{CUSTOM_SCHEMA}:{OMEN_PATH}", "command")
    binding = _gsettings_cli("get", f"{CUSTOM_SCHEMA}:{OMEN_PATH}", "binding")
    if name is None or command is None or binding is None:
        raise OSError("cannot read OMEN key shortcut (no Gio, no gsettings)")
    return {"name": name.strip("'"), "command": command.strip("'"),
            "binding": binding.strip("'")}


def set_omen_key(command: str, name: str = OMEN_DEFAULT_NAME) -> dict:
    """Bind XF86Launch2 to command. Returns the payload that was written."""
    command = (command or "").strip()
    if not command:
        raise ValueError("command must not be empty")
    payload = binding_payload(name or OMEN_DEFAULT_NAME, command)
    settings = _gio_settings(CUSTOM_SCHEMA, OMEN_PATH)
    if settings is not None:
        try:
            settings.set_string("name", payload["name"])
            settings.set_string("command", payload["command"])
            settings.set_string("binding", payload["binding"])
            set_custom_paths(ensure_omen_path(get_custom_paths()))
            return payload
        except OSError:
            raise
        except Exception as exc:
            raise OSError(f"cannot write OMEN key shortcut: {exc}") from None
    for key, val in payload.items():
        if _gsettings_cli("set", f"{CUSTOM_SCHEMA}:{OMEN_PATH}", key, val) is None:
            raise OSError("cannot write OMEN key shortcut (no Gio, no gsettings)")
    set_custom_paths(ensure_omen_path(get_custom_paths()))
    return payload


def clear_omen_key() -> None:
    """Remove the OMEN entry from the shortcut list (values are left inert)."""
    set_custom_paths(remove_omen_path(get_custom_paths()))
