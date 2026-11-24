#!/usr/bin/env bash
# Install/refresh only the userspace part of OMEN Macro Keys.
#
#   sudo ./install-userspace.sh
#
# Use this after changing the Python code (GUI, CLI, auto-switch daemon):
# it copies omen_macro/*.py, the README, the launchers and the desktop entry
# to /usr/local, then restarts the per-user auto-switch unit if it is running.
#
# It deliberately does NOT touch the kernel side: no DKMS build, no module
# reload, no udev/boot-service changes, no EC upload, no reboot.  Those belong
# to ./install.sh (first install) and ./fix-secureboot.sh (Secure Boot repair).
# Nothing here can break the macro keys; if the GUI is open, close and reopen
# it to pick up the new code.
#
# Exit codes: 0 done, 1 precondition failed (the message says which).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBDIR="/usr/local/lib/omen-macro"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

[[ ${EUID} -eq 0 ]] || die "run me with sudo"
[[ -f "${SCRIPT_DIR}/omen_macro/gui.py" ]] || die "${SCRIPT_DIR}/omen_macro/gui.py not found"

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

say "checking the import"
PYTHONPATH="${LIBDIR}" python3 -c "import omen_macro.gui, omen_macro.apply, omen_macro.boot_diag" \
	|| die "the installed modules do not import - nothing was removed, fix the code and re-run"

# The auto-switch daemon runs from the installed copy, so an already-running
# daemon keeps the old code until it is restarted.  Do not reach into the user
# session from root here: just tell the user.
if [[ -n "${SUDO_USER:-}" ]] && systemctl is-enabled omen-macro-auto.service >/dev/null 2>&1; then
	echo "  if the auto-switch daemon is running, restart it:"
	echo "    systemctl --user restart omen-macro-auto.service"
fi

say "done"
cat <<'DONE'
Close and reopen the "OMEN Macro Keys" window to use the new code.
The CLI picks it up immediately: omen-macro-apply --status

This did not touch the kernel module, the boot service or the EC table, so
the macro keys keep working throughout.
DONE
