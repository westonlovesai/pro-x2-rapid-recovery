#!/usr/bin/env python3
"""
Recovery tool for Logitech keyboards stuck in bootloader mode after a failed
firmware update. Written for the PRO X2 RAPID, see README.md.

It reflashes firmware using the official .dfu files that G HUB has already
downloaded to its local cache. No firmware is included in this repository.

Usage:
    python recover.py diagnose            read-only, safe to run any time
    python recover.py flash               reflash the parts that are missing/corrupt
    python recover.py flash --all         reflash every part listed in the G HUB package
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("The 'hidapi' package is missing. Install it with:  python -m pip install hidapi")

LOGITECH_VID = 0x046D
SWID = 0xA
REPORT_LONG = 0x11
DEV_IDX = 0xFF  # wired device

FEAT_ROOT = 0x0000
FEAT_FEATURE_SET = 0x0001
FEAT_FW_INFO = 0x0003
FEAT_NAME = 0x0005
FEAT_DFU = 0x00D0

ENTITY_TYPES = {0: "main application", 1: "bootloader", 2: "hardware", 3: "touchpad",
                4: "optical sensor", 5: "softdevice", 6: "RF companion", 7: "factory app",
                8: "RGB effect", 9: "motor drive", 12: "co-processor", 13: "co-processor"}

# DFU status codes (feature 0x00D0), same meaning as in fwupd's logitech-hidpp plugin
DFU_OK = {0x01: "packet accepted", 0x02: "DFU success", 0x05: "DFU success, entity restart required",
          0x06: "DFU success, system restart required"}
DFU_BUSY = {0x03, 0x17, 0x20}
DFU_ERRORS = {0x04: "generic error", 0x10: "generic error", 0x11: "bad voltage", 0x13: "bad magic string",
              0x14: "DFU not started", 0x15: "bad sequence number", 0x16: "unsupported command",
              0x18: "address out of range", 0x19: "unaligned address", 0x1A: "bad size",
              0x1B: "missing program data", 0x1C: "missing check data", 0x1D: "program failed to write",
              0x1E: "program failed to verify", 0x1F: "firmware check failure", 0x21: "bad firmware",
              0x22: "erase failure"}

HIDPP_BUSY = 0x08


class HidppError(Exception):
    pass


# ---------------------------------------------------------------- HID++ transport

class Device:
    def __init__(self, info):
        self.info = info
        self.pid = info["product_id"]
        self.h = hid.device()
        self.h.open_path(info["path"])
        self._drain()
        self.features = {}

    def close(self):
        try:
            self.h.close()
        except Exception:
            pass

    def _drain(self):
        while self.h.read(64, 50):
            pass

    def call(self, feat_idx, fn, params=b"", timeout=2.0):
        fn_byte = (fn << 4) | SWID
        pkt = bytes([REPORT_LONG, DEV_IDX, feat_idx, fn_byte]) + bytes(params)
        self.h.write(pkt.ljust(20, b"\x00"))
        end = time.time() + timeout
        while time.time() < end:
            r = self.h.read(64, 100)
            if not r:
                continue
            r = bytes(r)
            if r[0] != REPORT_LONG or r[1] != DEV_IDX:
                continue
            if r[2] == 0xFF and r[3] == feat_idx and r[4] == fn_byte:
                raise HidppError(r[5])
            if r[2] == feat_idx and r[3] == fn_byte:
                return r[4:]
        raise TimeoutError(f"no reply to feature {feat_idx} function {fn}")

    def read_notification(self, feat_idx, timeout):
        end = time.time() + timeout
        while time.time() < end:
            r = self.h.read(64, 200)
            if r and r[0] == REPORT_LONG and r[1] == DEV_IDX and r[2] == feat_idx:
                return bytes(r[4:])
        return None

    def feature(self, fid):
        if fid not in self.features:
            d = self.call(0, 0, fid.to_bytes(2, "big"))
            self.features[fid] = d[0] or None
        return self.features[fid]

    def protocol_version(self):
        d = self.call(0, 1, bytes([0, 0, 0x5A]))
        return d[0], d[1]

    def feature_list(self):
        fs = self.feature(FEAT_FEATURE_SET)
        if fs is None:
            return []
        count = self.call(fs, 0)[0]
        out = []
        for i in range(1, count + 1):
            d = self.call(fs, 1, bytes([i]))
            out.append(int.from_bytes(d[0:2], "big"))
        return out

    def name(self):
        idx = self.feature(FEAT_NAME)
        if idx is None:
            return None
        length = self.call(idx, 0)[0]
        buf = b""
        while len(buf) < length:
            d = self.call(idx, 1, bytes([len(buf)]))
            buf += d[: length - len(buf)]
        return buf.decode("ascii", "replace")

    def entities(self):
        idx = self.feature(FEAT_FW_INFO)
        if idx is None:
            return []
        count = self.call(idx, 0)[0]
        return [Entity(i, self.call(idx, 1, bytes([i]))) for i in range(count)]


class Entity:
    def __init__(self, index, raw):
        self.index = index
        self.raw = raw[:16]
        self.type = raw[0]
        self.prefix = raw[1:4]
        self.major, self.minor = raw[4], raw[5]
        self.build = int.from_bytes(raw[6:8], "big")
        self.active = bool(raw[8] & 1)
        self.pid = int.from_bytes(raw[9:11], "big")

    @property
    def readable_prefix(self):
        p = self.prefix.rstrip(b"\x00 ")
        return all(0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A for c in p) and len(p) > 0

    @property
    def target_name(self):
        """Name in the same form G HUB's dfu.json uses, e.g. U179, CC16, CC17."""
        return self.prefix.rstrip(b"\x00 ").decode("ascii", "replace") + f"{self.major:02X}"

    @property
    def version(self):
        return f"{self.major:02X}.{self.minor:02X} build {self.build:04X}"

    def describe(self):
        kind = ENTITY_TYPES.get(self.type, f"type {self.type}")
        pfx = self.prefix.decode("ascii", "replace").replace("\x00", " ")
        state = "running" if self.active else "-"
        return f"[{self.index}] {kind:18} {pfx:4} {self.version:18} {state}"


