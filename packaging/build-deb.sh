#!/usr/bin/env bash
# Build the omen-macro Debian package.
#
#   packaging/build-deb.sh [output-dir]
#
# The package contains the userspace app, docs, icon, systemd units, udev rule
# and the kernel patch.  Its post-install step patches and rebuilds the hp-wmi
# driver from the hp-omen-extra DKMS package, so that package has to be
# installed on the target machine; if it is not, the app is still installed and
# a message explains what to run later.
#
# The output name is omen-macro_<version>_all.deb, with a .sha256 next to it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
PKG="omen-macro"
ARCH="all"
OUT="${1:-${REPO}/dist}"
mkdir -p "${OUT}"

VERSION="${VERSION:-$(git -C "${REPO}" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')}"
[[ -n "${VERSION}" ]] || VERSION="0.0.0"
DEB="${PKG}_${VERSION}_${ARCH}.deb"

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
ROOT="${STAGE}/${PKG}_${VERSION}_${ARCH}"

say() { printf '==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "${REPO}/omen_macro/gui.py" ]] || die "run me from the omen-macro checkout"

# ------------------------------------------------------------------ payload
say "staging ${PKG} ${VERSION}"
install -d "${ROOT}/DEBIAN" \
	"${ROOT}/usr/bin" \
	"${ROOT}/usr/lib/${PKG}/omen_macro" \
	"${ROOT}/usr/lib/${PKG}/kernel" \
	"${ROOT}/usr/share/doc/${PKG}/docs" \
	"${ROOT}/usr/share/applications" \
	"${ROOT}/usr/share/icons/hicolor/scalable/apps" \
	"${ROOT}/usr/lib/systemd/system" \
	"${ROOT}/usr/lib/systemd/user" \
	"${ROOT}/usr/lib/udev/rules.d"

install -m644 "${REPO}"/omen_macro/*.py "${ROOT}/usr/lib/${PKG}/omen_macro/"
install -m644 "${REPO}/README.md" "${ROOT}/usr/share/doc/${PKG}/README.md"
install -m644 "${REPO}"/screenshot*.webp "${ROOT}/usr/share/doc/${PKG}/"
install -m644 "${REPO}"/docs/*.md "${ROOT}/usr/share/doc/${PKG}/docs/"
install -m644 "${REPO}/desktop/omen-macro.desktop" \
	"${ROOT}/usr/share/applications/omen-macro.desktop"
install -m644 "${REPO}/desktop/omen-macro.svg" \
	"${ROOT}/usr/share/icons/hicolor/scalable/apps/omen-macro.svg"
install -m644 "${REPO}/kernel/hp-wmi-macro.patch" "${ROOT}/usr/lib/${PKG}/kernel/"
install -m755 "${REPO}/verify.sh" "${ROOT}/usr/bin/omen-macro-verify"
install -m755 "${SCRIPT_DIR}/kernel-setup.sh" "${ROOT}/usr/lib/${PKG}/kernel-setup.sh"
install -m755 "${SCRIPT_DIR}/kernel-teardown.sh" "${ROOT}/usr/lib/${PKG}/kernel-teardown.sh"

# Command line launchers: same entry points as the git install, but the code
# lives in /usr/lib instead of /usr/local/lib.
write_launcher() {
	local name="$1" module="$2"
	cat > "${ROOT}/usr/bin/${name}" <<LAUNCHER
#!/bin/sh
export PYTHONPATH=/usr/lib/${PKG}\${PYTHONPATH:+:\$PYTHONPATH}
exec python3 -m ${module} "\$@"
LAUNCHER
	chmod 755 "${ROOT}/usr/bin/${name}"
}
write_launcher omen-macro omen_macro.gui
write_launcher omen-macro-apply omen_macro.apply
write_launcher omen-macro-auto omen_macro.auto_switch

# Units: package paths differ from the git install, so rewrite the two paths
# that are embedded in them.
for unit in omen-macro-apply.service omen-macro-auto.service; do
	dst="${ROOT}/usr/lib/systemd/system/${unit}"
	[[ "${unit}" == *auto* ]] && dst="${ROOT}/usr/lib/systemd/user/${unit}"
	sed -e "s#/usr/local/bin/omen-macro#/usr/bin/omen-macro#g" \
		-e "s#file:///usr/local/lib/omen-macro/#file:///usr/share/doc/omen-macro/#g" \
		"${REPO}/systemd/${unit}" > "${dst}"
	chmod 644 "${dst}"
done

install -m644 "${REPO}/udev/99-omen-macro.rules" \
	"${ROOT}/usr/lib/udev/rules.d/99-omen-macro.rules"

# ------------------------------------------------------------------ control
INSTALLED_SIZE="$(du -sk "${ROOT}" | cut -f1)"
cat > "${ROOT}/DEBIAN/control" <<CONTROL
Package: ${PKG}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-pango-1.0, systemd, kmod
Recommends: dkms, patch
Installed-Size: ${INSTALLED_SIZE}
Maintainer: clayleopardlabs <contactleopardlabs@gmail.com>
Homepage: https://github.com/clayleopardlabs/omen-macro
Description: macro keys and GUI for HP OMEN laptops on Linux
 Makes the six macro keys (P1-P6) on an HP OMEN laptop work on Linux, with
 a GTK4 app to record, edit and manage macros, per-key commands, profiles
 and optional profile switching by focused application.
 .
 The package patches and rebuilds the hp-wmi driver from the hp-omen-extra
 DKMS package in its post-install step, so that package must be installed
 first. With Secure Boot enabled, the module signing key has to be enrolled
 once (see /usr/share/doc/omen-macro/docs/SECURE-BOOT.md).
CONTROL

install -m755 "${SCRIPT_DIR}/postinst" "${ROOT}/DEBIAN/postinst"
install -m755 "${SCRIPT_DIR}/prerm" "${ROOT}/DEBIAN/prerm"
install -m755 "${SCRIPT_DIR}/postrm" "${ROOT}/DEBIAN/postrm"

# changelog and checksums that dpkg tools expect
{
	cat <<CHANGELOG
${PKG} (${VERSION}) unstable; urgency=medium

  * Release ${VERSION}: GTK4 app, linked to the patched hp-wmi driver.

 -- clayleopardlabs <contactleopardlabs@gmail.com>  $(date -R)
CHANGELOG
} | gzip -9n > "${ROOT}/usr/share/doc/${PKG}/changelog.Debian.gz"
chmod 644 "${ROOT}/usr/share/doc/${PKG}/changelog.Debian.gz"

(
	cd "${ROOT}"
	find . -type f ! -path "./DEBIAN/*" -printf '%P\n' | sort | while read -r file; do
		printf '%s  %s\n' "$(md5sum "${file}" | cut -d' ' -f1)" "${file}"
	done > DEBIAN/md5sums
)
chmod 644 "${ROOT}/DEBIAN/md5sums"

# ------------------------------------------------------------------- build
say "building ${DEB}"
dpkg-deb --build --root-owner-group "${ROOT}" "${OUT}/${DEB}" >/dev/null
(
	cd "${OUT}"
	sha256sum "${DEB}" > "${DEB}.sha256"
)
say "done"
printf '\n  %s\n  %s\n' "${OUT}/${DEB}" "${OUT}/${DEB}.sha256"
printf '\nInstall with:\n  sudo apt install %s\n' "${OUT}/${DEB}"
