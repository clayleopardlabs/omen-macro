#!/bin/bash
# Patch and rebuild the hp-wmi driver for omen-macro.
#
# Called from the package post-install step, and safe to run by hand at any
# time (it is idempotent):
#
#   sudo /usr/lib/omen-macro/kernel-setup.sh
#
# It never fails the caller: every problem is reported and the script exits 0,
# because the app is usable once the module side is fixed.
set -uo pipefail

SYSFS="/sys/devices/platform/hp-wmi"
PATCH="/usr/lib/omen-macro/kernel/hp-wmi-macro.patch"
DOC="/usr/share/doc/omen-macro"

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
	warn "run this script as root: sudo $0"
	exit 0
fi

if [ ! -f "${PATCH}" ]; then
	warn "kernel patch not found: ${PATCH}"
	exit 0
fi

KSRC=""
for source_dir in /usr/src/hp-omen-extra-*; do
	if [ -f "${source_dir}/hp-wmi.c" ]; then
		KSRC="${source_dir}"
	fi
done
if [ -z "${KSRC}" ]; then
	warn "the hp-omen-extra source is not in /usr/src."
	warn "Install that package first (it is part of the OMEN Space stack), then run:"
	warn "  sudo /usr/lib/omen-macro/kernel-setup.sh"
	warn "The app is installed but cannot work until the driver is patched."
	exit 0
fi

VERSION="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${KSRC}/dkms.conf")"
if [ -z "${VERSION}" ]; then
	warn "cannot read PACKAGE_VERSION from ${KSRC}/dkms.conf"
	exit 0
fi

command -v dkms >/dev/null 2>&1 || { warn "dkms is not installed: sudo apt install dkms"; exit 0; }
command -v patch >/dev/null 2>&1 || { warn "patch is not installed: sudo apt install patch"; exit 0; }

say "Patching the hp-wmi driver in ${KSRC}"
if grep -q 'HPWMI_MACRO_PROFILE_SET' "${KSRC}/hp-wmi.c"; then
	echo "  already patched"
else
	BACKUP="${KSRC}/hp-wmi.c.bak-omen-macro-$(date +%Y%m%d-%H%M%S)"
	if ! cp -a "${KSRC}/hp-wmi.c" "${BACKUP}"; then
		warn "could not back up ${KSRC}/hp-wmi.c"
		exit 0
	fi
	if ! patch -p1 -d "${KSRC}" < "${PATCH}"; then
		cp -a "${BACKUP}" "${KSRC}/hp-wmi.c"
		warn "the kernel patch did not apply. Nothing was changed."
		exit 0
	fi
	echo "  backup: ${BACKUP}"
fi

say "Rebuilding and installing the DKMS module (hp-omen-extra ${VERSION})"
if ! dkms build -m hp-omen-extra -v "${VERSION}" --force; then
	warn "dkms build failed. The module was not replaced."
	exit 0
fi
if ! dkms install -m hp-omen-extra -v "${VERSION}" --force; then
	warn "dkms install failed. The module was not replaced."
	exit 0
fi

say "Loading hp_wmi"
modprobe -r hp_omen_extra >/dev/null 2>&1 || true
modprobe -r hp_wmi >/dev/null 2>&1 || true
if ! modprobe hp_wmi 2>/tmp/omen-macro-modprobe.err; then
	modprobe_error="$(cat /tmp/omen-macro-modprobe.err 2>/dev/null)"
	rm -f /tmp/omen-macro-modprobe.err
	if printf '%s' "${modprobe_error}" | grep -q 'Key was rejected by service'; then
		warn "Secure Boot is rejecting the patched module."
		warn "Enrol the signing key once:"
		warn "  sudo mokutil --import /var/lib/shim-signed/mok/MOK.der"
		warn "  sudo reboot"
		warn "Then choose Enroll MOK in the blue screen at boot."
		warn "Full steps: ${DOC}/docs/SECURE-BOOT.md"
	else
		warn "could not load hp_wmi: ${modprobe_error:-see dmesg}"
	fi
	exit 0
fi
rm -f /tmp/omen-macro-modprobe.err

for _ in $(seq 1 40); do
	[ -e "${SYSFS}/macro_profile" ] && break
	sleep 0.25
done
if [ ! -e "${SYSFS}/macro_profile" ]; then
	warn "the module loaded but ${SYSFS}/macro_profile never appeared."
	warn "Reboot once; the boot service will retry the upload."
	exit 0
fi

chgrp omen-hw "${SYSFS}/macro_profile" "${SYSFS}/macro_status" >/dev/null 2>&1 || true
chmod 0220 "${SYSFS}/macro_profile" >/dev/null 2>&1 || true
chmod 0444 "${SYSFS}/macro_status" >/dev/null 2>&1 || true
udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=platform >/dev/null 2>&1 || true

if command -v omen-macro-apply >/dev/null 2>&1; then
	say "Sending the default profile"
	omen-macro-apply || warn "the profile was not sent yet. The boot service will retry."
fi

exit 0