# ---------------------------------------------------------------- device discovery

def logitech_hidpp_interfaces():
    """Return the long-report HID++ interface of each connected Logitech device, keyed by serial/PID."""
    found = {}
    for d in hid.enumerate(LOGITECH_VID):
        if d["usage_page"] == 0xFF00 and d["usage"] == 0x0002:
            found[(d["product_id"], d["serial_number"])] = d
    return list(found.values())


def is_bootloader(info):
    return "bootloader" in (info.get("product_string") or "").lower()


# ---------------------------------------------------------------- G HUB cache

def default_cache_dirs():
    dirs = []
    pd = os.environ.get("ProgramData")
    if pd:
        dirs.append(os.path.join(pd, "LGHUB", "depots"))
    dirs.append("/Users/Shared/LGHUB/depots")  # macOS, untested
    return [d for d in dirs if os.path.isdir(d)]


class Package:
    """One G HUB firmware package (a depot folder with dfu.json + manifest.json)."""

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "dfu.json"), encoding="utf-8") as f:
            self.dfu = json.load(f)
        files = {}
        mf = os.path.join(folder, "manifest.json")
        if os.path.exists(mf):
            with open(mf, encoding="utf-8") as f:
                for res in json.load(f).get("resources", []):
                    files[res["key"]] = res["src"]
        self.images = []
        for c in self.dfu.get("contents", []):
            key = c["binaryFileKey"]["key"]
            ids = {i["interfaceId"].lower() for i in c.get("interfaceInfos", [])}
            targets = set()
            for i in c.get("interfaceInfos", []):
                targets.update(i.get("targetEntity", []))
            self.images.append({
                "version": c.get("version"),
                "file": os.path.join(folder, files.get(key, key)),
                "sha256": c["binaryFileKey"].get("hash", "").lower(),
                "targets": sorted(targets),
                "interfaces": ids,
            })

    @property
    def interface_ids(self):
        out = set()
        for img in self.images:
            out |= img["interfaces"]
        return out

    def matches_pid(self, pid):
        return f"{LOGITECH_VID:04x}_{pid:04x}" in self.interface_ids

    def normal_pids(self):
        """PIDs this package lists that are not marked 'force' (i.e. the normal, non-bootloader mode)."""
        out = set()
        for c in self.dfu.get("contents", []):
            for i in c.get("interfaceInfos", []):
                if not i.get("force"):
                    out.add(int(i["interfaceId"].split("_")[1], 16))
        return out


