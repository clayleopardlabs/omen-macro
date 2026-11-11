"""Direct access to the EC macro table exposed by the patched hp-wmi driver.

write_profile() sends the whole 4096 byte table in a single write() syscall,
which is what the kernel bin_attribute hands to the WMI call. Kept separate
from gui.py so the auto-switch daemon can upload without importing GTK.
"""

from __future__ import annotations

import os
from pathlib import Path

from omen_macro import codec  # noqa: E402

DEFAULT_SYSFS = "/sys/devices/platform/hp-wmi"
PROFILE_ATTR = "macro_profile"
STATUS_ATTR = "macro_status"
SYSFS_ENV = "OMEN_MACRO_SYSFS"


def sysfs_dir() -> Path:
    """Directory holding macro_profile / macro_status, honouring OMEN_MACRO_SYSFS."""
    override = os.environ.get(SYSFS_ENV, "").strip()
    return Path(override) if override else Path(DEFAULT_SYSFS)


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


def upload_config(cfg: dict, sysfs: Path | None = None) -> int:
    """Encode cfg and upload it. Returns bytes written. Raises on failure."""
    return write_profile(codec.config_to_profile(cfg), sysfs)
