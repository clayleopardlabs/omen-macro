# Install, update and remove

## Before you start

* Ubuntu (tested on 26.04, kernel 7.0.0-43) on an HP OMEN laptop.
* The `hp-omen-extra` DKMS package installed. It provides the `hp-wmi`
  driver that this project patches. On this laptop it is version 2.1.2.
* `sudo` (the installer needs root to build a kernel module).

## Install

```bash
cd omen-macro
sudo ./install.sh
```

It takes a few minutes. The installer:

1. backs up and patches `hp-wmi.c` from `/usr/src/hp-omen-extra-2.1.2`;
2. rebuilds and reinstalls the DKMS module and reloads `hp_wmi`;
3. installs the app and command-line tools to `/usr/local`;
4. installs the udev rule, the app icon and the boot service;
5. sends the default profile to the keyboard.

The first run creates a **Default** profile with the stock numpad layout
(`P1` to `P6` type numpad `1` to `6`). If you have an old `profile.json`, it is
imported as Default.

If the keys do nothing right after installing, reboot once: the old module may
still have been loaded when the first profile was sent.

### Check that it worked

Press `P1` in a text editor. It should type a numpad `1`.

```bash
omen-macro-apply --status     # expect applied=1
sudo ./verify.sh              # full check, including Secure Boot
```

### Secure Boot is enabled

The kernel will not load the patched module until the signing key is enrolled
once. This is a one-time, two-minute step with one reboot:

```bash
sudo ./fix-secureboot.sh
sudo reboot
```

Then confirm the enrolment in the blue **MOK management** screen. Full steps
and explanations: [SECURE-BOOT.md](SECURE-BOOT.md).

## Update only the app (no rebuild, no reboot)

After changing the Python code, or after pulling new code from git:

```bash
sudo ./install-userspace.sh
```

This copies only the app files, launchers, icon and docs. It does **not** touch
the kernel module, the boot service or the keyboard table, so the keys keep
working while you do it. Close and reopen the app window afterwards.

## Remove everything

```bash
sudo ./revert.sh
```

This clears the keyboard table, restores the original `hp-wmi.c`, rebuilds the
DKMS module and removes the udev rule, services, icon and installed programs.
Your profiles in `/etc/omen-macro` are kept.

The enrolled Secure Boot key is not removed. It is harmless (it only vouches
for modules built on this machine). To remove it you would need another
confirmation in the blue MOK screen:

```bash
sudo mokutil --delete /var/lib/shim-signed/mok/MOK.der
# then reboot and confirm
```

## Files and folders

| Path | What |
|---|---|
| `/usr/local/bin/omen-macro` | the app launcher |
| `/usr/local/lib/omen-macro/` | app code and docs |
| `/etc/omen-macro/` | your profiles (`profiles/*.json`) and `active-profile` |
| `/etc/systemd/system/omen-macro-apply.service` | sends the profile at boot |
| `/etc/udev/rules.d/99-omen-macro.rules` | gives the app group access to the keyboard table |
| `/sys/devices/platform/hp-wmi/macro_profile` | the keyboard table (created by the patched driver) |