def find_packages(pid, cache_dirs):
    pkgs = []
    for base in cache_dirs:
        for path in glob.glob(os.path.join(base, "**", "dfu.json"), recursive=True):
            try:
                p = Package(os.path.dirname(path))
            except (OSError, ValueError, KeyError):
                continue
            if p.matches_pid(pid):
                pkgs.append(p)
    pkgs.sort(key=lambda p: os.path.getmtime(os.path.join(p.folder, "dfu.json")), reverse=True)
    return pkgs


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def check_image(img):
    """Return (ok, message, data). Never raises for a bad file."""
    if not os.path.exists(img["file"]):
        return False, "file missing from cache", None
    with open(img["file"], "rb") as f:
        data = f.read()
    if not img["sha256"]:
        return False, "no checksum in dfu.json", None
    if hashlib.sha256(data).hexdigest() != img["sha256"]:
        return False, "checksum does not match dfu.json", None
    if len(data) < 32 or len(data) % 16:
        return False, "unexpected file size", None
    return True, "checksum OK", data


def version_matches(entity, img):
    parts = (img["version"] or "").split(".")
    if len(parts) != 3:
        return False
    major, minor, build = parts
    return (major[-2:].upper() == f"{entity.major:02X}"
            and minor.upper() == f"{entity.minor:X}"
            and build.upper() == f"{entity.build:X}")


# ---------------------------------------------------------------- analysis

def analyse(entities, pkg):
    """Work out, for each image in the package, which entity it targets and what state that entity is in."""
    known_targets = set()
    for img in pkg.images:
        known_targets.update(img["targets"])

    results = []
    for img in pkg.images:
        ok, msg, data = check_image(img)
        r = {"img": img, "file_ok": ok, "file_msg": msg, "data": data,
             "entity": None, "state": None, "flashable": False, "reason": ""}
        results.append(r)
        if not ok:
            r["state"] = "cannot check"
            r["reason"] = msg
            continue
        idx = data[0]
        ent = next((e for e in entities if e.index == idx), None)
        r["entity"] = ent
        if ent is None:
            r["state"] = "target slot missing"
            r["reason"] = f"file targets entity {idx}, device does not report it"
            continue
        if ent.type == 1:
            r["state"] = "refused"
            r["reason"] = "file would overwrite the bootloader"
            continue
        name = ent.target_name
        if name in img["targets"]:
            r["state"] = "ok" if version_matches(ent, img) else "different version"
            r["flashable"] = True
        elif ent.readable_prefix and name in known_targets:
            r["state"] = "refused"
            r["reason"] = f"entity {idx} holds {name}, which belongs to a different file"
        else:
            r["state"] = "CORRUPT"
            r["flashable"] = True
    return results


# ---------------------------------------------------------------- flashing

def flash_image(dev, data, progress=True):
    dfu = dev.feature(FEAT_DFU)
    if dfu is None:
        raise RuntimeError("device does not support DFU feature 0x00D0")
    total = len(data) // 16
    cmd = 4  # first packet: dfuStart, then dfuCmdData1..3,0,1.. (same as fwupd)
    last = None
    for i in range(total):
        chunk = data[i * 16:(i + 1) * 16]
        d = dev.call(dfu, cmd, chunk, timeout=5.0)
        st = d[4] & 0x7F
        if st in DFU_BUSY:
            st = wait_dfu(dev, dfu)
        elif st not in DFU_OK:
            raise RuntimeError(f"device rejected packet {i + 1}/{total}: "
                               f"0x{st:02X} {DFU_ERRORS.get(st, 'unknown error')}")
        last = st
        cmd = (cmd + 1) % 4
        if progress and (i % 256 == 0 or i == total - 1):
            pct = (i + 1) * 100 // total
            print(f"\r    {pct:3d}%  ({i + 1}/{total} packets)", end="", flush=True)
    if progress:
        print()
    return last


