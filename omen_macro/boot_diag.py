#!/usr/bin/env python3
"""Explain why the hp-wmi hardware interface is unavailable.

The GUI and ``omen-macro-apply`` call :func:`diagnose_missing_interface` when
``/sys/devices/platform/hp-wmi/macro_profile`` cannot be opened.  Re-running
``install.sh`` is not always the answer: when Secure Boot is enabled and the
DKMS-built hp-wmi module is signed by a local MOK key that is not enrolled,
the kernel refuses the module with "Key was rejected by service" and no sysfs
attribute is ever created.  This module probes the system, read-only, so the
banner can name the real cause instead of guessing.

Probes (all optional, every one degrades to ``None``/``False``):

    Secure Boot      efivar value, falling back to ``mokutil --sb-state``
    module loaded    /sys/module/hp_wmi or /proc/modules
    module file      ``modinfo -n hp_wmi``, then the DKMS updates directory
    module signer    ``modinfo -F signer hp_wmi``
    signer enrolled  ``mokutil --list-enrolled`` / ``mokutil --test-key``
    lockdown         /sys/kernel/security/lockdown

Nothing here writes, calls sudo, imports GTK or raises.  There is no caching:
each call re-probes because the answer changes as soon as the key is enrolled
or the module is loaded.
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Directory holding macro_profile / macro_status (mirrors omen_macro.ec).
DEFAULT_SYSFS = Path("/sys/devices/platform/hp-wmi")
#: ec.PROFILE_ATTR; kept local so this module depends only on the stdlib.
PROFILE_ATTR = "macro_profile"

MODULE_NAME = "hp_wmi"
DEFAULT_MODULE_ROOT = Path("/sys/module") / MODULE_NAME
MODULES_FILE = Path("/proc/modules")
MODULES_DIR = Path("/lib/modules")
SECUREBOOT_EFIVAR = Path(
    "/sys/firmware/efi/efivars/"
    "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
)
LOCKDOWN_PATH = Path("/sys/kernel/security/lockdown")
MOK_CERT = Path("/var/lib/shim-signed/mok/MOK.der")

#: Every probe command gets this long, then gives up.  The tools are local and
#: return in a few milliseconds; the timeout only protects a hung mokutil.
PROBE_TIMEOUT = 1.0

#: A probe runner: argv + timeout -> combined stdout/stderr, or None when the
#: command could not be run at all (missing binary, timeout, bad encoding).
Runner = Callable[[list[str], float], str | None]


@dataclass(frozen=True)
class Environment:
    """Facts collected from the machine; ``None`` means "could not determine"."""

    sysfs: Path
    secure_boot: bool | None
    module_loaded: bool
    module_file: str | None
    signer: str | None
    signer_enrolled: bool | None
    lockdown: str | None
    kernel_release: str | None = None
    permission_error: str | None = None


@dataclass(frozen=True)
class Diagnosis:
    """One-line ``summary`` plus a multi-line, copy-pasteable ``hint``."""

    scenario: str
    summary: str
    hint: str
    environment: Environment


# --------------------------------------------------------------------------- #
# Read-only probes - each returns a plain value and never raises
# --------------------------------------------------------------------------- #
def _run(argv: list[str], timeout: float = PROBE_TIMEOUT) -> str | None:
    """Run one read-only probe command and return stdout+stderr combined.

    ``None`` means the command could not run at all (binary missing, timeout).
    A command that runs but exits non-zero still returns its output, because
    mokutil phrases its answer as text ("is not enrolled").
    """
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(os.environ, LC_ALL="C"),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout or ""


def _read_text(path: Path) -> str | None:
    """Read a small text file; return None when it is missing or unreadable."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _efi_secure_boot(efivars: Path) -> bool | None:
    """Decode the SecureBoot efivar: four attribute bytes, then one value byte."""
    try:
        raw = efivars.read_bytes()
    except OSError:
        return None
    if len(raw) < 5:
        return None
    return raw[4] == 1


def secure_boot_state(
    efivars: Path = SECUREBOOT_EFIVAR,
    run: Runner = _run,
    timeout: float = PROBE_TIMEOUT,
) -> bool | None:
    """True/False when Secure Boot can be read, None when it cannot."""
    state = _efi_secure_boot(efivars)
    if state is not None:
        return state
    out = run(["mokutil", "--sb-state"], timeout)
    if not out:
        return None
    text = out.lower()
    if "secureboot enabled" in text:
        return True
    if "secureboot disabled" in text:
        return False
    return None


