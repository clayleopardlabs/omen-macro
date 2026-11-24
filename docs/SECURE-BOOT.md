# Secure Boot

If Secure Boot is enabled, Ubuntu only loads a kernel module that is signed by
a key it trusts. DKMS signs the patched `hp_wmi` module with a key that belongs
to this machine:

```
/var/lib/shim-signed/mok/MOK.der    (CN=Omen Secure Boot Module Signature key)
```

That key is not trusted until you **enrol it once**. Until then every boot
logs:

```
modprobe: ERROR: could not insert 'hp_wmi': Key was rejected by service
```

and the app shows *hardware interface not found*. Nothing is broken: the
module is built and signed correctly. Only the enrolment is missing. No
rebuild is needed. The fan/thermal module (`hp_omen_extra`) is signed the same
way and is rejected for the same reason.

## Fix it (one time)

```bash
cd omen-macro
sudo ./fix-secureboot.sh     # asks for a one-time password (8+ characters)
sudo reboot
```

Before Ubuntu starts you get a blue **MOK management** screen. It only appears
when an enrolment is waiting.

1. Choose **Enroll MOK**
2. Choose **View key**. It must say `CN=Omen Secure Boot Module Signature key`
3. **Continue**, then **Yes**
4. Type the one-time password from step 1
5. **Reboot**

Do **not** pick **Continue boot** (the first item): it skips the enrolment and
the keys stay dead; you would have to run the script again.

That is all. On the next boot `hp_wmi` loads by itself and the boot service
sends your profile. Check with `sudo ./verify.sh` or press `P1`.

## Run it again safely

`fix-secureboot.sh` is safe to re-run. It:

* checks that the installed module really is signed by `MOK.der`;
* stages the enrolment if it is missing (or says it is already staged);
* reloads `hp_wmi` and restarts the boot service if the key is already
  enrolled;
* refreshes the app files from the repo.

It never rebuilds DKMS, edits kernel sources, changes firmware/BIOS settings
or uses the network.

Exit codes: `0` = keys work now; `10` = reboot and confirm the enrolment;
`1` = something failed (the message says what).

## If you would rather turn Secure Boot off

That also works: an unsigned module loads when Secure Boot is off. But the keys
die again as soon as Secure Boot is switched back on, and Windows or a firmware
update may switch it back on for you (this is exactly what happened on the
machine this was developed on). Keeping Secure Boot on and enrolling the key is
the stable option. The key vouches only for modules built on this machine.

## If the module is not signed at all

If this prints nothing:

```bash
modinfo -F signer hp_wmi
```

then the module has no signature, and enrolling a key cannot help. Reinstall
the DKMS package so DKMS signs it again, then run `fix-secureboot.sh`:

```bash
sudo apt install --reinstall hp-omen-extra
```
