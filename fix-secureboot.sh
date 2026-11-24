#!/usr/bin/env bash
# Repair the "hardware interface not found" failure that Secure Boot causes.
#
#   sudo ./fix-secureboot.sh
#
# With Secure Boot enabled the kernel only inserts a module signed by a key
# that shim trusts.  DKMS already signs the patched hp-wmi with this machine's
# own key (/var/lib/shim-signed/mok/MOK.der, CN "Omen Secure Boot Module
# Signature key"), but that key is not in shim's MOK list until it is enrolled
# once by hand, so every boot logs
#
#   modprobe: ERROR: could not insert 'hp_wmi': Key was rejected by service
#
# and /sys/devices/platform/hp-wmi/macro_profile never appears.  The module is
# fine - built, patched and signed; only the enrolment is missing.
#
# What this script does, depending on the Secure Boot state:
#   * Secure Boot off, or the key already enrolled: reloads hp_wmi (no DKMS
#     rebuild), repairs the omen-hw permissions and restarts
#     omen-macro-apply.service.  The keys work immediately, no reboot.
#   * Secure Boot on and the key not enrolled: stages the one-time enrolment
#     with `mokutil --import` and prints exactly what to answer in the blue
#     "MOK management" screen at the next boot.
#   * in both cases: refreshes the userspace .py files from this repo in
#     /usr/local/lib/omen-macro and reinstalls the udev rule, so the improved
#     Secure Boot diagnostics ship with the repair.
#
# It is idempotent and deliberately conservative: no DKMS rebuild, no kernel
# source edits, no firmware/BIOS changes, no network.  Turning Secure Boot off
# in the BIOS is the fallback; this script never touches it.
#
# Exit codes:
#   0  the keys work now (module loaded, macro_profile present)
#  10  the one-time enrolment must be confirmed in the blue MOK screen at the
#      next boot; this script stages it, or prints the command when there is no
#      terminal to type the password into
#   1  a precondition failed (the message says which)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSFS="/sys/devices/platform/hp-wmi"
SVC="omen-macro-apply.service"
LIBDIR="/usr/local/lib/omen-macro"
MOK_DIR="/var/lib/shim-signed/mok"
MOK_DER="${MOK_DIR}/MOK.der"
# DKMS signs with this subject; only used when openssl cannot read MOK.der.
FALLBACK_CN="Omen Secure Boot Module Signature key"

