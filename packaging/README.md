# Building the Debian package

The package installs the app into normal system paths (`/usr/bin`,
`/usr/lib/omen-macro`) and its post-install step patches and rebuilds the
`hp-wmi` driver from the `hp-omen-extra` DKMS package.

```bash
packaging/build-deb.sh
sudo apt install ./dist/omen-macro_1.0.0_all.deb
```

The script needs `dpkg-deb` (already installed on Ubuntu) and a git checkout
for the version number. Output goes to `dist/` by default; pass another
directory as the first argument.

## What the package does

Install:

1. creates the `omen-hw` group and `/etc/omen-macro`;
2. adds the user who ran `apt` to `omen-hw` (takes effect after the next login);
3. patches `hp-wmi.c` in `/usr/src/hp-omen-extra-*`, rebuilds and installs the
   DKMS module, and loads it;
4. installs and enables the boot service and the udev rule;
5. sends the default numpad profile to the keyboard.

If step 3 cannot run (no `hp-omen-extra` source, no DKMS, Secure Boot key not
enrolled), the package still installs and prints what to do. Run it again with:

```bash
sudo /usr/lib/omen-macro/kernel-setup.sh
```

Remove:

```bash
sudo apt remove omen-macro
```

The pre-remove step clears the macro table and restores the original
`hp-wmi.c` from the backup made at install time, then rebuilds the DKMS module.
`sudo apt purge omen-macro` also removes `/etc/omen-macro` (your profiles).
