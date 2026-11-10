#!/usr/bin/env bash
# Verify that the patched driver is live and that the EC really emits the
# configured keystrokes.
#
#   sudo ./verify.sh
#
# Checks, in order:
#   1. the patched hp-wmi is loaded and exposes the macro interface
#   2. macro_status reports a successful upload
#   3. presses on P1..P6 arrive as numpad key codes at the input layer
#
# Note: the macro keys replay real keystrokes, so if the terminal running this
# script has focus the digits 1..6 will be typed into it.  That is expected and
# is itself proof that the EC replay works.
set -uo pipefail

SYSFS="/sys/devices/platform/hp-wmi"
DURATION="${OMEN_MACRO_CAPTURE_SECONDS:-15}"
FAILS=0
FAILED_CHECKS=()

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
info() { printf '  ---- %s\n' "$*"; }
fail() { bad "$1"; FAILS=$((FAILS + 1)); FAILED_CHECKS+=("$2"); }

echo "== 1. driver =="
if [[ -e "${SYSFS}/macro_profile" && -e "${SYSFS}/macro_status" ]]; then
	ok "macro_profile and macro_status exist"
	info "permissions: $(stat -c '%A %U:%G' "${SYSFS}/macro_profile" 2>/dev/null)"
else
	bad "macro interface missing - the patched hp-wmi is not loaded"
	echo "     reboot, then re-run this script"
	exit 1
fi

LSMOD="$(lsmod)"
if grep -q '^hp_wmi[[:space:]]' <<<"${LSMOD}"; then
	ok "hp_wmi is loaded"
else
	fail "hp_wmi is not loaded" "hp_wmi loaded"
fi

echo
echo "== 2. EC upload status =="
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
echo "== 3. key capture =="
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
