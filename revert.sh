#!/usr/bin/env bash
# Undo install.sh: restore the original hp-wmi driver and remove the extras.
#
#   sudo ./revert.sh
#
# Your /etc/omen-macro/profile.json is kept (harmless, a few hundred bytes).
set -euo pipefail

KSRC="/usr/src/hp-omen-extra-2.1.2"
PKG="hp-omen-extra"
SYSFS="/sys/devices/platform/hp-wmi"

[[ ${EUID} -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

# Clear the EC table first so the macro engine stops replaying keystrokes after
# the interface disappears.
if [[ -e "${SYSFS}/macro_profile" ]] && command -v omen-macro-apply >/dev/null; then
	echo "==> clearing the EC macro table"
	omen-macro-apply --clear 2>/dev/null || true
fi

systemctl disable --now omen-macro-apply.service 2>/dev/null || true
rm -f /etc/systemd/system/omen-macro-apply.service
systemctl daemon-reload || true
rm -f /etc/udev/rules.d/99-omen-macro.rules
udevadm control --reload-rules || true

BAK="$(ls -1t "${KSRC}"/hp-wmi.c.bak-omen-macro-* 2>/dev/null | head -1 || true)"
if [[ -n "${BAK}" ]]; then
	echo "==> restoring ${BAK}"
	cp -a "${BAK}" "${KSRC}/hp-wmi.c"
	VER="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${KSRC}/dkms.conf")"
	dkms build -m "${PKG}" -v "${VER}" --force
	dkms install -m "${PKG}" -v "${VER}" --force
	modprobe -r hp_omen_extra 2>/dev/null || true
	modprobe -r hp_wmi 2>/dev/null || true
	modprobe hp_wmi 2>/dev/null || true
	modprobe hp_omen_extra 2>/dev/null || true
else
	echo "no hp-wmi.c backup found under ${KSRC}; reinstall the DKMS package by hand" >&2
fi

rm -f /usr/local/bin/omen-macro /usr/local/bin/omen-macro-apply
rm -rf /usr/local/lib/omen-macro
rm -f /usr/share/applications/omen-macro.desktop

echo
echo "Done. If hp_wmi could not be unloaded, reboot to get the original driver back."
echo "Config kept at /etc/omen-macro/profile.json."
