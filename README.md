# PRO X2 RAPID recovery tool

A small command line tool for getting a Logitech PRO X2 RAPID keyboard out of bootloader mode after a firmware update has gone wrong.

It reinstalls the firmware using the official files that G HUB has already downloaded to your PC. No firmware is included in this repository, and the tool does not download anything.

## Why this exists

My PRO X2 RAPID was brand new and stopped working. No lighting, the LED screen wouldn't turn on, and Windows only saw a device called "Bootloader for PRO X2 RAPID". The keyboard has a factory reset option, but it's in the menu on the LED screen, so that was useless. G HUB showed "initialization failed", the keyboard disappeared from G HUB, came back, failed again, and kept doing that every couple of seconds. Logitech support didn't have a fix.

When I read the firmware information out of the bootloader, it turned out that one of the three firmware parts on the keyboard (the one G HUB calls CC17) was corrupted. Its version info was garbage. Because of that, the bootloader wouldn't start the main firmware, and G HUB couldn't find the part it was looking for, so it gave up and reset the keyboard in a loop.

Writing the official CC17 image back to that slot fixed it. The keyboard booted normally after a replug, and the lighting and screen came back. This tool is that fix, cleaned up so other people can use it.

## Will it work for my keyboard?

**PRO X2 RAPID:** This is the only keyboard it has been tested on, and only on one unit with one specific fault (a corrupted CC17 part). If your PRO X2 RAPID got stuck the same way, it's very likely to help.

**Other Logitech keyboards:** It might work. Nothing in the tool is hardcoded to the PRO X2 RAPID. It works out which files belong to which firmware part from G HUB's own package files, and many newer Logitech G devices use the same update protocol. But I have not tested it on anything else. If you try it on another model, please open an issue with the output of `diagnose` (remove your serial number first), whether it worked or not.

It will **not** help if:

- The keyboard doesn't show up on USB at all. If Windows doesn't see a "Bootloader for ..." device, there is nothing for software to talk to.
- The device uses a different update method. The tool checks for this and refuses to flash if the device doesn't support it.
- You only have it connected wirelessly. Use the cable.

## Disclaimer

This is an unofficial tool. It is not made, endorsed or supported by Logitech.

You use it entirely at your own risk. It writes firmware to your keyboard, and while it has a lot of checks built in, I can't guarantee it won't make things worse on your device. I take no responsibility if something breaks. If your keyboard is new and still under warranty, consider using the warranty first.

## Requirements

