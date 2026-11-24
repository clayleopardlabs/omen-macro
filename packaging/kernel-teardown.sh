#!/bin/bash
# Restore the stock hp-wmi driver when omen-macro is removed.
#
# Called from the package pre-remove step.  Safe to run by hand:
#
#   sudo /usr/lib/omen-macro/kernel-teardown.sh
#
# Like kernel-setup.sh, it reports problems and exits 0.
set -uo pipefail

SYSFS="/sys/devices/platform/hp-wmi"

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
	warn "run this script as root: sudo $0"
	exit 0
fi

# Clear the table so the keys go back to doing nothing, before the driver that
# exposes the attribute is replaced.
if [ -w "${SYSFS}/macro_profile" ]; then
	say "Clearing the macro table"
	dd if=/dev/zero of="${SYSFS}/macro_profile" bs=4096 count=1 >/dev/null 2>&1 || true
fi

KSRC=""
for source_dir in /usr/src/hp-omen-extra-*; do
	if [ -f "${source_dir}/hp-wmi.c" ]; then
		KSRC="${source_dir}"
	fi
done

if [ -z "${KSRC}" ]; then
	warn "the hp-omen-extra source is not in /usr/src. Nothing to restore."
	exit 0
fi

if ! grep -q 'HPWMI_MACRO_PROFILE_SET' "${KSRC}/hp-wmi.c"; then
	echo "The driver is not patched, nothing to restore."
	exit 0
fi

BACKUP="$(ls -1t "${KSRC}"/hp-wmi.c.bak-omen-macro-* 2>/dev/null | head -1)"
if [ -z "${BACKUP}" ]; then
	warn "no backup of hp-wmi.c found in ${KSRC}."
	warn "Reinstall hp-omen-extra to get the original source back:"
	warn "  sudo apt install --reinstall hp-omen-extra"
	exit 0
fi

say "Restoring ${BACKUP}"
cp -a "${BACKUP}" "${KSRC}/hp-wmi.c" || { warn "could not restore hp-wmi.c"; exit 0; }

VERSION="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${KSRC}/dkms.conf")"
if [ -n "${VERSION}" ] && command -v dkms >/dev/null 2>&1; then
	say "Rebuilding the stock DKMS module"
	dkms build -m hp-omen-extra -v "${VERSION}" --force >/dev/null 2>&1 || true
	dkms install -m hp-omen-extra -v "${VERSION}" --force >/dev/null 2>&1 || true
	modprobe -r hp_wmi >/dev/null 2>&1 || true
	modprobe hp_wmi >/dev/null 2>&1 || true
fi

udevadm control --reload-rules >/dev/null 2>&1 || true
echo "The original driver is back."
exit 0