def module_loaded(
    module_root: Path = DEFAULT_MODULE_ROOT,
    modules_file: Path = MODULES_FILE,
) -> bool:
    """True when hp_wmi is in the running kernel (sysfs entry or /proc/modules)."""
    if module_root.is_dir():
        return True
    text = _read_text(modules_file)
    if not text:
        return False
    return any(
        line.split(" ", 1)[0] == MODULE_NAME
        for line in text.splitlines()
        if line.strip()
    )


def module_path(
    run: Runner = _run,
    timeout: float = PROBE_TIMEOUT,
    kernel_release: str | None = None,
    modules_dir: Path = MODULES_DIR,
) -> str | None:
    """Path of the installed hp-wmi module file, or None when it is not built."""
    out = run(["modinfo", "-n", MODULE_NAME], timeout)
    if out:
        for line in out.splitlines():
            candidate = line.strip()
            # modinfo errors also land on the merged stream; keep real paths.
            if candidate.startswith("/") and ".ko" in candidate:
                return candidate
    release = kernel_release or platform.release()
    pattern = str(modules_dir / release / "updates" / "dkms" / "hp-wmi.ko*")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def module_signer(
    run: Runner = _run,
    timeout: float = PROBE_TIMEOUT,
) -> str | None:
    """Common name of the key that signed hp_wmi, or None when unsigned."""
    out = run(["modinfo", "-F", "signer", MODULE_NAME], timeout)
    if not out:
        return None
    for line in out.splitlines():
        value = line.strip()
        if value and "ERROR" not in value.upper():
            return value
    return None


def signer_enrolled(
    signer: str | None,
    mok_cert: Path = MOK_CERT,
    run: Runner = _run,
    timeout: float = PROBE_TIMEOUT,
) -> bool | None:
    """Is the module signer in the firmware MOK list?  None when unknowable."""
    if not signer:
        return None
    listed = run(["mokutil", "--list-enrolled"], timeout)
    if listed is not None:
        flat = " ".join(listed.split()).lower()
        if signer.lower() in flat:
            return True
    # Ask mokutil about the exact certificate that signed the module.
    if mok_cert.is_file():
        answer = run(["mokutil", "--test-key", str(mok_cert)], timeout)
        if answer:
            text = answer.lower()
            if "is already enrolled" in text:
                return True
            if "is not enrolled" in text:
                return False
    # A healthy MOK listing without this signer is conclusive.
    if listed is not None and (
        "certificate:" in listed.lower() or "subject:" in listed.lower()
    ):
        return False
    return None


def lockdown_state(path: Path = LOCKDOWN_PATH) -> str | None:
    """Active kernel lockdown mode ("none", "integrity", "confidentiality")."""
    text = _read_text(path)
    if not text:
        return None
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            return token[1:-1]
    return text.strip() or None


def _permission_error(sysfs: Path) -> str | None:
    """PermissionError text for the sysfs path, when the user is locked out."""
    for candidate in (sysfs, sysfs / PROFILE_ATTR):
        try:
            candidate.stat()
        except PermissionError as exc:
            return str(exc)
        except OSError:
            continue
    return None


def probe_environment(
    sysfs: Path | None = None,
    *,
    efivars: Path = SECUREBOOT_EFIVAR,
    module_root: Path = DEFAULT_MODULE_ROOT,
    modules_file: Path = MODULES_FILE,
    modules_dir: Path = MODULES_DIR,
    mok_cert: Path = MOK_CERT,
    lockdown_path: Path = LOCKDOWN_PATH,
    kernel_release: str | None = None,
    timeout: float = PROBE_TIMEOUT,
    run: Runner = _run,
) -> Environment:
    """Collect everything :func:`explain` needs; never raises, never writes.

    The keyword paths and ``run`` exist for tests: point them at temp files or
    a fake runner to exercise every branch without the real hardware.
    """
    base = Path(sysfs) if sysfs is not None else DEFAULT_SYSFS
    release = kernel_release or platform.release()
    signer = module_signer(run=run, timeout=timeout)
    return Environment(
        sysfs=base,
        secure_boot=secure_boot_state(efivars, run, timeout),
        module_loaded=module_loaded(module_root, modules_file),
        module_file=module_path(run, timeout, release, modules_dir),
        signer=signer,
        signer_enrolled=signer_enrolled(signer, mok_cert, run, timeout),
        lockdown=lockdown_state(lockdown_path),
        kernel_release=release,
        permission_error=_permission_error(base),
    )


