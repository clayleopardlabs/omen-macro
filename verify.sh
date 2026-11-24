#!/usr/bin/env bash
# Verify that the patched driver is live and that the EC really emits the
# configured keystrokes.
#
#   sudo ./verify.sh
#
# Checks, in order:
#   1. Secure Boot state, the DKMS signing key and the installed module's
#      signature (a Secure Boot rejection is the usual cause of a missing
#      macro interface since Secure Boot was enabled from Windows)
#   2. the patched hp-wmi is loaded and exposes the macro interface, and the
#      boot service is active
#   3. macro_status reports a successful upload
#   4. presses on P1..P6 arrive as numpad key codes at the input layer
#
# Note: the macro keys replay real keystrokes, so if the terminal running this
# script has focus the digits 1..6 will be typed into it.  That is expected and
# is itself proof that the EC replay works.
set -uo pipefail

SYSFS="/sys/devices/platform/hp-wmi"
DURATION="${OMEN_MACRO_CAPTURE_SECONDS:-15}"
MOK_DER="/var/lib/shim-signed/mok/MOK.der"
DEFAULT_MOK_CN="Omen Secure Boot Module Signature key"
SVC="omen-macro-apply.service"
FAILS=0
FAILED_CHECKS=()

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
info() { printf '  ---- %s\n' "$*"; }
fail() { bad "$1"; FAILS=$((FAILS + 1)); FAILED_CHECKS+=("$2"); }

echo "== 1. Secure Boot / module signature =="
SB_ENABLED=0
MOK_OK=0
if command -v mokutil >/dev/null 2>&1; then
	SB_STATE="$(mokutil --sb-state 2>&1 || true)"
	if [[ "${SB_STATE}" == *"SecureBoot enabled"* ]]; then
		SB_ENABLED=1
		info "Secure Boot: enabled"
	elif [[ "${SB_STATE}" == *"SecureBoot disabled"* ]]; then
		info "Secure Boot: disabled"
	else
		info "Secure Boot: unknown (${SB_STATE:-mokutil gave no output})"
	fi
else
	info "Secure Boot: mokutil is not installed, cannot check"
fi

# The DKMS signing key's CN.  openssl is optional: the fallback is the name
# every hp-omen-extra 2.1.2 install uses.
MOK_CN="${DEFAULT_MOK_CN}"
if [[ -r "${MOK_DER}" ]] && command -v openssl >/dev/null 2>&1; then
	_CN="$(openssl x509 -inform DER -in "${MOK_DER}" -noout -subject 2>/dev/null \
		| sed -n 's/^subject=.*CN *= *//p' || true)"
	if [[ -n "${_CN}" ]]; then MOK_CN="${_CN}"; fi
fi

MODPATH="$(modinfo -n hp_wmi 2>/dev/null || true)"
if [[ -n "${MODPATH}" && -f "${MODPATH}" ]]; then
	SIGNER="$(modinfo -F signer "${MODPATH}" 2>/dev/null || true)"
	SIGKEY="$(modinfo -F sig_key "${MODPATH}" 2>/dev/null || true)"
	info "module: ${MODPATH}"
	if [[ -n "${SIGKEY}" ]]; then
		info "signer: ${SIGNER} (sig_key ${SIGKEY})"
	else
		warn "the installed module carries no signature"
		if [[ ${SB_ENABLED} -eq 1 ]]; then
			fail "hp_wmi is unsigned but Secure Boot is enabled - the kernel refuses it" "module signed"
		fi
	fi
else
	fail "hp_wmi is not installed for $(uname -r) (hp-omen-extra DKMS package missing)" "module signed"
	info "install it with: sudo ./install.sh"
fi

if [[ -r "${MOK_DER}" ]]; then
	info "DKMS key: ${MOK_DER} (CN ${MOK_CN})"
else
	info "DKMS key: ${MOK_DER} is missing (DKMS signing not set up)"
fi

if command -v mokutil >/dev/null 2>&1; then
	# Read each list once and match in the shell instead of piping into
	# `grep -q`: with pipefail a grep that exits before mokutil has finished
	# writing turns a successful match into a false "not enrolled" (seen right
	# after a boot).  --test-key is an independent answer to the same question
	# and also covers the case where the list itself cannot be read yet.
	MOK_PENDING="$(mokutil --list-new 2>/dev/null || true)"
	MOK_ENROLLED="$(mokutil --list-enrolled 2>/dev/null || true)"
	MOK_TESTKEY="$(mokutil --test-key "${MOK_DER}" 2>/dev/null || true)"
	if grep -qF -- "${MOK_CN}" <<<"${MOK_PENDING}"; then
		fail "an MOK enrolment is pending - reboot and confirm it in the blue MOK screen (Enroll MOK -> ...)" "MOK key enrolled"
	elif grep -qF -- "${MOK_CN}" <<<"${MOK_ENROLLED}" \
		|| [[ "${MOK_TESTKEY}" == *"is already enrolled"* ]]; then
		MOK_OK=1
		ok "the DKMS key is enrolled in shim's MOK list"
	elif [[ -z "${MOK_ENROLLED//[[:space:]]/}" && "${MOK_TESTKEY}" != *"is not enrolled"* ]]; then
		warn "cannot read the MOK list right now - re-run this check in a moment"
	elif [[ ${SB_ENABLED} -eq 1 ]]; then
		fail "the DKMS key is NOT enrolled, so the kernel rejects hp_wmi (\"Key was rejected by service\")" "MOK key enrolled"
		echo "       fix: sudo ./fix-secureboot.sh   (then confirm the blue MOK screen at reboot)"
	else
		info "the DKMS key is not enrolled (harmless while Secure Boot is off)"
	fi