def wait_dfu(dev, dfu):
    for _ in range(10):
        d = dev.read_notification(dfu, 15)
        if d is None:
            continue
        st = d[4] & 0x7F
        if st in DFU_OK:
            return st
        if st in DFU_BUSY:
            continue
        raise RuntimeError(f"device reported 0x{st:02X} {DFU_ERRORS.get(st, 'unknown error')}")
    raise RuntimeError("timed out waiting for the device")


def restart_into_app(dev, entities):
    dfu = dev.feature(FEAT_DFU)
    app = next((e for e in entities if e.type == 0), None)
    if dfu is None or app is None:
        return False
    for _ in range(5):
        try:
            dev.call(dfu, 5, bytes([app.index]), timeout=1.5)
        except HidppError as e:
            if e.args[0] == HIDPP_BUSY:
                time.sleep(2)
                continue
            return False
        except (TimeoutError, OSError):
            return True  # no reply usually means it rebooted
        return True
    return False


def wait_for_pids(pids, timeout):
    end = time.time() + timeout
    while time.time() < end:
        for d in hid.enumerate(LOGITECH_VID):
            if d["product_id"] in pids:
                return d["product_id"]
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------- commands

def ghub_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
    except OSError:
        return False
    return "lghub" in out


def pick_device(args):
    ifaces = logitech_hidpp_interfaces()
    if args.pid:
        ifaces = [i for i in ifaces if i["product_id"] == int(args.pid, 16)]
    if not ifaces:
        sys.exit("No Logitech device with an HID++ interface was found. Is the keyboard plugged in with a cable?")
    boot = [i for i in ifaces if is_bootloader(i)]
    if len(boot) > 1:
        sys.exit("More than one device is in bootloader mode. Unplug the others or pass --pid.")
    if boot:
        return boot[0]
    if len(ifaces) > 1 and not args.pid:
        print("No device is in bootloader mode. Logitech devices found:")
        for i in ifaces:
            print(f"    {i['product_id']:04X}  {i['product_string']}")
        sys.exit("Pass --pid XXXX to inspect one of them.")
    return ifaces[0]


def print_report(dev, info, entities, pkg, results):
    print(f"Device:     {info['product_string']}  (USB ID {LOGITECH_VID:04X}:{dev.pid:04X})")
    try:
        major, minor = dev.protocol_version()
        print(f"Protocol:   HID++ {major}.{minor}")
    except Exception:
        print("Protocol:   no HID++ 2.0 reply")
    mode = "BOOTLOADER (recovery mode)" if is_bootloader(info) else "normal"
    print(f"Mode:       {mode}")
    print(f"DFU 0x00D0: {'yes' if dev.feature(FEAT_DFU) else 'no'}")
    print()
    print("Firmware parts reported by the device:")
    for e in entities:
        print("    " + e.describe())
    print()
    if pkg is None:
        print("G HUB cache: no firmware package for this device was found.")
        return
    print(f"G HUB package: {pkg.folder}")
    for r in results:
        img = r["img"]
        fname = os.path.basename(img["file"])
        tgt = "/".join(img["targets"])
        ent = r["entity"]
        where = f"entity {ent.index}" if ent else "-"
        print(f"    {tgt:6} v{img['version']:10} {fname}")
        print(f"           file: {r['file_msg']}; target: {where}; state: {r['state']}"
              + (f" ({r['reason']})" if r["reason"] else ""))


def cmd_diagnose(args):
    info = pick_device(args)
    dev = Device(info)
    try:
        entities = dev.entities()
        pkgs = find_packages(dev.pid, args.cache)
        pkg = pkgs[0] if pkgs else None
        results = analyse(entities, pkg) if pkg else []
        print_report(dev, info, entities, pkg, results)
    finally:
        dev.close()

    print()
    if not is_bootloader(info):
        print("The device is running its normal firmware. Nothing to recover.")
        return
    broken = [r for r in results if r["state"] == "CORRUPT"]
    if broken:
        names = ", ".join("/".join(r["img"]["targets"]) for r in broken)
        print(f"Found corrupt or missing firmware: {names}")
        print("Run  python recover.py flash  to reinstall it from the G HUB cache.")
    elif results:
        print("All firmware parts look intact but the device is still in bootloader mode.")
        print("Try unplugging and replugging it. If it stays here, `flash --all` reinstalls every part.")


def confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_flash(args):
    if ghub_running():
        sys.exit("G HUB is running. Quit it from the tray icon (right click > Quit) and try again.\n"
                 "It keeps resetting the keyboard while it is open.")
    info = pick_device(args)
    if not is_bootloader(info):
        sys.exit("The device is not in bootloader mode. This tool only flashes devices that are stuck there.")

    dev = Device(info)
    try:
        if dev.feature(FEAT_DFU) is None:
            sys.exit("This device does not support DFU feature 0x00D0, so this tool cannot flash it.")
        entities = dev.entities()
        pkgs = find_packages(dev.pid, args.cache)
        if not pkgs:
            sys.exit("No firmware package for this device was found in the G HUB cache.\n"
                     "Open G HUB once with the keyboard plugged in so it downloads the firmware, then quit it.")
        pkg = pkgs[0]
        results = analyse(entities, pkg)
        print_report(dev, info, entities, pkg, results)
        print()

        bad_files = [r for r in results if not r["file_ok"]]
        if bad_files:
            sys.exit("Some files in the G HUB package failed their checks. Not flashing anything.")
        refused = [r for r in results if r["state"] in ("refused", "target slot missing")]
        if refused:
            sys.exit("The package does not line up with what the device reports. Not flashing anything.")

        if args.all:
            todo = [r for r in results if r["flashable"]]
        else:
            todo = [r for r in results if r["state"] == "CORRUPT"]
        if not todo:
            print("Nothing looks corrupt. Unplug and replug the keyboard first.")
            print("If it is still stuck after that, run  python recover.py flash --all")
            return

        print("About to write:")
        for r in todo:
            print(f"    {os.path.basename(r['img']['file'])}  ->  entity {r['entity'].index}")
        print()
        print("Do not unplug the keyboard while this runs.")
        if not args.yes and not confirm("Type 'yes' to continue: "):
            print("Cancelled. Nothing was written.")
            return

        for r in todo:
            print(f"  Writing {os.path.basename(r['img']['file'])} ...")
            st = flash_image(dev, r["data"])
            print(f"    done ({DFU_OK.get(st, hex(st))})")

        entities = dev.entities()
        print()
        print("Firmware parts after flashing:")
        for e in entities:
            print("    " + e.describe())

        print()
        print("Asking the keyboard to start its normal firmware ...")
        restart_into_app(dev, entities)
    finally:
        dev.close()

    normal = pkg.normal_pids()
    pid = wait_for_pids(normal, 10)
    if pid:
        print(f"Success. The keyboard is back in normal mode (USB ID {LOGITECH_VID:04X}:{pid:04X}).")
        return
    print("The keyboard did not restart by itself, which is normal for this bootloader.")
    print("Unplug it, wait about 10 seconds, plug it back in, and keep G HUB closed.")
    print("Waiting up to 2 minutes for it to come back ...")
    pid = wait_for_pids(normal, 120)
    if pid:
        print(f"Success. The keyboard is back in normal mode (USB ID {LOGITECH_VID:04X}:{pid:04X}).")
    else:
        print("It has not come back in normal mode. Run  python recover.py diagnose  and check the output.")


def main():
    ap = argparse.ArgumentParser(description="Recover a Logitech keyboard stuck in bootloader mode "
                                             "using firmware from the G HUB cache.")
    ap.add_argument("--pid", help="USB product ID to use (hex), if more than one device is connected")
    ap.add_argument("--cache", action="append", help="G HUB depots folder (default: auto-detect)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("diagnose", help="read-only check of the device and the G HUB cache")
    fp = sub.add_parser("flash", help="reinstall firmware from the G HUB cache")
    fp.add_argument("--all", action="store_true", help="reinstall every part, not only the corrupt ones")
    fp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args()
    args.cache = args.cache or default_cache_dirs()
    if not args.cache:
        sys.exit("Could not find the G HUB cache. Pass its depots folder with --cache.")

    try:
        if args.command == "diagnose":
            cmd_diagnose(args)
        else:
            cmd_flash(args)
    except OSError as e:
        sys.exit(f"Lost contact with the device: {e}")


if __name__ == "__main__":
    main()
