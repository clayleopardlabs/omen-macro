#!/usr/bin/env bash
# Install OMEN macro-key support (P1-P6) on Ubuntu.
#
#   sudo ./install.sh
#
# What it does, in order:
#   1. backs up and patches /usr/src/hp-omen-extra-2.1.2/hp-wmi.c so the driver
#      exposes a writable 4096-byte EC macro table (macro_profile/macro_status)
#   2. rebuilds + reinstalls the hp-omen-extra DKMS package and reloads it
#   3. installs the userspace encoder, CLI, daemon and GTK GUI to /usr/local
#   4. grants the omen-hw group write access to macro_profile (udev rule)
#   5. installs a oneshot systemd unit that re-uploads the active profile
#      at boot (the EC forgets it on a cold boot), plus a per-user unit for
#      the optional auto-switch daemon (off unless enabled in the GUI)
#   6. uploads the active profile so P1-P6 emit numpad 1-6 immediately
#      (first run seeds a Default profile, migrating legacy profile.json)
#
# Revert with ./revert.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KSRC="/usr/src/hp-omen-extra-2.1.2"
PKG="hp-omen-extra"
LIBDIR="/usr/local/lib/omen-macro"
SYSFS="/sys/devices/platform/hp-wmi"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

[[ ${EUID} -eq 0 ]] || die "run me with sudo"
[[ -f "${KSRC}/hp-wmi.c" ]] || die "${KSRC}/hp-wmi.c not found - is the hp-omen-extra DKMS package installed?"
command -v dkms >/dev/null || die "dkms not installed"
VER="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${KSRC}/dkms.conf")"
[[ -n "${VER}" ]] || die "cannot read PACKAGE_VERSION from ${KSRC}/dkms.conf"
BOARD="$(cat /sys/class/dmi/id/board_name 2>/dev/null || echo unknown)"
say "board ${BOARD}, hp-omen-extra ${VER}"

# ---------------------------------------------------------------- 1. patch
if grep -q 'HPWMI_MACRO_PROFILE_SET' "${KSRC}/hp-wmi.c"; then
	say "kernel source is already patched, leaving it alone"
else
	STAMP="$(date +%Y%m%d-%H%M%S)"
	BAK="${KSRC}/hp-wmi.c.bak-omen-macro-${STAMP}"
	cp -a "${KSRC}/hp-wmi.c" "${BAK}"
	say "patched driver: backup at ${BAK}"
	if ! patch -p1 -d "${KSRC}" --dry-run < "${SCRIPT_DIR}/kernel/hp-wmi-macro.patch" >/dev/null 2>&1; then
		cp -a "${BAK}" "${KSRC}/hp-wmi.c"
		die "the kernel patch does not apply to ${KSRC}/hp-wmi.c (source changed?). Revert with ${BAK}"
	fi
	patch -p1 -d "${KSRC}" < "${SCRIPT_DIR}/kernel/hp-wmi-macro.patch"
fi

# ---------------------------------------------------------------- 2. build + reload
say "rebuilding the DKMS module"
if ! dkms build -m "${PKG}" -v "${VER}" --force; then
	die "dkms build failed - the module was NOT replaced, your system is unchanged"
fi
dkms install -m "${PKG}" -v "${VER}" --force

# udev rule must be in place before the module comes back up
install -Dm644 "${SCRIPT_DIR}/udev/99-omen-macro.rules" /etc/udev/rules.d/99-omen-macro.rules
udevadm control --reload-rules || true
getent group omen-hw >/dev/null || groupadd omen-hw
if [[ -n "${SUDO_USER:-}" ]] && ! id -nG "${SUDO_USER}" | tr ' ' '\n' | grep -qx omen-hw; then
	usermod -aG omen-hw "${SUDO_USER}" && echo "added ${SUDO_USER} to group omen-hw (log out/in for it to take effect)"
fi

say "reloading hp_wmi"
modprobe -r hp_omen_extra 2>/dev/null || true
modprobe -r hp_wmi 2>/dev/null || true
RELOADED=0
if modprobe hp_wmi 2>/dev/null; then RELOADED=1; else echo "could not unload the old hp_wmi (in use?) - a reboot is needed"; fi
modprobe hp_omen_extra 2>/dev/null || true
for _ in $(seq 1 20); do [[ -e "${SYSFS}/macro_profile" ]] && break; sleep 0.25; done
[[ -e "${SYSFS}/macro_profile" ]] || RELOADED=0

chgrp omen-hw "${SYSFS}/macro_profile" 2>/dev/null || true
chmod 0220 "${SYSFS}/macro_profile" 2>/dev/null || true
chmod 0444 "${SYSFS}/macro_status" 2>/dev/null || true

# ---------------------------------------------------------------- 3. userspace
say "installing userspace tools to ${LIBDIR}"
install -d "${LIBDIR}/omen_macro"
install -m644 "${SCRIPT_DIR}"/omen_macro/*.py "${LIBDIR}/omen_macro/"
install -m644 "${SCRIPT_DIR}/README.md" "${LIBDIR}/README.md"

cat > /usr/local/bin/omen-macro <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH=/usr/local/lib/omen-macro${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m omen_macro.gui "$@"
LAUNCHER
cat > /usr/local/bin/omen-macro-apply <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH=/usr/local/lib/omen-macro${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m omen_macro.apply "$@"
LAUNCHER
cat > /usr/local/bin/omen-macro-auto <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH=/usr/local/lib/omen-macro${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m omen_macro.auto_switch "$@"
LAUNCHER
chmod 755 /usr/local/bin/omen-macro /usr/local/bin/omen-macro-apply /usr/local/bin/omen-macro-auto
install -Dm644 "${SCRIPT_DIR}/desktop/omen-macro.desktop" /usr/share/applications/omen-macro.desktop

# ---------------------------------------------------------------- 4. config
# Seeding happens at runtime: the first apply (below) or GUI start migrates a
# legacy profile.json and writes profiles/Default.json + active-profile.
install -d -g omen-hw -m 2775 /etc/omen-macro

# ---------------------------------------------------------------- 5. boot service
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-apply.service" /etc/systemd/system/omen-macro-apply.service
systemctl daemon-reload
systemctl enable omen-macro-apply.service >/dev/null

# Optional auto-switch daemon: per-user unit, stays off until the GUI opts in.
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-auto.service" /etc/systemd/user/omen-macro-auto.service

# ---------------------------------------------------------------- 6. apply now
say "uploading the active profile"
if [[ ${RELOADED} -eq 1 ]]; then
	omen-macro-apply || {
		echo
		echo "The EC did not accept the profile (see codes above)."
		echo "Nothing is broken: the macro engine simply stays disabled."
	}
	echo
	echo "macro_status: $(cat "${SYSFS}/macro_status" 2>/dev/null | tr '\n' ' ')"
	echo
	cat <<'DONE'
Next: press P1. It should now type a numpad 1.

Verify the kernel-side story with:
    sudo libinput debug-events --show-keycodes     # expect KEY_KP1
Or just open a text editor and press the keys.

Configure your own macros with:
    omen-macro            (GUI, also in the app grid as "OMEN Macro Keys")

If the keys do nothing, reboot once: the old module may still have been
resident while the EC rejected the first upload.
DONE
else
	echo
	echo "A reboot is required before the new driver is active."
	echo "After rebooting, the saved profile is uploaded automatically."
fi
