"""Optional auto-switch daemon: change macro profile with the focused app.

Polls the focused application over AT-SPI (works on Wayland/GNOME, no shell
extension needed), matches it against the ordered rules in auto-switch.json,
and switches to the mapped profile when focus moves. Falls back to a
configured profile when nothing matches.

Respects manual overrides: it only acts when the focused *application* changes,
so picking a profile by hand in the GUI is left alone until focus moves again.

State and side effects per switch: EC table upload, GNOME shortcut sync,
active-profile pointer (so a reboot keeps the last auto-selected profile).
Everything is logged to the journal; failures never kill the loop.

Rules file <root>/auto-switch.json:
    {"rules": [{"match": "cs2", "profile": "Gaming"}], "fallback": "Default"}
match is a case-insensitive substring of "app name + window title"; first
rule wins. The GUI owns this file; the daemon only reads it.

CLI:
    omen-macro-auto                 run the loop (systemd user service)
    omen-macro-auto --once          print detection + desired profile, no switch
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from omen_macro import omen_key  # noqa: E402
from omen_macro import profiles  # noqa: E402
from omen_macro.ec import upload_config  # noqa: E402

RULES_FILENAME = "auto-switch.json"
POLL_INTERVAL = 2.0

_atspi_ready = False


def load_rules(root: Path) -> tuple[list[dict], str | None]:
    """Return (rules, fallback). Missing/corrupt file means no rules."""
    path = root / RULES_FILENAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return [], None
    if not isinstance(raw, dict):
        return [], None
    rules: list[dict] = []
    for entry in raw.get("rules") or []:
        if not isinstance(entry, dict):
            continue
        match = entry.get("match")
        profile = entry.get("profile")
        if isinstance(match, str) and match.strip() and isinstance(profile, str):
            rules.append({"match": match.strip(), "profile": profile.strip()})
    fallback = raw.get("fallback")
    return rules, fallback if isinstance(fallback, str) and fallback.strip() else None


def save_rules(root: Path, rules: list[dict], fallback: str | None) -> Path:
    """Write the rules file. Raises OSError/ValueError."""
    clean: list[dict] = []
    for entry in rules or []:
        match = entry.get("match", "")
        profile = entry.get("profile", "")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("each rule needs a non-empty match string")
        clean.append({"match": match.strip(),
                      "profile": profiles.validate_name(profile)})
    if fallback is not None:
        fallback = profiles.validate_name(fallback)
    root.mkdir(parents=True, exist_ok=True)
    path = root / RULES_FILENAME
    data: dict = {"rules": clean}
    if fallback:
        data["fallback"] = fallback
    path.write_text(json.dumps(data, indent=2) + "\n")
    profiles._fix_perms(root, is_dir=True)
    profiles._fix_perms(path)
    return path


def match_app(app_name: str, win_title: str, rules: list[dict]) -> str | None:
    """First rule whose match appears in app name or window title (pure)."""
    hay = f"{app_name or ''}\n{win_title or ''}".lower()
    for rule in rules or []:
        needle = (rule.get("match") or "").strip().lower()
        if needle and needle in hay:
            return rule.get("profile")
    return None


def active_window() -> tuple[str, str] | None:
    """Focused (app name, window title) over AT-SPI, or None when unknown.

    Never raises: any failure (no session bus, a11y off, dead app) is None,
    and the caller simply retries later.
    """
    global _atspi_ready
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return None
    try:
        if not _atspi_ready:
            Atspi.init()
            _atspi_ready = True
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
            except Exception:
                continue
            try:
                count = app.get_child_count()
            except Exception:
                continue
            for j in range(count):
                try:
                    win = app.get_child_at_index(j)
                except Exception:
                    continue
                try:
                    if win.get_role_name() not in ("frame", "window", "dialog"):
                        continue
                    if win.get_state_set().contains(Atspi.StateType.ACTIVE):
                        return (app.get_name() or "", win.get_name() or "")
                except Exception:
                    continue
    except Exception:
        return None
    return None


def switch_to(root: Path, name: str) -> tuple[dict, str, Path]:
    """Activate profile name: EC upload, shortcut sync, pointer update."""
    cfg, resolved, path = profiles.resolve_config(root, prefer=name)
    upload_config(cfg)
    omen_key.sync_commands(cfg.get("commands") or {})
    profiles.set_active(root, resolved)
    return cfg, resolved, path


def run_daemon(root: Path, interval: float = POLL_INTERVAL) -> int:
    print("omen-macro-auto: starting", flush=True)
    last_app: str | None = None
    while True:
        try:
            active = profiles.ensure_store(root)
            rules, fallback = load_rules(root)
            detected = active_window()
            if detected is None:
                time.sleep(10.0)
                continue
            app_name, win_title = detected
            if app_name != last_app:
                last_app = app_name
                desired = match_app(app_name, win_title, rules) or fallback or active
                if desired != active:
                    try:
                        _, resolved, _ = switch_to(root, desired)
                    except FileNotFoundError as exc:
                        print(f"omen-macro-auto: skip ({exc})", flush=True)
                        continue
                    print(f"omen-macro-auto: {app_name!r} -> profile {resolved}",
                          flush=True)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(f"omen-macro-auto: error: {exc}", flush=True)
            time.sleep(30.0)
            continue
        time.sleep(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="print detection + desired profile, do not switch")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL,
                    help="poll seconds between focus checks")
    args = ap.parse_args(argv)
    root = profiles.config_root()

    if args.once:
        active = profiles.ensure_store(root)
        rules, fallback = load_rules(root)
        detected = active_window()
        print(f"active-profile: {active}")
        print(f"rules: {len(rules)} fallback: {fallback}")
        if detected is None:
            print("focused: (unknown - AT-SPI unavailable?)")
            return 0
        app_name, win_title = detected
        print(f"focused app: {app_name!r} window: {win_title!r}")
        print(f"desired: {match_app(app_name, win_title, rules) or fallback or active}")
        return 0

    return run_daemon(root, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