fi

echo
echo "== 2. driver =="
if [[ -e "${SYSFS}/macro_profile" && -e "${SYSFS}/macro_status" ]]; then
	ok "macro_profile and macro_status exist"
	info "permissions: $(stat -c '%A %U:%G' "${SYSFS}/macro_profile" 2>/dev/null)"
else
	bad "macro interface missing - the patched hp-wmi is not loaded"
	if [[ ${SB_ENABLED} -eq 1 && ${MOK_OK} -eq 0 ]]; then
		echo "     Secure Boot is enabled and the DKMS key is not enrolled, so the"
		echo "     kernel rejects the module.  Run:  sudo ./fix-secureboot.sh"
		echo "     then confirm the enrolment in the blue MOK screen at reboot."
	else
		echo "     reboot, then re-run this script (or sudo ./install.sh)"
	fi
	exit 1
fi

LSMOD="$(lsmod)"
if grep -q '^hp_wmi[[:space:]]' <<<"${LSMOD}"; then
	ok "hp_wmi is loaded"
else
	fail "hp_wmi is not loaded" "hp_wmi loaded"
fi

if systemctl is-active --quiet "${SVC}" 2>/dev/null; then
	ok "${SVC} is active"
else
	fail "${SVC} is not active - the profile is not re-uploaded at boot (systemctl status ${SVC})" "boot service active"
fi

echo
echo "== 3. EC upload status =="
if [[ -r "${SYSFS}/macro_status" ]]; then
	STATUS="$(cat "${SYSFS}/macro_status")"
	printf '%s\n' "${STATUS}" | sed 's/^/     /'
	if grep -q 'applied=1' <<<"${STATUS}"; then
		ok "the EC accepted the table and the macro engine is enabled"
	else
		fail "the EC did not accept the table (see the return codes above)" "EC accepted the table"
	fi
else
	fail "cannot read ${SYSFS}/macro_status" "EC accepted the table"
fi

echo
echo "== 4. key capture =="
echo "  Press P1, P2, P3, P4, P5, P6 now (${DURATION} seconds)..."
python3 - "${DURATION}" <<'PY'
import glob
import select
import struct
import sys
import time

duration = float(sys.argv[1])

# Numpad key codes (linux/input-event-codes.h).  Matched by code, not by name,
# because the header spells them KEY_KP1 while /proc and evdev call them KP1.
EXPECTED = {79: "KP1", 80: "KP2", 81: "KP3", 75: "KP4", 76: "KP5", 77: "KP6"}

fds = {}
for path in sorted(glob.glob("/dev/input/event*")):
    try:
        fds[path] = open(path, "rb", buffering=0)
    except OSError:
        continue

if not fds:
    print("  FAIL no readable /dev/input/event* (run with sudo)")
    sys.exit(1)

seen = []
deadline = time.time() + duration
while time.time() < deadline:
    ready, _, _ = select.select(list(fds.values()), [], [], 0.5)
    for handle in ready:
        data = handle.read(24)
        if len(data) < 24:
            continue
        _sec, _usec, etype, code, value = struct.unpack("llHHi", data)
        if etype != 1 or value != 1:      # EV_KEY, press only
            continue
        seen.append(code)
        label = EXPECTED.get(code)
        if label:
            print(f"    press: {label} (code {code})  <-- macro key")
        elif code >= 100:
            print(f"    press: code {code} (not a macro key)")

found = {code for code in seen if code in EXPECTED}
print()
if found:
    names = ", ".join(EXPECTED[c] for c in sorted(found))
    print(f"  PASS {len(found)}/6 macro keys delivered keystrokes: {names}")
else:
    print("  FAIL no macro-key keystrokes seen")
    print("       - were P1-P6 pressed while the script was running?")
    print("       - if the upload status above is PASS, reboot once and retry")
    sys.exit(1)
missing = [EXPECTED[c] for c in EXPECTED if c not in found]
if missing:
    print(f"       missing: {', '.join(missing)}")
    sys.exit(1)
sys.exit(0)
PY
rc=$?
if [[ ${rc} -ne 0 ]]; then
	FAILS=$((FAILS + 1))
	FAILED_CHECKS+=("macro keys delivered keystrokes")
fi

echo
if [[ ${FAILS} -eq 0 ]]; then
	printf '\033[32mAll checks passed\033[0m - P1..P6 are working.\n'
else
	printf '\033[31m%d check(s) failed\033[0m: %s\n' "${FAILS}" "$(IFS=', '; echo "${FAILED_CHECKS[*]}")"
	echo "See README.md#troubleshooting."
fi
exit "${FAILS}"
