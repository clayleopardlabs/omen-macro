#!/usr/bin/env bash
# Install OMEN macro-key support (P1-P6) on Ubuntu.
#
#   sudo ./install.sh
#
# What it does, in order:
#   1. backs up and patches /usr/src/hp-omen-extra-2.1.2/hp-wmi.c so the driver
#      exposes a writable 4096-byte EC macro table (macro_profile/macro_status)
#   2. rebuilds + reinstalls the hp-omen-extra DKMS package and reloads it
#   3. checks Secure Boot: with it enabled the DKMS key
#      (/var/lib/shim-signed/mok/MOK.der) has to be enrolled once, otherwise the
#      kernel rejects the freshly built module with "Key was rejected by
#      service".  The enrolment itself is staged by ./fix-secureboot.sh (this
#      script only detects the situation and stops before claiming success).
#   4. installs the userspace encoder, CLI, daemon and GTK GUI to /usr/local
#   5. grants the omen-hw group write access to macro_profile (udev rule)
#   6. installs a oneshot systemd unit that re-uploads the active profile
#      at boot (the EC forgets it on a cold boot), plus a per-user unit for
#      the optional auto-switch daemon (off unless enabled in the GUI)
#   7. uploads the active profile so P1-P6 emit numpad 1-6 immediately
#      (first run seeds a Default profile, migrating legacy profile.json)
#
# Re-running is safe.  Revert with ./revert.sh; repair a Secure Boot rejection
# without rebuilding anything with ./fix-secureboot.sh.
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
SB_BLOCKED=0
if MODPROBE_OUT="$(modprobe hp_wmi 2>&1)"; then
	RELOADED=1
else
	printf '%s\n' "${MODPROBE_OUT}"
	if [[ "${MODPROBE_OUT}" == *"Key was rejected by service"* ]]; then
		SB_BLOCKED=1
	fi
fi
modprobe hp_omen_extra 2>/dev/null || true
for _ in $(seq 1 20); do [[ -e "${SYSFS}/macro_profile" ]] && break; sleep 0.25; done
[[ -e "${SYSFS}/macro_profile" ]] || RELOADED=0

chgrp omen-hw "${SYSFS}/macro_profile" 2>/dev/null || true
chmod 0220 "${SYSFS}/macro_profile" 2>/dev/null || true
chmod 0444 "${SYSFS}/macro_status" 2>/dev/null || true

# ------------------------------------------- 2b. Secure Boot (module signature)
# DKMS signs the module with /var/lib/shim-signed/mok/MOK.der, but with Secure
# Boot on the kernel only inserts modules signed by a key in shim's MOK list.
# That list normally holds just Canonical's CA until the DKMS key is enrolled
# once, so meanwhile the module loads nowhere: modprobe fails with
# "Key was rejected by service" and macro_profile never appears.  Enrolling it
# needs a one-time password plus a confirmation in the blue MOK screen at the
# next boot, so all we do here is notice the situation and let the end of the
# script stop instead of printing a success message that would be wrong.
MOK_DER="/var/lib/shim-signed/mok/MOK.der"
MOK_CN="Omen Secure Boot Module Signature key"
# Capture command output and match in the shell: `mokutil ... | grep -q` can
# miss a match under pipefail when grep exits before mokutil finished writing.
SB_STATE=""
if command -v mokutil >/dev/null 2>&1; then
	SB_STATE="$(mokutil --sb-state 2>/dev/null || true)"
fi
if [[ "${SB_STATE}" == *"SecureBoot enabled"* ]]; then
	say "Secure Boot is enabled - checking the DKMS signing key"
	if [[ -f "${MOK_DER}" ]] && command -v openssl >/dev/null; then
		_CN="$(openssl x509 -inform DER -in "${MOK_DER}" -noout -subject 2>/dev/null \
			| sed -n 's/^subject=.*CN *= *//p' || true)"
		if [[ -n "${_CN}" ]]; then MOK_CN="${_CN}"; fi
	fi
	MOK_LIST="$(mokutil --list-enrolled 2>/dev/null || true)"
	MOK_TESTKEY="$(mokutil --test-key "${MOK_DER}" 2>/dev/null || true)"
	if [[ -f "${MOK_DER}" ]] \
		&& { grep -qF -- "${MOK_CN}" <<<"${MOK_LIST}" || [[ "${MOK_TESTKEY}" == *"is already enrolled"* ]]; }; then
		echo "  MOK key '${MOK_CN}' is enrolled"
	else
		SB_BLOCKED=1
		echo "  MOK key '${MOK_CN}' is NOT enrolled - the kernel will reject the module"
	fi
fi

# ---------------------------------------------------------------- 4. userspace
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
install -Dm644 "${SCRIPT_DIR}/desktop/omen-macro.svg" \
	/usr/share/icons/hicolor/scalable/apps/omen-macro.svg
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q 2>/dev/null || true

# ---------------------------------------------------------------- 5. config
# Seeding happens at runtime: the first apply (below) or GUI start migrates a
# legacy profile.json and writes profiles/Default.json + active-profile.
install -d -g omen-hw -m 2775 /etc/omen-macro

# ---------------------------------------------------------------- 6. boot service
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-apply.service" /etc/systemd/system/omen-macro-apply.service
systemctl daemon-reload
systemctl enable omen-macro-apply.service >/dev/null

# Optional auto-switch daemon: per-user unit, stays off until the GUI opts in.
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-auto.service" /etc/systemd/user/omen-macro-auto.service

# ---------------------------------------------------------------- 7. apply now
if [[ ${RELOADED} -eq 0 && ${SB_BLOCKED} -eq 1 ]]; then
	# The module is built, installed and signed, but the kernel will not insert
	# it: Secure Boot only trusts keys in shim's MOK list and the DKMS key is
	# not enrolled yet.  fix-secureboot.sh stages that enrolment (a one-time
	# step per machine) and prints the blue MOK screen keystrokes.  No DKMS
	# rebuild is involved, so do not pretend the install finished.
	say "Secure Boot is blocking the patched module"
	if [[ -x "${SCRIPT_DIR}/fix-secureboot.sh" ]]; then
		"${SCRIPT_DIR}/fix-secureboot.sh" || true
	else
		cat <<'MSG'
Enrol the DKMS signing key by hand, then reboot:
    sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
    sudo reboot
At the blue "MOK management" screen choose Enroll MOK -> View key ->
Continue -> Yes, type the one-time password, then Reboot.
MSG
	fi
	cat <<'MSG'

Everything except the module load is installed: userspace tools, udev rule and
the boot service are in place.  P1-P6 stay dead until the key is enrolled and
you have rebooted once - after that boot the service uploads the profile
automatically and there is nothing left to run.

Re-run ./install.sh if you prefer it to confirm the result, or use:
    sudo ./verify.sh
MSG
	exit 1
fi

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

If Secure Boot gets enabled later (e.g. from Windows), the kernel starts
rejecting this module - run sudo ./fix-secureboot.sh once and confirm the
enrolment in the blue MOK screen at the next boot.
DONE
else
	echo
	echo "A reboot is required before the new driver is active."
	echo "After rebooting, the saved profile is uploaded automatically."
fi
