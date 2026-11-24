# Troubleshooting

## The app says "hardware interface not found"

The patched driver is not loaded, so `/sys/devices/platform/hp-wmi/macro_profile`
does not exist. The banner and the status bar in the app name the reason.
Common causes:

| What you see | Fix |
|---|---|
| Secure Boot is on and the signing key is not enrolled | `sudo ./fix-secureboot.sh`, then confirm the blue MOK screen at the next boot. See [SECURE-BOOT.md](SECURE-BOOT.md). |
| Module was just rebuilt | Reboot once. |
| Module not installed for the running kernel | `sudo ./install.sh` |
| Permission denied writing | `sudo ./fix-boot.sh` (reinstalls the udev rule and permissions) |

Full check:

```bash
sudo ./verify.sh
```

It tests the driver, the signing key, the boot service, the keyboard return
codes and the key presses themselves.

## The keys stopped working after a reboot

The keyboard chip forgets the table on every cold boot, so a boot service
sends it again. If the keys are dead for a while and then start working, the
service ran late. Run `sudo ./fix-boot.sh` once; it fixes the unit ordering and
the udev rule without rebuilding anything.

Check what the service did:

```bash
systemctl status omen-macro-apply
journalctl -b -u omen-macro-apply
```

If it says `hp-wmi macro_profile never appeared`, see the first section above.
If it says `Key was rejected by service`, Secure Boot is rejecting the module:
[SECURE-BOOT.md](SECURE-BOOT.md).

## `macro_status` shows an error code

Read it with:

```bash
omen-macro-apply --status
```

* `profile_write_ret=0` and `mode_set_ret=0` and `applied=1` is good.
* A non-zero `profile_write_ret` means the keyboard rejected the table. Nothing
  is damaged — the engine just stays off. Please report the code.
* `ENODEV` or `EIO` while writing means the driver could not reach the keyboard
  controller (this firmware may not support WMI query 15).

## The app will not start

```bash
omen-macro
```

run from a terminal shows the Python error. The app needs GTK4
(`python3-gi`, `gir1.2-gtk-4.0`) and a normal desktop session.

## Keys type the wrong thing

Open the row with **Edit** and fix the step — see
[USING-THE-APP.md](USING-THE-APP.md#editing-a-macro-by-hand). Two keys that
were pressed one after the other sometimes get recorded as one chord; press
**Split** on that row.

---

## Boot timing (technical)

The EC forgets the table on a cold boot, so `omen-macro-apply.service` sends it
again. Two ordering pitfalls, both already handled in the shipped unit:

* Do **not** add `After=multi-user.target`. On some machines userspace takes
  minutes to reach that point, while the driver is ready a few seconds after
  boot; the keys stay dead until the upload happens. The unit starts as soon
  as `basic.target` is reached.
* `macro_profile` is created when the driver probes the device, which can be
  later than udev's `add` event. A rule with `TEST=="macro_profile"` would
  silently fail, so the shipped rule waits for the file and the unit's
  `ExecStartPost` also fixes the group.

`sudo ./fix-boot.sh` applies exactly those two fixes (unit + udev rule +
permissions, no rebuild). Re-running `install.sh` does the same but is slower
because it rebuilds the DKMS package.