# --------------------------------------------------------------------------- #
# Turning facts into a summary and a hint
# --------------------------------------------------------------------------- #
def _make(
    scenario: str, summary: str, hint: str, environment: Environment
) -> Diagnosis:
    return Diagnosis(
        scenario=scenario,
        summary=" ".join(summary.split()),
        hint=hint.strip(),
        environment=environment,
    )


def _lockdown_note(environment: Environment) -> str:
    """One paragraph when kernel lockdown also restricts module signatures."""
    mode = environment.lockdown
    if mode and mode != "none":
        return (
            f"Kernel lockdown is '{mode}'; only modules signed by a trusted "
            "key load.\n\n"
        )
    return ""


def _permission(environment: Environment, path: Path) -> Diagnosis:
    error = environment.permission_error or "permission denied"
    hint = f"""\
The kernel device interfaces exist, but this user cannot open them:

    {error}

Add yourself to the omen-hw group (install.sh creates it and its udev rule),
then log out and back in so the new group membership is applied:

sudo usermod -aG omen-hw "$USER"

After logging in again, check both:

getent group omen-hw
ls -l {path}

If the attribute is still not writable by the group, re-run sudo ./install.sh
to reinstall the udev rule (no module rebuild is needed for this).
"""
    return _make(
        "permission",
        "The hardware interface exists but this user cannot access it.",
        hint,
        environment,
    )


def _module_loaded_path(environment: Environment) -> Diagnosis:
    module_file = environment.module_file
    if module_file and "updates/dkms" in module_file:
        summary = (
            "hp-wmi is loaded, but it created no macro_profile - the running "
            "module may be the stock (unpatched) hp-wmi."
        )
        hint = f"""\
The hp-wmi module is in the kernel, but {PROFILE_ATTR} was never created.

The loaded module file is:

    {module_file}

If that is the DKMS build, the attribute may have failed to register. Reload
the module and read the kernel log:

sudo modprobe -r hp_wmi && sudo modprobe hp_wmi
journalctl -k -b | grep -i hp_wmi

ls {environment.sysfs}/
"""
    else:
        summary = (
            "hp-wmi is loaded, but it created no macro_profile - the running "
            "driver is the stock (unpatched) hp-wmi."
        )
        hint = f"""\
The hp-wmi module is in the kernel, but {PROFILE_ATTR} was never created.

The stock driver never creates these attributes, so the patched module has to
be installed:

sudo ./install.sh

Then reboot and check:

ls {environment.sysfs}/
journalctl -k -b | grep -i hp_wmi
"""
    return _make("module-loaded", summary, hint, environment)


def _module_not_installed(environment: Environment) -> Diagnosis:
    release = environment.kernel_release or platform.release()
    summary = f"The patched hp-wmi module is not installed for kernel {release}."
    secure_boot_note = ""
    if environment.secure_boot is True:
        secure_boot_note = """\
Secure Boot is on: install.sh signs the module with the local MOK key. If the
blue MOK screen was never completed, enrol that key too:

sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot

"""
    elif environment.secure_boot is None:
        secure_boot_note = """\
Secure Boot state could not be determined. Check it with:

mokutil --sb-state

"""
    hint = f"""\
No hp-wmi.ko was found under {MODULES_DIR}/{release}/updates/dkms/, so the
patched driver was never built for the running kernel.

Build and install it:

sudo ./install.sh

Then reboot and verify:

ls {environment.sysfs}/
journalctl -k -b | grep -i hp_wmi
{_lockdown_note(environment)}
{secure_boot_note}\
"""
    return _make("module-not-installed", summary, hint, environment)


def _not_loaded(environment: Environment) -> Diagnosis:
    module_file = environment.module_file or "(unknown path)"
    state = " (Secure Boot is off)" if environment.secure_boot is False else ""
    summary = f"The patched hp-wmi module is installed but not loaded{state}."
    secure_boot_note = ""
    if environment.secure_boot is None:
        secure_boot_note = """\
Secure Boot state is unknown. If the log says "Key was rejected by service",
enrol the local signing key:

sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot

"""
    hint = f"""\
The module file is present:

    {module_file}

Load it now and read what the kernel says:

sudo modprobe hp_wmi
journalctl -k -b | grep -i hp_wmi

If modprobe fails, (re)install the patched driver and reboot:

sudo ./install.sh
sudo reboot

Then check: ls {environment.sysfs}/
{_lockdown_note(environment)}
{secure_boot_note}\
"""
    return _make("module-not-loaded", summary, hint, environment)


