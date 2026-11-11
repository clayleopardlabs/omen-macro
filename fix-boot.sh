#!/usr/bin/env bash
# Apply the boot-time fixes only: systemd ordering + udev permissions.
#
#   sudo ./fix-boot.sh
#
# Unlike install.sh this does NOT rebuild the kernel module, so it takes
# seconds.  Use it to repair an install whose EC table was uploaded too late
# (after multi-user.target) or whose macro_profile lost its omen-hw group.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSFS="/sys/devices/platform/hp-wmi"

[[ ${EUID} -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

[[ -e "${SYSFS}/macro_profile" ]] || {
	echo "macro_profile does not exist - is the patched hp-wmi loaded?" >&2
	echo "Check: dmesg | grep 'macro:'" >&2
	exit 1
}

say "installing udev rule (no TEST on the attribute, waits for probe to finish)"
install -Dm644 "${SCRIPT_DIR}/udev/99-omen-macro.rules" /etc/udev/rules.d/99-omen-macro.rules
udevadm control --reload-rules || true

say "installing systemd unit (starts early instead of after multi-user.target)"
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-apply.service" /etc/systemd/system/omen-macro-apply.service
install -Dm644 "${SCRIPT_DIR}/systemd/omen-macro-auto.service" /etc/systemd/user/omen-macro-auto.service
systemctl daemon-reload
systemctl enable omen-macro-apply.service >/dev/null

say "repairing config tree ownership (root:omen-hw so GUI users can save)"
chgrp omen-hw /etc/omen-macro 2>/dev/null || true
chmod 2775 /etc/omen-macro 2>/dev/null || true
find /etc/omen-macro -maxdepth 1 -type f -exec chgrp omen-hw {} + 2>/dev/null || true
find /etc/omen-macro -maxdepth 1 -type f -exec chmod 0664 {} + 2>/dev/null || true
if [[ -d /etc/omen-macro/profiles ]]; then
	chgrp omen-hw /etc/omen-macro/profiles 2>/dev/null || true
	chmod 2775 /etc/omen-macro/profiles 2>/dev/null || true
	find /etc/omen-macro/profiles -maxdepth 1 -type f -exec chgrp omen-hw {} + 2>/dev/null || true
	find /etc/omen-macro/profiles -maxdepth 1 -type f -exec chmod 0664 {} + 2>/dev/null || true
fi

say "re-uploading the profile and repairing permissions"
systemctl restart omen-macro-apply.service || true
chgrp omen-hw "${SYSFS}/macro_profile" "${SYSFS}/macro_status" 2>/dev/null || true
chmod 0220 "${SYSFS}/macro_profile" 2>/dev/null || true
chmod 0444 "${SYSFS}/macro_status" 2>/dev/null || true

echo
systemctl status omen-macro-apply.service --no-pager 2>&1 | head -12 || true
echo
echo "macro_status: $(tr '\n' ' ' < "${SYSFS}/macro_status" 2>/dev/null)"
echo "permissions:  $(stat -c '%A %U:%G' "${SYSFS}/macro_profile")"
echo
echo "Try P1 now - it should type a numpad 1."
echo "Then reboot and press P1 again to confirm the early start."