- Windows 10 or 11. (The code might run on macOS if G HUB's cache is in `/Users/Shared/LGHUB/depots`, but that is untested.)
- Python 3.8 or newer
- The `hidapi` Python package
- G HUB installed, and its cache containing the firmware for your keyboard. If G HUB has tried to update or recover the keyboard, the files are almost certainly there already.
- A USB cable connection to the keyboard

## How to use it

### 1. Install

Download or clone this repository, open a terminal in the folder, and install the one dependency:

```
python -m pip install hidapi
```

### 2. Quit G HUB completely

This step matters. While G HUB is running it keeps trying to initialize the keyboard, and every failed attempt resets it. That's what causes the connect/disconnect loop.

Closing the G HUB window isn't enough. Right click the G HUB icon in the system tray (bottom right, near the clock) and choose **Quit**. The `flash` command checks for this and refuses to run if G HUB is still open.

### 3. Plug in the keyboard with the cable

Use a USB port directly on the PC if you can, not a hub.

### 4. Run the diagnosis

```
python recover.py diagnose
```

This is read-only. It doesn't change anything on the keyboard, so you can run it as many times as you like. It shows:

- whether the keyboard is in bootloader mode or running normally
- every firmware part the keyboard reports, with its version
- the matching firmware package in the G HUB cache, whether each file's checksum matches, and which part on the keyboard it belongs to
- which parts look corrupt

Here's roughly what my keyboard looked like when it was broken:

```
Device:     Bootloader for PRO X2 RAPID  (USB ID 046D:ABE4)
Protocol:   HID++ 4.2
Mode:       BOOTLOADER (recovery mode)
DFU 0x00D0: yes

Firmware parts reported by the device:
    [0] bootloader         BL2  61.01 build 0011   running
    [1] main application   U1   79.02 build 0015   -
    [2] co-processor       CC   16.00 build 0017   -
    [3] RGB effect         EXT  00.00 build 0000   -
    [4] RGB effect         EXT  00.00 build 0000   -
    [5] co-processor       .xb  66.00 build 6A73   -

G HUB package: C:\ProgramData\LGHUB\depots\876190\<id>
    U179   v179.2.15   pro_x2_rapid_m1_v179_02_15.dfu
           file: checksum OK; target: entity 1; state: ok
    CC16   v16.0.17    pro_x2_rapid_m2_v16_00_17.dfu
           file: checksum OK; target: entity 2; state: ok
    CC17   v17.2.18    pro_x2_rapid_m3_v17_02_18.dfu
           file: checksum OK; target: entity 5; state: CORRUPT

Found corrupt or missing firmware: CC17
Run  python recover.py flash  to reinstall it from the G HUB cache.
```

Entity 5 should say `CC 17.xx`, but it says `.xb 66.00 build 6A73`, which is nonsense. That's the broken part.

If more than one Logitech device is connected and none of them is in bootloader mode, the tool lists them and asks you to pick one with `--pid`, for example `python recover.py --pid C364 diagnose`.

### 5. Flash

```
python recover.py flash
```

It runs the same checks as `diagnose`, shows you exactly which file it is going to write to which part of the keyboard, and asks you to type `yes` before writing anything. Don't unplug the keyboard while it's writing. A single part takes a couple of minutes.

Once it's done, it asks the keyboard to start its normal firmware. On my keyboard the bootloader ignored that request and the keyboard needed a replug. If that happens, the tool tells you to unplug the keyboard, wait about 10 seconds and plug it back in, then waits for it to come back in normal mode.

After that you can open G HUB again.

### If nothing looks corrupt but it's still stuck

Unplug and replug the keyboard first. If it still boots into the bootloader, you can reinstall every part in the package, not just the ones that look broken:

```
python recover.py flash --all
```

This can help when a part's version info looks fine but its contents are damaged, for example after an update was interrupted halfway.

### Options

| Option | What it does |
| --- | --- |
| `--pid XXXX` | Pick a specific device by USB product ID (hex). Goes before the command. |
| `--cache PATH` | Use a different G HUB `depots` folder instead of auto-detecting it. Goes before the command. |
| `flash --all` | Reinstall every part in the package, not only the corrupt ones. |
| `flash --yes` | Skip the confirmation prompt. |

## Safety checks

The tool refuses to write anything unless all of these hold:

- G HUB is not running.
- The device is in bootloader mode. It won't flash a keyboard that is running normally.
- The device supports the HID++ DFU feature (0x00D0).
- A firmware package for this exact device is in the G HUB cache.
- Every file in the package matches the SHA-256 checksum that G HUB's own `dfu.json` lists for it.
- Every file targets a firmware part the keyboard actually reports.
- No file would overwrite the bootloader.
- No file would overwrite a part that holds a different, valid component.
- You typed `yes` (unless you passed `--yes`).

If any check fails, nothing is written.

## How it works

Some background, for anyone curious or wanting to adapt it.

**HID++.** Logitech devices speak a vendor protocol called HID++ over regular USB HID reports. Everything G HUB does, from lighting to firmware updates, goes through it. A device exposes a list of "features", each with a numeric ID, and you call functions on them. The tool only uses a handful:

- `0x0000` Root: look up features and get the protocol version
- `0x0001` FeatureSet: list features
- `0x0003` DeviceFwInfo: list firmware parts ("entities") and their versions
- `0x0005` DeviceName
- `0x00D0` DFU: write firmware and restart

**Bootloader mode.** When the keyboard can't or won't start its main firmware, it stays in a small bootloader that shows up under a different USB product ID (`046D:ABE4` instead of the normal `046D:C364` for the PRO X2 RAPID). The bootloader only supports the four features above, plus DFU.

**Firmware parts.** The PRO X2 RAPID reports six entities: the bootloader, the main application (`U1 79.xx`), two co-processors (`CC 16.xx` and `CC 17.xx`) and two RGB effect slots. G HUB ships firmware for the main application and the two co-processors as three separate `.dfu` files.

**The G HUB cache.** G HUB keeps downloaded firmware in `C:\ProgramData\LGHUB\depots\<number>\<id>\`. Each package has a `dfu.json` listing which USB IDs it applies to (including the bootloader's, marked `"force": true`), which entity name each file targets (`U179`, `CC16`, `CC17`), the version and a SHA-256 hash. The tool finds the package by the device's USB ID and checks every hash. The first byte of each `.dfu` file is the entity index it should be written to. The tool checks that the entity at that index either carries the expected name or is unreadable, which is how it spots corruption.

**Writing.** The file is sent as-is in 16-byte chunks. The first chunk goes to DFU function 4 (dfuStart), and the following chunks rotate through functions 1, 2, 3, 0, 1 and so on. The device acknowledges every chunk with a status byte, and the tool stops at the first error. Afterwards it calls function 5 (restart) with the main application's entity index. This is the same scheme the open source [fwupd](https://github.com/fwupd/fwupd) project uses in its `logitech-hidpp` plugin, which I used as a reference.

## Reporting results

If you try this, especially on a keyboard other than the PRO X2 RAPID, please open an issue with:

- the keyboard model
- the full output of `python recover.py diagnose`, with the serial number removed
- what you ran and what happened

Failed attempts are just as useful as successful ones.

## License

MIT. See [LICENSE](LICENSE).

Logitech, G HUB and PRO X2 RAPID are trademarks of Logitech. This project is not affiliated with Logitech.
