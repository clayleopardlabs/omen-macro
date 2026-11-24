"""Unit checks for boot_diag: no hardware, no root, no real probe commands.

Run: python3 tests/test_boot_diag.py

Every scenario is built either from a synthetic Environment or from
probe_environment() pointed at temp files with a fake runner, so the results do
not depend on this machine's Secure Boot or loaded modules.  The one integration
check runs apply.main() against a non-existent --sysfs directory, which always
takes the missing-interface branch without touching the real config.
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omen_macro import apply, boot_diag, ec


class FakeRunner:
    """Stand-in for subprocess: canned output per argv prefix, records calls.

    An argv that matches no prefix returns None, i.e. "tool not available".
    """

    def __init__(self, answers: dict[tuple[str, ...], str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> str | None:
        self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return answer
        return None


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="omen-bootdiag-"))


def _environment(**changes) -> boot_diag.Environment:
    """A fully populated environment; override just the fields a test cares about."""
    values = dict(
        sysfs=Path("/sys/devices/platform/hp-wmi"),
        secure_boot=True,
        module_loaded=False,
        module_file="/lib/modules/6.0-test/updates/dkms/hp-wmi.ko.zst",
        signer="Omen Secure Boot Module Signature key",
        signer_enrolled=False,
        lockdown="integrity",
        kernel_release="6.0-test",
        permission_error=None,
    )
    values.update(changes)
    return boot_diag.Environment(**values)


def _probe(tmp: Path, run: FakeRunner, **overrides) -> boot_diag.Environment:
    """Probe temp-dir lookalikes so no real /sys or tool is required."""
    options = dict(
        efivars=tmp / "SecureBoot-efivar",
        module_root=tmp / "sys-module-hp_wmi",
        modules_file=tmp / "proc-modules",
        modules_dir=tmp / "lib-modules",
        mok_cert=tmp / "MOK.der",
        lockdown_path=tmp / "lockdown",
        kernel_release="6.0-test",
        run=run,
    )
    options.update(overrides)
    return boot_diag.probe_environment(tmp / "hp-wmi", **options)


# --------------------------------------------------------------------------- #
# Scenarios built from facts
# --------------------------------------------------------------------------- #
def test_secure_boot_key_rejected():
    diag = boot_diag.explain(_environment())
    assert diag.scenario == "secureboot-key-rejected"
    assert "not enrolled" in diag.summary
    lines = diag.hint.splitlines()
    # Copy-pasteable enrol command, no rebuild and no install.sh red herring.
    assert "sudo mokutil --import /var/lib/shim-signed/mok/MOK.der" in lines
    assert "sudo ./fix-secureboot.sh" in lines
    assert "sudo ./install.sh" not in diag.hint
    assert "Key was rejected by service" in " ".join(diag.hint.split())
    assert "journalctl -k -b | grep -i hp_wmi" in diag.hint
    assert "Omen Secure Boot Module Signature key" in diag.hint


def test_secure_boot_enrolled_but_still_missing():
    diag = boot_diag.explain(_environment(signer_enrolled=True))
    assert diag.scenario == "secureboot-enrolled"
    assert "still not loaded" in diag.summary
    assert "journalctl -k -b | grep -i hp_wmi" in diag.hint
    assert "sudo modprobe hp_wmi" in diag.hint
    # No point telling the user to enrol a key that is already enrolled.
    assert "mokutil --import" not in diag.hint


def test_secure_boot_unsigned_module():
    diag = boot_diag.explain(_environment(signer=None, signer_enrolled=None))
    assert diag.scenario == "secureboot-unsigned"
    assert "unsigned" in diag.summary
    assert "sudo ./install.sh" in diag.hint
    assert "sudo mokutil --import /var/lib/shim-signed/mok/MOK.der" in diag.hint


def test_secure_boot_unverified_signer():
    diag = boot_diag.explain(_environment(signer_enrolled=None))
    assert diag.scenario == "secureboot-unverified"
    assert "could not be checked" in diag.summary
    assert "mokutil --sb-state" in diag.hint
    assert "mokutil --import" in diag.hint


def test_secure_boot_off_module_not_loaded():
    diag = boot_diag.explain(
        _environment(secure_boot=False, signer=None, signer_enrolled=None,
                     lockdown="none")
    )
    assert diag.scenario == "module-not-loaded"
    assert "not loaded" in diag.summary
    assert "Secure Boot is off" in diag.summary
    assert "sudo modprobe hp_wmi" in diag.hint
    assert "sudo ./install.sh" in diag.hint
    assert "integrity" not in diag.hint


def test_module_not_installed():
    diag = boot_diag.explain(
        _environment(module_file=None, secure_boot=False, signer=None,
                     signer_enrolled=None)
    )
    assert diag.scenario == "module-not-installed"
    assert "not installed" in diag.summary
    assert "6.0-test" in diag.summary
    assert "sudo ./install.sh" in diag.hint
    assert "mokutil --import" not in diag.hint  # Secure Boot is off


def test_module_not_installed_with_secure_boot_on():
    diag = boot_diag.explain(_environment(module_file=None))
    assert diag.scenario == "module-not-installed"
    assert "mokutil --import" in diag.hint


def test_module_loaded_stock_driver():
    stock = "/lib/modules/6.0-test/kernel/drivers/platform/x86/hp-wmi.ko.zst"
    diag = boot_diag.explain(
        _environment(module_loaded=True, secure_boot=None, signer=None,
                     signer_enrolled=None, module_file=stock, lockdown=None)
    )
    assert diag.scenario == "module-loaded"
    assert "stock" in diag.summary
    assert "sudo ./install.sh" in diag.hint


def test_module_loaded_but_no_attribute():
    # DKMS build is loaded yet registered no attribute: not a stock-module case.
    diag = boot_diag.explain(_environment(module_loaded=True))
    assert diag.scenario == "module-loaded"
    assert "may be the stock" in diag.summary or "unpatched" in diag.summary
    assert "journalctl -k -b | grep -i hp_wmi" in diag.hint


def test_permission_hint():
    diag = boot_diag.explain(
        _environment(permission_error="[Errno 13] Permission denied")
    )
    assert diag.scenario == "permission"
    assert "cannot access" in diag.summary
    assert "sudo usermod -aG omen-hw" in diag.hint
    assert "getent group omen-hw" in diag.hint
    assert "mokutil --import" not in diag.hint


def test_hints_are_clean_and_copy_pasteable():
    favourites = [
        _environment(),
        _environment(signer_enrolled=True),
        _environment(signer=None, signer_enrolled=None),
        _environment(signer_enrolled=None),
        _environment(secure_boot=False, signer=None, signer_enrolled=None),
        _environment(module_file=None),
        _environment(module_loaded=True),
        _environment(permission_error="[Errno 13] Permission denied"),
    ]
    for environment in favourites:
        diag = boot_diag.explain(environment)
        assert diag.summary == " ".join(diag.summary.split())
        assert "\n" not in diag.summary
        assert diag.hint == diag.hint.strip()
        assert diag.hint
        assert "hardware interface not found - run install.sh" not in diag.hint
        for line in diag.hint.splitlines():
            # Commands are flush-left and carry no "$ " prompt to strip.
            assert not line.startswith("$ ")
            assert not line.lstrip().startswith("$ ")
            assert line == line.rstrip()


# --------------------------------------------------------------------------- #
# Probe behaviour: temp dirs + fake commands
# --------------------------------------------------------------------------- #
def test_probe_without_any_tool_degrades():
    tmp = _tmp()
    runner = FakeRunner()
    environment = _probe(tmp, runner)
    assert environment.secure_boot is None
    assert environment.module_loaded is False
    assert environment.module_file is None
    assert environment.signer is None
    assert environment.signer_enrolled is None
    assert environment.lockdown is None
    diag = boot_diag.explain(environment)
    assert diag.scenario == "module-not-installed"
    assert "could not be determined" in diag.hint


def test_efivar_decoding():
    tmp = _tmp()
    on = tmp / "SecureBoot-on"
    on.write_bytes(b"\x06\x00\x00\x00\x01")
    off = tmp / "SecureBoot-off"
    off.write_bytes(b"\x06\x00\x00\x00\x00")
    assert _probe(tmp, FakeRunner(), efivars=on).secure_boot is True
    assert _probe(tmp, FakeRunner(), efivars=off).secure_boot is False


def test_secure_boot_mokutil_fallback():
    tmp = _tmp()
    runner = FakeRunner({("mokutil", "--sb-state"): "SecureBoot enabled\n"})
    environment = _probe(tmp, runner)
    assert environment.secure_boot is True
    assert ["mokutil", "--sb-state"] in runner.calls


def test_probe_is_read_only():
    tmp = _tmp()
    efivars = tmp / "SecureBoot"
    efivars.write_bytes(b"\x06\x00\x00\x00\x01")
    modules = tmp / "proc-modules"
    modules.write_text("")
    runner = FakeRunner({
        ("modinfo", "-n", "hp_wmi"):
            "/lib/modules/6.0-test/updates/dkms/hp-wmi.ko.zst\n",
        ("modinfo", "-F", "signer", "hp_wmi"):
            "Omen Secure Boot Module Signature key\n",
        ("mokutil", "--list-enrolled"):
            "Subject: CN = Omen Secure Boot Module Signature key\n"
            "Certificate:\n",
    })
    environment = _probe(tmp, runner, efivars=efivars, modules_file=modules)
    assert environment.module_file == \
        "/lib/modules/6.0-test/updates/dkms/hp-wmi.ko.zst"
    assert environment.signer == "Omen Secure Boot Module Signature key"
    assert environment.signer_enrolled is True
    assert environment.secure_boot is True
    # Enrolment already answered by the listing: no key-import or key-test.
    assert not any("--test-key" in call for call in runner.calls)
    forbidden = ("--import", "--enroll", "--disable-validation", "--delete")
    assert runner.calls
    for call in runner.calls:
        assert not [arg for arg in call if arg in forbidden]


def test_signer_enrolled_test_key_fallback():
    tmp = _tmp()
    cert = tmp / "MOK.der"
    cert.write_bytes(b"fake der")
    not_enrolled = FakeRunner({
        ("mokutil", "--list-enrolled"): "",
        ("mokutil", "--test-key"): f"{cert} is not enrolled\n",
    })
    enrolled = FakeRunner({
        ("mokutil", "--list-enrolled"): "",
        ("mokutil", "--test-key"): f"{cert} is already enrolled\n",
    })
    assert boot_diag.signer_enrolled("Some key", cert, not_enrolled) is False
    assert boot_diag.signer_enrolled("Some key", cert, enrolled) is True
    # mokutil unusable and no certificate on disk -> unknown, never raises.
    assert boot_diag.signer_enrolled("Some key", tmp / "missing.der",
                                     FakeRunner()) is None


def test_module_loaded_proc_fallback():
    tmp = _tmp()
    modules = tmp / "proc-modules"
    modules.write_text("hp_wmi 16384 0 - Live 0x0000000000000000\nother 4 0\n")
    environment = _probe(tmp, FakeRunner(), modules_file=modules,
                         module_root=tmp / "no-sys-module")
    assert environment.module_loaded is True
    # The sysfs module directory alone is enough too.
    (tmp / "sys-module-hp_wmi").mkdir()
    assert _probe(tmp, FakeRunner(), modules_file=modules).module_loaded is True


def test_module_path_dkms_glob():
    tmp = _tmp()
    dkms = tmp / "lib-modules" / "6.0-test" / "updates" / "dkms"
    dkms.mkdir(parents=True)
    module = dkms / "hp-wmi.ko.zst"
    module.write_bytes(b"module")
    # modinfo fails (not installed in the usual place): glob must find it.
    runner = FakeRunner({
        ("modinfo", "-n", "hp_wmi"): "modinfo: ERROR: Module hp_wmi not found.\n",
    })
    assert boot_diag.module_path(runner, 1.0, "6.0-test",
                                 tmp / "lib-modules") == str(module)
    # Nothing anywhere -> None.
    assert boot_diag.module_path(FakeRunner(), 1.0, "7.0-test",
                                 tmp / "lib-modules") is None


def test_lockdown_parsing():
    tmp = _tmp()
    path = tmp / "lockdown"
    path.write_text("none [integrity] confidentiality\n")
    assert boot_diag.lockdown_state(path) == "integrity"
    assert boot_diag.lockdown_state(tmp / "missing") is None


def test_permission_probe_with_temp_dir():
    if os.geteuid() == 0:
        print("     (running as root: permission check skipped)")
        return
    tmp = _tmp()
    sysfs = tmp / "hp-wmi"
    sysfs.mkdir()
    os.chmod(sysfs, 0o000)
    try:
        environment = _probe(tmp, FakeRunner())
    finally:
        os.chmod(sysfs, 0o700)
    assert environment.permission_error is not None
    assert boot_diag.explain(environment).scenario == "permission"


def test_constants_match_ec():
    assert boot_diag.PROFILE_ATTR == ec.PROFILE_ATTR
    assert boot_diag.DEFAULT_SYSFS == Path(ec.DEFAULT_SYSFS)


def test_diagnose_missing_interface_never_raises():
    tmp = _tmp()
    diag = boot_diag.diagnose_missing_interface(
        tmp / "nowhere",
        efivars=tmp / "no-efivar",
        module_root=tmp / "no-module",
        modules_file=tmp / "no-modules",
        modules_dir=tmp / "no-lib",
        mok_cert=tmp / "no-cert",
        lockdown_path=tmp / "no-lockdown",
        kernel_release="9.9.9-test",
        run=FakeRunner(),
    )
    assert isinstance(diag, boot_diag.Diagnosis)
    assert diag.summary and diag.hint
    assert "9.9.9-test" in diag.summary


# --------------------------------------------------------------------------- #
# CLI wiring: apply must print the reason, not the old install.sh advice
# --------------------------------------------------------------------------- #
def test_apply_prints_diagnosis():
    tmp = _tmp()
    old_root = os.environ.get("OMEN_MACRO_ROOT")
    os.environ["OMEN_MACRO_ROOT"] = str(tmp / "config-root")
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = apply.main(["--sysfs", str(tmp / "no-hp-wmi")])
    finally:
        if old_root is None:
            os.environ.pop("OMEN_MACRO_ROOT", None)
        else:
            os.environ["OMEN_MACRO_ROOT"] = old_root
    stderr = err.getvalue()
    assert code == 3, f"expected return code 3, got {code}: {stderr}"
    assert "hardware interface not found" in stderr
    assert "reason:" in stderr
    # The old advice is gone; a concrete next step is present instead.
    assert "The patched hp-wmi module is not loaded - run install.sh" not in stderr
    assert "sudo " in stderr


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
