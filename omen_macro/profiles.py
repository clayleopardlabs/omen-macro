"""Named macro profiles for the OMEN macro GUI and the boot service.

Layout under the config root (usually /etc/omen-macro):

    profiles/<name>.json   one full config each: {spacing_ms, slots, commands}
    active-profile          plain text: the profile name the boot applies
    profile.json            legacy single-file config, migrated once on first run

The GUI and omen-macro-apply share this module so they can never disagree
about which profile is active. All writers go through save_profile /
set_active, which best-effort fix ownership to root:omen-hw 0664 (files) so a
root-seeded store stays writable for GUI users in the omen-hw group.

OMEN_MACRO_ROOT overrides the config root directory (tests, dev checkouts).
"""

from __future__ import annotations

import grp
import json
import os
import re
from pathlib import Path

from omen_macro import codec  # noqa: E402
from omen_macro import omen_key  # noqa: E402

PROFILES_DIR_NAME = "profiles"
LEGACY_FILENAME = "profile.json"
ACTIVE_FILENAME = "active-profile"
DEFAULT_NAME = "Default"

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
CONFIG_ENV = "OMEN_MACRO_ROOT"


def config_root() -> Path:
    """Config root dir, honouring OMEN_MACRO_ROOT.

    Mirrors the old single-file behaviour: /etc/omen-macro when it exists
    (normal installs), otherwise ~/.config/omen-macro (dev checkouts).
    """
    override = os.environ.get(CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if Path("/etc/omen-macro").is_dir():
        return Path("/etc/omen-macro")
    return Path.home() / ".config" / "omen-macro"


def profiles_dir(root: Path) -> Path:
    return root / PROFILES_DIR_NAME


def validate_name(name: str) -> str:
    """Return the stripped name, or raise ValueError (safe as a filename)."""
    cleaned = (name or "").strip()
    if not _NAME_RE.fullmatch(cleaned):
        raise ValueError(
            "profile names use letters, digits, _ and -, "
            "start with a letter or digit, max 40 characters"
        )
    return cleaned


def list_profiles(root: Path) -> list[str]:
    """Sorted profile names present on disk."""
    pdir = profiles_dir(root)
    if not pdir.is_dir():
        return []
    return sorted(p.stem for p in pdir.glob("*.json") if p.is_file())


def profile_path(root: Path, name: str) -> Path:
    return profiles_dir(root) / f"{validate_name(name)}.json"


def default_config() -> dict:
    return {"spacing_ms": 20, "slots": dict(codec.NUMPAD_DEFAULT)}


def _fix_perms(path: Path, is_dir: bool = False) -> None:
    """Best-effort root:omen-hw ownership so GUI users can rewrite seeds."""
    try:
        gid = grp.getgrnam("omen-hw").gr_gid
    except KeyError:
        return
    try:
        os.chown(path, -1, gid)
        os.chmod(path, 0o2775 if is_dir else 0o664)
    except OSError:
        pass


def save_profile(root: Path, name: str, cfg: dict) -> Path:
    """Write one profile file. Raises OSError/ValueError."""
    path = profile_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    _fix_perms(path.parent, is_dir=True)
    _fix_perms(path)
    return path


def read_profile(root: Path, name: str) -> dict:
    """Read one profile file. Raises OSError/ValueError."""
    cfg = json.loads(profile_path(root, name).read_text())
    if not isinstance(cfg, dict):
        raise ValueError("profile file does not hold a JSON object")
    return cfg


def delete_profile(root: Path, name: str) -> None:
    profile_path(root, name).unlink()


def rename_profile(root: Path, old: str, new: str) -> Path:
    """Rename a profile file. Raises OSError/ValueError."""
    src = profile_path(root, old)
    dst = profile_path(root, new)
    if dst.exists():
        raise ValueError(f"a profile named {validate_name(new)!r} already exists")
    src.rename(dst)
    return dst


def get_active(root: Path) -> str | None:
    """Active profile name, or None when unset/blank."""
    try:
        text = (root / ACTIVE_FILENAME).read_text().strip()
    except OSError:
        return None
    return text or None


def set_active(root: Path, name: str) -> None:
    """Point the boot at name. Raises OSError/ValueError."""
    validate_name(name)
    if name not in list_profiles(root):
        raise ValueError(f"no profile named {name!r}")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACTIVE_FILENAME
    path.write_text(name + "\n")
    _fix_perms(root, is_dir=True)
    _fix_perms(path)


def ensure_store(root: Path) -> str:
    """Create dirs, migrate legacy file, seed Default, fix active.

    Returns the active profile name. Safe to call on every start.
    """
    root.mkdir(parents=True, exist_ok=True)
    _fix_perms(root, is_dir=True)
    pdir = profiles_dir(root)
    pdir.mkdir(exist_ok=True)
    _fix_perms(pdir, is_dir=True)

    if not list_profiles(root):
        legacy = root / LEGACY_FILENAME
        if legacy.is_file():
            try:
                raw = json.loads(legacy.read_text())
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
                try:
                    spacing = int(raw.get("spacing_ms", 20))
                except (TypeError, ValueError):
                    spacing = 20
                commands, _ = omen_key.normalize_commands(raw)
                cfg: dict = {
                    "spacing_ms": max(0, min(2040, spacing)),
                    "slots": slots,
                }
                if commands:
                    cfg["commands"] = commands
                save_profile(root, DEFAULT_NAME, cfg)
            else:
                save_profile(root, DEFAULT_NAME, default_config())
        else:
            save_profile(root, DEFAULT_NAME, default_config())

    names = list_profiles(root)
    active = get_active(root)
    if active not in names:
        set_active(root, names[0])
        return names[0]
    return active


def resolve_config(root: Path, prefer: str | None = None) -> tuple[dict, str, Path]:
    """Return (config, name, path) for prefer or the active profile.

    Raises FileNotFoundError (with available names) / ValueError / OSError.
    """
    active = ensure_store(root)
    name = validate_name(prefer) if prefer else active
    path = profile_path(root, name)
    if not path.is_file():
        available = ", ".join(list_profiles(root)) or "(none)"
        raise FileNotFoundError(f"no profile named {name!r} (have: {available})")
    return read_profile(root, name), name, path