SB_ENABLED=0
KEY_REJECTED=0
CN=""

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
ok() { printf '  ok   %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*" >&2; }

# The CN of the DKMS signing key.  openssl is optional: without it the constant
# above is used, which is what every hp-omen-extra 2.1.2 install gets.
mok_cn() {
	local cn=""
	if command -v openssl >/dev/null 2>&1; then
		cn="$(openssl x509 -inform DER -in "${MOK_DER}" -noout -subject 2>/dev/null \
			| sed -n 's/^subject=.*CN *= *//p')" || cn=""
	fi
	printf '%s' "${cn:-${FALLBACK_CN}}"
}

# Uppercase hex without colons, i.e. the same form modinfo -F sig_key prints.
mok_serial() {
	openssl x509 -inform DER -in "${MOK_DER}" -noout -serial 2>/dev/null \
		| sed -n 's/^serial=//p' | tr -d ':' | tr 'a-f' 'A-F'
}

# Is the signing key part of shim's MOK list?  mokutil prints the certificate
# subject, so a CN substring match is enough and also survives a re-issue of
# the certificate with the same name.
key_enrolled() {
	local list testkey
	# Capture first: `mokutil | grep -q` can turn a match into a false negative
	# under pipefail when grep exits before mokutil finished writing.  Ask
	# --test-key as an independent second source.
	list="$(mokutil --list-enrolled 2>/dev/null || true)"
	grep -qiF -- "${CN}" <<<"${list}" && return 0
	testkey="$(mokutil --test-key "${MOK_DER}" 2>/dev/null || true)"
	[[ "${testkey}" == *"is already enrolled"* ]]
}

# Print the post-reboot enrolment steps.  Deliberately concrete: the shim menu
# is keyboard driven and "Continue boot" - the default - silently skips the
# whole thing.
mok_steps() {
	cat <<MSG

What happens next - the module itself is fine, only this key needs enrolling:

  1. Reboot:
         sudo reboot

  2. Before Ubuntu starts you get the blue "MOK management" screen (shim).
     With the arrow keys and Enter choose:
         Enroll MOK  ->  View key  ->  Continue  ->  Yes
     "View key" must read:  CN=${CN}
     Then type the one-time password set when the enrolment was staged, and
     finish with  Reboot  from the menu.

     Do NOT pick "Continue boot" (the first entry): it skips the enrolment and
     the module stays rejected.  The one-time password is not stored anywhere.

  3. After that boot nothing else is needed: hp_wmi loads on its own and
     omen-macro-apply.service uploads the active profile as usual.  Check with
         sudo ./verify.sh
     or just press P1 - it types a numpad 1.

  4. If the blue screen never appeared, or you picked another entry, re-run
         sudo ./fix-secureboot.sh
     It is safe to re-run and prints the current state.

  Fallback: if you cannot get through the MOK screen, disabling Secure Boot in
  the BIOS/UEFI setup also makes the module load (unsigned), but then Windows
  or a firmware update may turn Secure Boot back on and the same error returns.
MSG
}

# Refresh the userspace tools from this repo so the improved diagnostics ship
# with the repair.  Only files whose content actually differs are written.
refresh_userspace() {
	say "refreshing userspace files in ${LIBDIR}"
	install -d "${LIBDIR}/omen_macro"
	local f dst changed=0
	for f in "${SCRIPT_DIR}"/omen_macro/*.py; do
		dst="${LIBDIR}/omen_macro/$(basename "${f}")"
		if [[ -e "${dst}" ]] && cmp -s "${f}" "${dst}"; then
			continue
		fi
		install -m644 "${f}" "${dst}"
		echo "  updated $(basename "${f}")"
		changed=1
	done
	[[ ${changed} -eq 1 ]] || echo "  already up to date"
	if [[ -f "${SCRIPT_DIR}/README.md" ]] && ! cmp -s "${SCRIPT_DIR}/README.md" "${LIBDIR}/README.md"; then
		install -m644 "${SCRIPT_DIR}/README.md" "${LIBDIR}/README.md"
	fi
	# Keep the docs that the README links to next to it.
	if [[ -d "${SCRIPT_DIR}/docs" ]]; then
		install -d "${LIBDIR}/docs"
		install -m644 "${SCRIPT_DIR}"/docs/*.md "${LIBDIR}/docs/"
	fi
	if [[ ! -x /usr/local/bin/omen-macro-apply ]]; then
		warn "no /usr/local/bin/omen-macro-apply - run sudo ./install.sh to finish the installation"
	fi
}

# The udev rule grants the omen-hw group write access; the rule normally runs
# from install.sh, re-installing it here keeps this script self-contained.
refresh_udev() {
	say "refreshing the udev rule and the omen-hw group"
	install -Dm644 "${SCRIPT_DIR}/udev/99-omen-macro.rules" /etc/udev/rules.d/99-omen-macro.rules
	udevadm control --reload-rules || true
	getent group omen-hw >/dev/null || groupadd omen-hw
	if [[ -n "${SUDO_USER:-}" ]] && ! id -nG "${SUDO_USER}" | tr ' ' '\n' | grep -qx omen-hw; then
		usermod -aG omen-hw "${SUDO_USER}" && echo "  added ${SUDO_USER} to group omen-hw (log out and in again)"
	fi
}

# Refuse to enroll a key that did not sign the module: that would only move the
# failure to the next reboot.  Prefer the serial (unique) and fall back to the
# signer name when openssl is not available.
verify_signature() {
	local modpath signer sigkey serial signed_serial
	modpath="$(modinfo -n hp_wmi 2>/dev/null || true)"
	if [[ -z "${modpath}" || ! -f "${modpath}" ]]; then
		modpath="/lib/modules/$(uname -r)/updates/dkms/hp-wmi.ko.zst"
	fi
	[[ -f "${modpath}" ]] || die "hp-wmi is not installed for $(uname -r) - install the hp-omen-extra DKMS package first (sudo apt install hp-omen-extra), then re-run this script"
	signer="$(modinfo -F signer "${modpath}" 2>/dev/null || true)"
	sigkey="$(modinfo -F sig_key "${modpath}" 2>/dev/null || true)"
	[[ -n "${sigkey}" ]] || die "${modpath} is not signed at all, so no key can be enrolled for it. Reinstall the DKMS package (sudo apt install --reinstall hp-omen-extra) so DKMS signs it again, then re-run this script"

	serial="$(mok_serial || true)"
	if [[ -n "${serial}" ]]; then
		signed_serial="$(tr -d ':' <<<"${sigkey}" | tr 'a-f' 'A-F')"
		[[ "${signed_serial}" == "${serial}" ]] || die "signature mismatch: ${modpath} is signed by key ${sigkey}, ${MOK_DER} is ${serial}. Enrolling this key would not help - rebuild the module (sudo dkms install -m hp-omen-extra -v <version> --force) and re-run this script"
		ok "module signed by ${MOK_DER} (serial ${sigkey})"
	else
		[[ "${signer}" == *"${CN}"* ]] || die "signature mismatch: module signer is '${signer}', ${MOK_DER} is '${CN}'. Enrolling this key would not help - rebuild the module and re-run this script"
		ok "module signed by '${signer}'"
	fi
}

# Load the module that is already installed and signed and make the interface
# usable.  Returns 1 when the interface still does not appear (the caller
# explains why); sets KEY_REJECTED when the kernel refused the signature.
runtime_fix() {
	say "loading the patched hp_wmi"
	modprobe -r hp_omen_extra 2>/dev/null || true
	modprobe -r hp_wmi 2>/dev/null || true
	local out="" rc=0
	out="$(modprobe hp_wmi 2>&1)" || rc=$?
	if [[ ${rc} -ne 0 ]]; then
		printf '%s\n' "${out}"
		[[ "${out}" == *"Key was rejected by service"* ]] && KEY_REJECTED=1
		return 1
	fi
	# hp_omen_extra (fan/thermal) is signed the same way and needs the same key;
	# it is not required for the macro keys, so never fail over it.
	modprobe hp_omen_extra 2>/dev/null || true
	for _ in $(seq 1 60); do
		[[ -e "${SYSFS}/macro_profile" ]] && break
		sleep 0.25
	done
	[[ -e "${SYSFS}/macro_profile" ]] || return 1
	chgrp omen-hw "${SYSFS}/macro_profile" "${SYSFS}/macro_status" 2>/dev/null || true
	chmod 0220 "${SYSFS}/macro_profile" 2>/dev/null || true
	chmod 0444 "${SYSFS}/macro_status" 2>/dev/null || true
	systemctl daemon-reload
	systemctl enable "${SVC}" >/dev/null 2>&1 || true
	systemctl restart "${SVC}" || true
	return 0
}

print_status() {
	echo
	if [[ -e "${SYSFS}/macro_profile" ]]; then
		ok "macro interface present ($(stat -c '%A %U:%G' "${SYSFS}/macro_profile" 2>/dev/null))"
		echo "  macro_status: $(tr '\n' ' ' < "${SYSFS}/macro_status" 2>/dev/null)"
	else
		warn "macro_profile is still missing"
	fi
	if systemctl is-active --quiet "${SVC}" 2>/dev/null; then
		ok "${SVC} is active"
	else
		warn "${SVC} is not active - systemctl status ${SVC}"
	fi
}

# ------------------------------------------------------------------ preflight
[[ ${EUID} -eq 0 ]] || die "run me with sudo"
command -v mokutil >/dev/null || die "mokutil is not installed - sudo apt install mokutil"
command -v modprobe >/dev/null || die "modprobe not found - is the kmod package installed?"

say "checking the Secure Boot state"
SB_STATE="$(mokutil --sb-state 2>&1 || true)"
case "${SB_STATE}" in
*"SecureBoot enabled"*)
	SB_ENABLED=1
	echo "  Secure Boot: enabled"
	;;
*"SecureBoot disabled"*)
	SB_ENABLED=0
	echo "  Secure Boot: disabled"
	;;
*)
	die "cannot tell whether Secure Boot is on (mokutil --sb-state: ${SB_STATE:-no output}). Refusing to guess - fix mokutil/efivarfs first"
	;;
esac

refresh_userspace
refresh_udev

# ------------------------------------------------- Secure Boot off: just load
if [[ ${SB_ENABLED} -eq 0 ]]; then
	say "Secure Boot is off - the module does not need the MOK key"
	if ! runtime_fix; then
		die "modprobe hp_wmi failed - see the kernel message above. Try a reboot with: sudo reboot, then re-run this script"
	fi
	print_status
	say "done - the macro keys work now, no reboot needed"
	exit 0
fi

# ------------------------------------------- Secure Boot on: key must be enrolled
say "checking the DKMS signing key and the installed module signature"
[[ -f "${MOK_DER}" ]] || die "${MOK_DER} is missing, so there is nothing to enroll. It is created by the DKMS signing setup: sudo apt install --reinstall hp-omen-extra, then re-run this script"
CN="$(mok_cn)"
ok "signing key: ${MOK_DER} (CN ${CN})"
verify_signature

if key_enrolled; then
	ok "the MOK key is already enrolled"
	if runtime_fix; then
		print_status
		say "done - the macro keys work now, no reboot needed"
		exit 0
	fi
	if [[ ${KEY_REJECTED} -eq 1 ]]; then
		# Enrolled a moment ago in the MOK screen: the kernel only picks the key
		# up on the next boot, so this is expected, not an error.
		echo
		echo "The kernel still rejects the module, which is normal right after the"
		echo "blue MOK screen: the key is only used from the next boot on."
		mok_steps
		exit 10
	fi
	die "hp_wmi did not load even though the key is enrolled - see the kernel message above, then try sudo reboot"
fi

# ------------------------------- Secure Boot on, key missing: stage the enrolment
# Capture the list first: piping mokutil into `grep -q` is unreliable under
# pipefail (grep may exit before mokutil finishes writing).
if grep -qiF -- "${CN}" <<<"$(mokutil --list-new 2>/dev/null || true)"; then
	ok "an enrolment request for '${CN}' is already staged"
else
	if [[ ! -t 0 ]]; then
		cat <<MSG

No terminal to type the one-time password into, so run this by hand first (the
steps after it are the same):

    sudo mokutil --import ${MOK_DER}

It asks twice for a password (at least 8 characters; it is not stored anywhere
and you will type it again in the blue MOK screen after the reboot).
MSG
	else
		say "staging the enrolment of ${MOK_DER}"
		echo "  mokutil asks for a one-time password twice (at least 8 characters);"
		echo "  you will type it again in the blue MOK management screen at boot."
		if ! mokutil --import "${MOK_DER}"; then
			die "mokutil --import failed - run it by hand: sudo mokutil --import ${MOK_DER}"
		fi
	fi
fi

mok_steps
exit 10
