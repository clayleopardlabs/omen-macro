#!/usr/bin/env python3
"""Push a macro profile into the EC of an HP OMEN laptop.

The kernel patch (see kernel/hp-wmi-macro.patch) creates two files on the
hp-wmi platform device:

    macro_profile   write-only binary, exactly 4096 bytes.  The write itself
                    programs the EC and enables the macro engine.
    macro_status    read-only text with the WMI return codes of the last
                    attempt, for diagnostics.

Usage:
    omen-macro-apply                 # apply /etc/omen-macro/profile.json
    omen-macro-apply --default       # apply the stock numpad layout
    omen-macro-apply --dry-run       # encode and summarise, touch nothing
    omen-macro-apply --status        # just print macro_status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import codec  # noqa: E402

DEFAULT_CONFIG = Path("/etc/omen-macro/profile.json")
SYSFS = Path(os.environ.get("OMEN_MACRO_SYSFS", "/sys/devices/platform/hp-wmi"))


def read_status(sysfs: Path = SYSFS) -> str:
    path = sysfs / "macro_status"
    try:
        return path.read_text().strip()
    except OSError as exc:
        return f"(cannot read {path}: {exc})"


def write_profile(profile: bytes, sysfs: Path = SYSFS) -> int:
    """Write the 4096 byte table in one syscall so the bin attribute sees it whole."""
    if len(profile) != codec.PROFILE_SIZE:
        raise ValueError(f"profile must be {codec.PROFILE_SIZE} bytes")
    path = sysfs / "macro_profile"
    with open(path, "wb", buffering=0) as fh:
        return fh.write(profile)


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def summarise(cfg: dict) -> str:
    lines = [f"spacing: {cfg.get('spacing_ms', 0)} ms"]
    slots = cfg.get("slots") or {}
    if not slots:
        return "\n".join(lines + ["all slots empty"])
    for name in codec.SLOT_NAMES:
        chords = slots.get(name)
        if chords:
            lines.append(f"  {name:<10} {codec.describe_entry(chords)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--sysfs", type=Path, default=SYSFS)
    ap.add_argument("--default", action="store_true",
                    help="apply the stock numpad layout instead of a config file")
    ap.add_argument("--clear", action="store_true",
                    help="upload an all-empty table (all macro keys do nothing)")
    ap.add_argument("--dry-run", action="store_true", help="encode only, no hardware access")
    ap.add_argument("--status", action="store_true", help="print macro_status and exit")
    args = ap.parse_args(argv)

    if args.status:
        print(read_status(args.sysfs))
        return 0

    if args.clear:
        cfg = {"spacing_ms": 0, "slots": {}}
    elif args.default:
        cfg = {"spacing_ms": 20, "slots": codec.NUMPAD_DEFAULT}
    else:
        if not args.config.is_file():
            print(f"no config at {args.config}; use --default", file=sys.stderr)
            return 2
        cfg = load_config(args.config)

    try:
        profile = codec.config_to_profile(cfg)
    except codec.MacroError as exc:
        print(f"encode error: {exc}", file=sys.stderr)
        return 2

    print(summarise(cfg))
    print(f"encoded {len(profile)} bytes, {sum(1 for b in profile if b)} non-zero")

    if args.dry_run:
        return 0

    try:
        written = write_profile(profile, args.sysfs)
    except FileNotFoundError:
        print(f"hardware interface not found: {args.sysfs / 'macro_profile'}\n"
              "The patched hp-wmi module is not loaded - run install.sh.", file=sys.stderr)
        return 3
    except PermissionError:
        print("permission denied writing macro_profile; run install.sh to grant "
              "the omen-hw group write access", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"EC rejected the profile: {exc}", file=sys.stderr)
        print(read_status(args.sysfs), file=sys.stderr)
        return 5

    print(f"wrote {written} bytes")
    print(read_status(args.sysfs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