def _secure_boot_blocks(environment: Environment) -> Diagnosis:
    signer = environment.signer
    enrolled = environment.signer_enrolled
    note = _lockdown_note(environment)
    if enrolled is False and signer:
        summary = (
            "Secure Boot is rejecting the locally signed hp-wmi module - its "
            "signing certificate is not enrolled."
        )
        hint = f"""\
Secure Boot is on. The hp-wmi module is signed with the local key:

    {signer}

That key is not in the firmware's MOK list, so the kernel refuses the module
with "Key was rejected by service" and {PROFILE_ATTR} never appears.

Enrol that key once - no rebuild or reinstall is needed:

sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot

At the blue "MOK management" screen choose Enrol MOK, then Continue, then
Reboot. The same thing can be done non-interactively with:

sudo ./fix-secureboot.sh

Alternative: turn Secure Boot off in the firmware and reboot.

{note}Check afterwards with: journalctl -k -b | grep -i hp_wmi
"""
        return _make("secureboot-key-rejected", summary, hint, environment)
    if enrolled is True:
        summary = (
            "Secure Boot already trusts the hp-wmi signing certificate, but "
            "the module is still not loaded."
        )
        hint = f"""\
The certificate that signed hp_wmi is enrolled:

    {signer}

Enrolment is not the problem, so ask the kernel why insertion failed:

journalctl -k -b | grep -i hp_wmi
sudo modprobe hp_wmi

If the log still says "Key was rejected by service", the installed module was
signed by a different (or rotated) key. Re-sign and enrol it:

sudo ./fix-secureboot.sh
sudo reboot

{note}"""
        return _make("secureboot-enrolled", summary, hint, environment)
    if signer is None:
        summary = "Secure Boot is on and the installed hp-wmi module is unsigned."
        hint = f"""\
Secure Boot is on and hp_wmi has no signer, so the kernel refuses to load it.

Sign it with the local MOK key, enrol that key, and reboot:

sudo ./install.sh
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot

Alternatively disable Secure Boot in the firmware, or run:

sudo ./fix-secureboot.sh

{note}Check afterwards with: journalctl -k -b | grep -i hp_wmi
"""
        return _make("secureboot-unsigned", summary, hint, environment)
    # signer is known but mokutil could not tell whether it is enrolled.
    summary = (
        "Secure Boot is on and hp-wmi is not loaded; whether its signing "
        "certificate is enrolled could not be checked (mokutil unavailable)."
    )
    hint = f"""\
Secure Boot is on and hp_wmi is not loaded. mokutil is not available, so the
signing certificate could not be checked. Run these read-only checks:

mokutil --sb-state
modinfo -F signer hp_wmi
journalctl -k -b | grep -i hp_wmi

If the log says "Key was rejected by service", enrol the local key:

sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot

or run: sudo ./fix-secureboot.sh

{note}"""
    return _make("secureboot-unverified", summary, hint, environment)


def explain(environment: Environment) -> Diagnosis:
    """Classify probed facts and build the user-facing summary and hint."""
    path = environment.sysfs / PROFILE_ATTR
    if environment.permission_error:
        return _permission(environment, path)
    if environment.module_loaded:
        return _module_loaded_path(environment)
    if not environment.module_file:
        return _module_not_installed(environment)
    if environment.secure_boot is True:
        return _secure_boot_blocks(environment)
    return _not_loaded(environment)


def diagnose_missing_interface(
    sysfs: Path | None = None, **probe_options: Any
) -> Diagnosis:
    """Probe the machine and explain why ``macro_profile`` is missing.

    Read-only, sub-second, never raises - safe to call from a GTK callback or
    a systemd unit.  ``sysfs`` overrides the device directory (the GUI honours
    OMEN_MACRO_SYSFS); keyword options are forwarded to
    :func:`probe_environment` for tests.
    """
    return explain(probe_environment(sysfs, **probe_options))
