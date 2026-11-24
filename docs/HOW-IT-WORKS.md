# How it works

This page explains the moving parts. You do not need it to use the app.

## Why the keys do nothing out of the box

The six macro keys are **not** wired to the keyboard controller. The embedded
controller (EC) keeps a 4096-byte table of PS/2 scan-code sequences. When you
press a P-key, the EC replays whatever sequence is stored in that key's slot.

Until that table is written, a P-key press produces **no input event at all**.
That is why ordinary remappers (`keyd`, `xbindkeys`, OMEN Space …) cannot bind
these keys: they never see anything to bind.

HP's Windows *OMEN Gaming Hub* fills the table with two ACPI/WMI queries and
then switches the macro engine on. Lan Tian reverse engineered this in 2022
([write-up](https://lantian.pub/en/article/modify-computer/reverse-engineered-linux-driver-for-hp-omen-macro-keys.lantian/),
[code](https://github.com/xddxdd/hp-omen-linux-module)). The technique was
carried forward to kernel 7.0 by
[FamleeX/hp-omen-cm2047nr](https://github.com/FamleeX/hp-omen-cm2047nr).

| Approach | Works on this laptop (board `8BAD`)? |
|---|---|
| Mainline `hp-wmi` | no macro support |
| `pelrun/hp-omen-linux-module` | RGB/backlight only ([issue #18](https://github.com/pelrun/hp-omen-linux-module/issues/18)) |
| OMEN Space / `hp-omen-extra` | RGB, fan and thermal only |
| `sa4zet/hp.omen.kernel.patch` | macro code, but old kernels (6.8 to 6.10) |
| **this project** | ports the published technique to the DKMS driver already installed, adds a writable interface and a GUI |

## The kernel side

`kernel/hp-wmi-macro.patch` adds two files to the `hp-wmi` driver from the
`hp-omen-extra` DKMS package:

| sysfs file | Mode | Purpose |
|---|---|---|
| `/sys/devices/platform/hp-wmi/macro_profile` | write-only, 4096 B | write the whole macro table; the write uploads it (WMI query 15) and switches the engine on (query 23) |
| `/sys/devices/platform/hp-wmi/macro_status` | read-only | return codes of the last attempt: `profile_write_ret`, `mode_set_ret`, `applied` |

The driver does not encode anything; it just moves the buffer to the EC. All
policy (what the keys do) lives in userspace, where it can change without
rebuilding a module.

## The userspace side

| File | What it does |
|---|---|
| `omen_macro/codec.py` | scan-code table, chord/delay encoder, profile builder, decoder for the UI summaries. Pure standard library. |
| `omen_macro/apply.py` | command line: send a profile, list profiles, dry-run, read status. |
| `omen_macro/gui.py` | the GTK4 app: recording, per-key macros and commands, profiles, auto-switch, EC status. Styled after OMEN Gaming Hub; the colours live in the `THEME` dict at the top. |
| `omen_macro/profiles.py` | profile store shared by the app, the CLI and the boot service (`profiles/<name>.json` + `active-profile`). |
| `omen_macro/auto_switch.py` | optional per-user daemon that switches profiles based on the focused app. |
| `omen_macro/ec.py` | the 4096-byte upload helper used by both upload paths. |
| `omen_macro/boot_diag.py` | explains why the interface is missing (module not loaded, Secure Boot rejection, permissions …). |
| `desktop/omen-macro.svg` | the app icon. |

## Table format (for anyone extending this)

The table has 30 slots, in EC order: `P1…P6`, `Ctrl+P1…P6`, `Alt+P1…P6`,
`Shift+P1…P6`, `Fn+P1…P6`, followed by one extra `0x01` byte.

Each slot is `[len][bytes…]`:

* `len` counts itself, so N payload bytes use N+1;
* an unused slot is the single byte `0x01`;
* press codes are PS/2 scan code set 1; release codes are `make | 0x80`;
* `0xFF, ms` is a delay;
* extended keys start with `0xE0`.

The stock layout maps `P1…P6` to numpad `1…6`.

## Limits

* Only keys with a PS/2 scan code set 1 code can be replayed. `F13` to `F24`,
  media/volume/brightness keys and most `XF86…` keys are impossible.
* One macro can hold at most 254 payload bytes, about 60 keystrokes (fewer
  with delays).
* Spacing is 0 to 2040 ms. The EC's delay marker is one byte (0 to 255 ms), so
  longer delays are sent as several markers.
* The EC forgets the table on a cold boot; that is why a boot service sends it
  again. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the boot timing.
* Modifier variants (`Ctrl+P1`, …) are separate slots. If a variant is empty,
  pressing it runs the base macro (e.g. `Ctrl+P1` runs `P1`'s macro with Ctrl
  held).

## Layout of the repository

```
omen-macro/
├── install.sh              # first-time install (kernel + app)
├── install-userspace.sh    # update only the app
├── fix-secureboot.sh       # enrol the module signing key
├── fix-boot.sh             # repair the boot service/udev timing
├── revert.sh               # uninstall
├── verify.sh               # check the whole chain
├── kernel/                 # the hp-wmi patch
├── omen_macro/             # the app, CLI and daemons
├── desktop/                # .desktop entry and icon
├── systemd/, udev/         # boot service and udev rule
├── tests/                  # test suite and visual QA harness
└── docs/                   # these documents
```
