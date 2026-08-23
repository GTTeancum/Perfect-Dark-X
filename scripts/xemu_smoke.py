#!/usr/bin/env python3
import argparse
import ctypes
import hashlib
import os
import socket
import subprocess
import threading
import time
import re

from PIL import Image, ImageDraw


XEMU_DIR = r"C:\Games\Emulators\Xemu"
XEMU_BASE = os.path.join(XEMU_DIR, "xemu.exe")
XEMU_JA = os.path.join(XEMU_DIR, "xemu_ja.exe")
OUT_DIR = os.path.join("scripts", "output")
XEMU_TOML = os.path.join(XEMU_DIR, "xemu.toml")
MAP_PATH_OVERRIDE = ""
RUNTIME_BUILD_ID_MARKER = b"STEFX_RUNTIME_BUILD_ID "


def cap_windows_process_affinity(pid, core_count=6):
    if os.name != "nt":
        return None

    available = max(1, os.cpu_count() or 1)
    selected = min(max(1, core_count), available)
    logical_ids = list(range(0, available, 2))[:selected]
    if len(logical_ids) < selected:
        logical_ids = list(range(selected))
    affinity_mask = sum(1 << logical_id for logical_id in logical_ids)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.SetProcessAffinityMask.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    process_set_information = 0x0200
    handle = kernel32.OpenProcess(process_set_information, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        if not kernel32.SetProcessAffinityMask(handle, affinity_mask):
            raise OSError(
                ctypes.get_last_error(), "SetProcessAffinityMask failed")
    finally:
        kernel32.CloseHandle(handle)
    return affinity_mask


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def runtime_build_id_from_xbe(path):
    with open(path, "rb") as f:
        data = f.read()
    start = data.find(RUNTIME_BUILD_ID_MARKER)
    if start < 0:
        return None
    end = start
    limit = min(len(data), start + 256)
    while end < limit and data[end] not in (0, 10, 13):
        end += 1
    return data[start:end].decode("ascii", errors="replace")


def runtime_build_id_identity_failures(path, runtime_build_id):
    if not runtime_build_id:
        return []
    basename = os.path.basename(path).lower()
    expected = []
    if basename == "default.xbe":
        expected = ["personality=default", "log=ef_sp_log.txt"]
    elif basename == "efmp.xbe":
        expected = ["personality=efmp", "log=ef_mp_log.txt"]
    return [fragment for fragment in expected if fragment not in (runtime_build_id or "")]


SMOKE_KEYBOARD_MAP = """
[input.keyboard_controller_scancode_map]
a = 4
b = 5
x = 27
y = 28
white = 30
black = 31
ltrigger = 26
rtrigger = 22
dpad_left = 22
dpad_right = 9
dpad_up = 8
dpad_down = 7
lstick_left = 22
lstick_right = 9
lstick_up = 8
lstick_down = 7
lstick_btn = 29
rstick_left = 13
rstick_right = 15
rstick_up = 12
rstick_down = 14
rstick_btn = 16
start = 40
back = 42
guide = 10
""".strip()


def parse_key_schedule(spec):
    events = []
    if not spec:
        return events
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        when, key = part.split(":", 1)
        hold = 0.18
        if "*" in key:
            key, hold_spec = key.split("*", 1)
            hold = float(hold_spec)
        events.append([float(when), key.strip(), hold, False])
    return sorted(events, key=lambda e: e[0])


def send_window_key(pid, key, hold=0.18):
    if os.name != "nt":
        return False, "host window keys require Windows"

    key_map = {
        "ret": (0x0D, 0x1C),
        "enter": (0x0D, 0x1C),
        "backspace": (0x08, 0x0E),
    }
    mapping = key_map.get(key.lower())
    if mapping is None and len(key) == 1 and key.isalpha():
        vk = ord(key.upper())
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        mapping = (vk, user32.MapVirtualKeyW(vk, 0))
    if mapping is None:
        return False, "unsupported host window key: %s" % key

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    windows = []

    @enum_proc_type
    def enum_proc(hwnd, _lparam):
        window_pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return 1

    user32.EnumWindows(enum_proc, 0)
    if not windows:
        return False, "no visible XEMU window for pid %d" % pid

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def window_area(hwnd):
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0
        return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)

    # XEMU owns small helper/tooltip windows as well as the render window.
    # Posting to the first enumerated window is therefore nondeterministic.
    hwnd = max(windows, key=window_area)
    vk, scan = mapping
    key_down = 1 | (scan << 16)
    key_up = key_down | (1 << 30) | (1 << 31)
    if not user32.PostMessageW(hwnd, 0x0100, vk, key_down):
        return False, "WM_KEYDOWN failed: %d" % ctypes.get_last_error()
    def release_key():
        user32.PostMessageW(hwnd, 0x0101, vk, key_up)

    release_timer = threading.Timer(max(0.05, hold), release_key)
    release_timer.daemon = True
    release_timer.start()
    return True, "hwnd=0x%x async_release=%.2f" % (hwnd, max(0.05, hold))


def resolve_map_symbol(symbol):
    map_paths = []
    if MAP_PATH_OVERRIDE:
        map_paths.append(MAP_PATH_OVERRIDE)
    map_paths += [
        os.path.join("build", "release", "default.map"),
        os.path.join("build", "release", "ja-release.map"),
        os.path.join("code", "x_exe", "Release", "default.map"),
        os.path.join("code", "x_exe", "Release", "ja-release.map"),
        os.path.join("codemp", "x_exe", "Release", "default.map"),
        os.path.join("codemp", "x_exe", "Release", "jamp.map"),
        os.path.join("codemp", "x_exe", "Release", "jamp-release.map"),
    ]
    # lld-link emits 64-bit zero-extended addresses even for the 32-bit Xbox
    # image, while older MSVC maps use eight digits.
    pattern = re.compile(r"\b%s\b\s+([0-9a-fA-F]{8,16})\b" % re.escape(symbol))
    for path in map_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        return int(match.group(1), 16), path
        except OSError:
            pass
    return None, None


def resolve_map_symbol_in(symbol, path):
    pattern = re.compile(r"\b%s\b\s+([0-9a-fA-F]{8,16})\b" % re.escape(symbol))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    return int(match.group(1), 16)
    except OSError:
        pass
    return None


def resolve_literal_map_symbol_in(symbol, path):
    """Resolve decorated C++ symbols whose leading '?' has no word boundary."""
    pattern = re.compile(
        r"(?:^|\s)%s\s+([0-9a-fA-F]{8,16})(?:\s|$)" % re.escape(symbol))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    return int(match.group(1), 16)
    except OSError:
        pass
    return None


def resolve_symbol_offsets(base_symbol, symbols):
    base, base_path = resolve_map_symbol(base_symbol)
    if base is None or base_path is None:
        return {}
    return resolve_symbol_offsets_in(base_symbol, symbols, base_path)


def resolve_symbol_offsets_in(base_symbol, symbols, path):
    base = resolve_map_symbol_in(base_symbol, path)
    offsets = {}
    if base is None:
        return offsets
    for name in symbols:
        addr = resolve_map_symbol_in(name, path)
        if addr is not None and addr >= base:
            offsets[name] = (addr - base) // 4
    return offsets


def signed_u32(value):
    return value - 0x100000000 if value & 0x80000000 else value


def write_contact_sheet(paths, dest):
    images = []
    for path in paths:
        try:
            images.append((path, Image.open(path).convert("RGB")))
        except Exception:
            pass
    if not images:
        return False

    thumb_w = 420
    label_h = 24
    padding = 10
    cols = 2
    thumbs = []
    for path, img in images:
        scale = float(thumb_w) / float(img.width)
        thumb_h = max(1, int(img.height * scale))
        thumb = img.resize((thumb_w, thumb_h))
        thumbs.append((path, thumb))

    rows = (len(thumbs) + cols - 1) // cols
    cell_h = max(t.height for _p, t in thumbs) + label_h + padding
    sheet = Image.new("RGB", (cols * (thumb_w + padding) + padding, rows * cell_h + padding), "white")
    draw = ImageDraw.Draw(sheet)
    for idx, (path, thumb) in enumerate(thumbs):
        row = idx // cols
        col = idx % cols
        x = padding + col * (thumb_w + padding)
        y = padding + row * cell_h
        sheet.paste(thumb, (x, y + label_h))
        draw.text((x, y), os.path.basename(path), fill=(0, 0, 0))
    sheet.save(dest)
    return True


def close_windows_security_alerts():
    return []


def ensure_xemu_copy():
    if not os.path.exists(XEMU_JA):
        import shutil
        shutil.copy2(XEMU_BASE, XEMU_JA)


_XEMU_SCREENSHOT_FLAG_RVA_CACHE = {}


def pe_sections(data):
    pe_offset = int.from_bytes(data[0x3c:0x40], "little")
    section_count = int.from_bytes(data[pe_offset + 6:pe_offset + 8], "little")
    optional_size = int.from_bytes(data[pe_offset + 20:pe_offset + 22], "little")
    optional_offset = pe_offset + 24
    magic = int.from_bytes(data[optional_offset:optional_offset + 2], "little")
    if magic == 0x20b:
        image_base = int.from_bytes(data[optional_offset + 24:optional_offset + 32], "little")
    else:
        image_base = int.from_bytes(data[optional_offset + 28:optional_offset + 32], "little")
    section_offset = optional_offset + optional_size
    sections = []
    for index in range(section_count):
        offset = section_offset + index * 40
        name = data[offset:offset + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        virtual_size = int.from_bytes(data[offset + 8:offset + 12], "little")
        virtual_address = int.from_bytes(data[offset + 12:offset + 16], "little")
        raw_size = int.from_bytes(data[offset + 16:offset + 20], "little")
        raw_offset = int.from_bytes(data[offset + 20:offset + 24], "little")
        sections.append((name, virtual_address, raw_offset, max(virtual_size, raw_size)))
    return image_base, sections


def xbe_sections(data):
    if len(data) < 0x124 or int.from_bytes(data[0:4], "little") != 0x48454258:
        return []
    base_address = int.from_bytes(data[0x104:0x108], "little")
    section_count = int.from_bytes(data[0x11c:0x120], "little")
    section_headers_va = int.from_bytes(data[0x120:0x124], "little")
    section_headers_offset = section_headers_va - base_address
    sections = []
    for index in range(section_count):
        offset = section_headers_offset + index * 0x38
        if offset < 0 or offset + 0x18 > len(data):
            break
        virtual_address = int.from_bytes(data[offset + 4:offset + 8], "little")
        virtual_size = int.from_bytes(data[offset + 8:offset + 12], "little")
        name_va = int.from_bytes(data[offset + 0x14:offset + 0x18], "little")
        name_offset = name_va - base_address
        if 0 <= name_offset < len(data):
            name = data[name_offset:name_offset + 32].split(b"\0", 1)[0].decode(
                "ascii", "replace")
        else:
            name = ""
        sections.append((name, virtual_address, virtual_size))
    return sections


def linked_pe_va_to_xbe_va(linked_va, map_path):
    image_root = os.path.splitext(map_path)[0]
    pe_path = image_root + ".exe"
    xbe_path = image_root + ".xbe"
    if not os.path.isfile(pe_path) or not os.path.isfile(xbe_path):
        return None
    with open(pe_path, "rb") as stream:
        pe_data = stream.read()
    with open(xbe_path, "rb") as stream:
        xbe_data = stream.read()
    image_base, pe_section_list = pe_sections(pe_data)
    xbe_section_list = xbe_sections(xbe_data)
    for name, virtual_address, _raw_offset, size in pe_section_list:
        section_start = image_base + virtual_address
        if section_start <= linked_va < section_start + size:
            section_offset = linked_va - section_start
            for xbe_name, xbe_address, xbe_size in xbe_section_list:
                if xbe_name == name and section_offset < xbe_size:
                    return xbe_address + section_offset
            return None
    return None


def runtime_map_va(linked_va, map_path):
    """Translate a linker-map VA to the address used by the running XBE."""
    if linked_va is None:
        return None
    runtime_va = linked_pe_va_to_xbe_va(linked_va, map_path)
    return runtime_va if runtime_va is not None else linked_va


def resolve_runtime_map_symbol(symbol):
    linked_va, map_path = resolve_map_symbol(symbol)
    return runtime_map_va(linked_va, map_path), map_path, linked_va


def resolve_runtime_map_symbol_in(symbol, map_path):
    linked_va = resolve_map_symbol_in(symbol, map_path)
    return runtime_map_va(linked_va, map_path)


def pe_file_offset_to_va(offset, image_base, sections):
    for _name, virtual_address, raw_offset, size in sections:
        if raw_offset and raw_offset <= offset < raw_offset + size:
            return image_base + virtual_address + (offset - raw_offset)
    return None


def pe_va_to_file_offset(va, image_base, sections):
    rva = va - image_base
    for _name, virtual_address, raw_offset, size in sections:
        if virtual_address <= rva < virtual_address + size:
            return raw_offset + (rva - virtual_address)
    return None


def pe_find_rip_xrefs(data, image_base, sections, target_va):
    text_section = None
    for section in sections:
        if section[0] == ".text":
            text_section = section
            break
    if text_section is None:
        return []

    _name, text_va, text_raw, text_size = text_section
    text = data[text_raw:text_raw + text_size]
    hits = []
    import struct
    for index in range(0, len(text) - 4):
        disp = struct.unpack_from("<i", text, index)[0]
        va = image_base + text_va + index + 4 + disp
        if va == target_va:
            hits.append(image_base + text_va + index)
    return hits


def xemu_find_screenshot_flag_pointer_rva(xemu_exe):
    cached = _XEMU_SCREENSHOT_FLAG_RVA_CACHE.get(xemu_exe)
    if cached is not None:
        return cached

    with open(xemu_exe, "rb") as handle:
        data = handle.read()
    image_base, sections = pe_sections(data)

    screenshot_offsets = []
    start = 0
    while True:
        offset = data.find(b"Screenshot\0", start)
        if offset < 0:
            break
        screenshot_offsets.append(offset)
        start = offset + 1

    f12_offsets = []
    start = 0
    while True:
        offset = data.find(b"F12\0", start)
        if offset < 0:
            break
        f12_offsets.append(offset)
        start = offset + 1

    screenshot_xrefs = []
    for offset in screenshot_offsets:
        va = pe_file_offset_to_va(offset, image_base, sections)
        if va is not None:
            screenshot_xrefs.extend(pe_find_rip_xrefs(data, image_base, sections, va))

    f12_xrefs = []
    for offset in f12_offsets:
        va = pe_file_offset_to_va(offset, image_base, sections)
        if va is not None:
            f12_xrefs.extend(pe_find_rip_xrefs(data, image_base, sections, va))

    anchors = []
    for screenshot_xref in screenshot_xrefs:
        for f12_xref in f12_xrefs:
            if abs(screenshot_xref - f12_xref) < 64:
                anchors.append(min(screenshot_xref, f12_xref) - 3)

    text_section = None
    for section in sections:
        if section[0] == ".text":
            text_section = section
            break
    if text_section is None:
        return None

    _name, _text_va, text_raw, text_size = text_section
    text_end = text_raw + text_size
    import struct
    for anchor in anchors:
        anchor_offset = pe_va_to_file_offset(anchor, image_base, sections)
        if anchor_offset is None:
            continue
        search_start = max(text_raw, anchor_offset - 128)
        search_end = min(text_end, anchor_offset + 0x1200)
        for offset in range(search_start, search_end - 12):
            if (data[offset:offset + 3] == b"\x48\x8b\x05" and
                    data[offset + 7:offset + 10] == b"\xc6\x00\x01" and
                    data[offset + 10] == 0xe9):
                pattern_va = pe_file_offset_to_va(offset, image_base, sections)
                disp = struct.unpack_from("<i", data, offset + 3)[0]
                pointer_va = pattern_va + 7 + disp
                pointer_rva = pointer_va - image_base
                _XEMU_SCREENSHOT_FLAG_RVA_CACHE[xemu_exe] = pointer_rva
                return pointer_rva
    return None


def xemu_process_module_base(process_handle):
    import ctypes

    psapi = ctypes.windll.psapi
    modules = (ctypes.c_void_p * 1024)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcessModules(
            process_handle, ctypes.byref(modules), ctypes.sizeof(modules),
            ctypes.byref(needed)):
        return None
    return int(modules[0]) if modules[0] else None


def xemu_trigger_native_screenshot(pid, xemu_exe, screenshot_dir, timeout=3.0):
    if os.name != "nt":
        return False, "xemu native screenshots require Windows", None

    import ctypes

    os.makedirs(screenshot_dir, exist_ok=True)
    before = {
        os.path.abspath(os.path.join(screenshot_dir, name))
        for name in os.listdir(screenshot_dir)
        if name.lower().endswith(".png")
    }

    pointer_rva = xemu_find_screenshot_flag_pointer_rva(xemu_exe)
    if pointer_rva is None:
        return False, "xemu native screenshot flag path not found", None

    kernel32 = ctypes.windll.kernel32
    access = 0x0400 | 0x0008 | 0x0010 | 0x0020
    process = kernel32.OpenProcess(access, False, pid)
    if not process:
        return False, "xemu native screenshot OpenProcess failed pid=%d" % pid, None

    flag_addr = None
    try:
        module_base = xemu_process_module_base(process)
        if module_base is None:
            return False, "xemu native screenshot module base not found", None

        pointer_addr = module_base + pointer_rva
        pointer_buf = (ctypes.c_ubyte * 8)()
        transferred = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(
                process, ctypes.c_void_p(pointer_addr), pointer_buf, 8,
                ctypes.byref(transferred)):
            return False, "xemu native screenshot flag pointer unreadable", None

        flag_addr = int.from_bytes(bytes(pointer_buf), "little")
        one = (ctypes.c_ubyte * 1)(1)
        if not kernel32.WriteProcessMemory(
                process, ctypes.c_void_p(flag_addr), one, 1,
                ctypes.byref(transferred)):
            return False, "xemu native screenshot flag write failed", None
    finally:
        kernel32.CloseHandle(process)

    end = time.time() + timeout
    while time.time() < end:
        current = []
        for name in os.listdir(screenshot_dir):
            if not name.lower().endswith(".png"):
                continue
            path = os.path.abspath(os.path.join(screenshot_dir, name))
            if path not in before and os.path.exists(path):
                current.append(path)
        if current:
            newest = max(current, key=os.path.getmtime)
            return True, (
                "xemu native screenshot flag_rva=0x%x flag=0x%x file=%s" %
                (pointer_rva, flag_addr or 0, newest)
            ), newest
        time.sleep(0.1)

    return False, "xemu native screenshot flag set but no PNG appeared", None


def monitor_connect(port, timeout=12.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            sock = socket.socket()
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", port))
            time.sleep(0.2)
            try:
                sock.recv(65536)
            except Exception:
                pass
            return sock
        except Exception as exc:
            last = exc
            time.sleep(0.25)
    raise RuntimeError("monitor not ready: %s" % last)


def monitor_cmd(sock, cmd, wait=0.2):
    sock.sendall((cmd + "\r\n").encode("ascii"))
    time.sleep(wait)
    data = b""
    sock.settimeout(1.0)
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if data.rstrip().endswith(b"(qemu)"):
                break
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")


def parse_monitor_words(text, base_addr=None):
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    words = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        addr_text, values_text = line.split(":", 1)
        try:
            addr = int(addr_text.strip(), 16)
        except ValueError:
            continue
        values = []
        for token in re.findall(r"\b(?:0x)?[0-9a-fA-F]{8}\b", values_text):
            try:
                values.append(int(token, 16))
            except ValueError:
                pass
        if base_addr is None or addr == base_addr or values:
            words.extend(values)
    return words


def probe_xblog_physical_addr(sock, boot_va, preferred_delta, log):
    candidates = []
    for delta in (
        preferred_delta,
        0x2a3000,
        0x2a4000,
        0x287000,
        0x286000,
        0x285000,
        0x284000,
        0x283000,
        0x282000,
        0x281000,
        0x280000,
        0x264000,
        0x244000,
    ):
        if delta and delta not in candidates:
            candidates.append(delta)

    for delta in candidates:
        addr = boot_va - delta
        if addr < 4:
            continue
        try:
            reply = monitor_cmd(sock, "xp/8wx 0x%08x" % (addr - 4), 0.3)
        except OSError:
            continue
        words = parse_monitor_words(reply)
        if len(words) >= 5 and words[0] == 0x53504546 and words[4] == 0x48424653:
            log("xblog_probe matched delta=0x%x addr=0x%08x" % (delta, addr))
            return addr
        log("xblog_probe miss delta=0x%x addr=0x%08x words=%s" %
            (delta, addr, ",".join("0x%08x" % w for w in words[:5])))

        scan_start = max(0, (addr - 0x1000) & ~3)
        try:
            scan_reply = monitor_cmd(sock, "xp/2048wx 0x%08x" % scan_start, 0.8)
        except OSError:
            continue
        scan_words = parse_monitor_words(scan_reply)
        for i in range(0, max(0, len(scan_words) - 5)):
            if scan_words[i] == 0x53504546 and scan_words[i + 4] == 0x48424653:
                found_addr = scan_start + i * 4 + 4
                found_delta = boot_va - found_addr
                log("xblog_probe scanned delta=0x%x addr=0x%08x" % (found_delta, found_addr))
                return found_addr

    # XLaunchNewImage can relocate the replacement title outside the cold-boot
    # title window. Scan Xbox RAM once for the paired telemetry sentinels.
    broad_start = 0x00000000
    broad_size = 0x04000000
    broad_path = os.path.abspath(
        os.path.join(OUT_DIR, "_xblog_probe_%d.bin" % os.getpid()))
    try:
        if monitor_pmemsave(sock, broad_start, broad_size, broad_path):
            with open(broad_path, "rb") as broad_file:
                broad_data = broad_file.read()
            heartbeat = b"SFBH"
            precrt = b"FEPS"
            search_at = 0
            while True:
                heartbeat_at = broad_data.find(heartbeat, search_at)
                if heartbeat_at < 0:
                    break
                if (heartbeat_at >= 16 and
                        broad_data[heartbeat_at - 16:heartbeat_at - 12] == precrt):
                    found_addr = broad_start + heartbeat_at - 12
                    found_delta = boot_va - found_addr
                    log("xblog_probe broad delta=0x%x addr=0x%08x" %
                        (found_delta, found_addr))
                    return found_addr
                search_at = heartbeat_at + 4
    except OSError as exc:
        log("xblog_probe broad unavailable=%s" % exc)
    finally:
        try:
            os.remove(broad_path)
        except OSError:
            pass
    return None


def probe_xblog_virtual_addr(sock, boot_va, log):
    """Return a live title's telemetry address without translating through RAM."""
    try:
        reply = monitor_cmd(sock, "x/9wx 0x%08x" % boot_va, 0.3)
    except OSError:
        return None
    words = parse_monitor_words(reply, boot_va)
    if len(words) >= 4 and words[3] == 0x48424653:
        log("xblog_virtual_probe matched addr=0x%08x" % boot_va)
        return boot_va
    log("xblog_virtual_probe miss addr=0x%08x words=%s" %
        (boot_va, ",".join("0x%08x" % word for word in words[:4])))
    return None


def probe_magic_runtime_addr(sock, value_va, magic, command, runtime_delta,
                             log, label):
    addr = value_va if command == "x" else value_va - runtime_delta
    if addr < 4:
        return None
    try:
        reply = monitor_cmd(sock, "%s/5wx 0x%08x" % (command, addr), 0.25)
    except OSError:
        return None
    words = parse_monitor_words(reply, addr)
    if words and words[0] == magic:
        log("%s_probe matched command=%s delta=0x%x addr=0x%08x" %
            (label, command, runtime_delta, addr))
        return addr
    log("%s_probe exact-miss command=%s delta=0x%x addr=0x%08x words=%s" %
        (label, command, runtime_delta, addr,
         ",".join("0x%08x" % word for word in words[:5])))
    return None


def monitor_read_u32(sock, addr, phys_delta=None):
    cmd = "x"
    read_addr = addr
    if phys_delta is not None:
        cmd = "xp"
        read_addr = addr - phys_delta
    reply = monitor_cmd(sock, "%s/1wx 0x%08x" % (cmd, read_addr), 0.2)
    words = parse_monitor_words(reply, read_addr)
    return words[0] if words else None


def monitor_read_virtual_words(sock, addr, count):
    page_gpa = monitor_gva_to_gpa(sock, addr)
    if page_gpa is None:
        return []
    reply = monitor_cmd(
        sock, "xp/%dwx 0x%08x" % (count, page_gpa), 0.2)
    return parse_monitor_words(reply, page_gpa)


def monitor_read_virtual_cstring(sock, addr, limit=64):
    if not addr:
        return "<null>"
    page_gpa = monitor_gva_to_gpa(sock, addr)
    if page_gpa is None:
        return "<unmapped>"
    reply = monitor_cmd(
        sock, "xp/%dbx 0x%08x" % (limit, page_gpa), 0.2)
    data = []
    for line in reply.splitlines():
        if ":" not in line:
            continue
        values_text = line.split(":", 1)[1]
        for token in re.findall(r"(?i)0x([0-9a-f]{2})\b", values_text):
            value = int(token, 16)
            if value == 0:
                return bytes(data).decode("latin-1", errors="replace")
            data.append(value)
    return bytes(data).decode("latin-1", errors="replace")


def monitor_gva_to_gpa(sock, addr):
    reply = monitor_cmd(sock, "gva2gpa 0x%08x" % addr, 0.15)
    match = re.search(r"(?i)gpa\s*:\s*0x([0-9a-f]+)", reply)
    if match:
        return int(match.group(1), 16)
    values = re.findall(r"(?i)0x([0-9a-f]+)", reply)
    return int(values[-1], 16) if values else None


def monitor_read_virtual_symbol_pages(sock, base_va, offsets, symbols,
                                      extra_words=None):
    """Translate and read each virtual page through its live guest mapping."""
    extra_words = extra_words or {}
    pages = {}
    for symbol in symbols:
        word_offset = offsets.get(symbol)
        if word_offset is None:
            continue
        count = max(1, int(extra_words.get(symbol, 1)))
        for slot in range(count):
            va = base_va + (word_offset + slot) * 4
            key = (symbol, slot) if slot else symbol
            pages.setdefault(va & ~0xFFF, []).append((key, va))

    values = {}
    for page_va, page_symbols in pages.items():
        start = min(va for _symbol, va in page_symbols) & ~3
        end = max(va for _symbol, va in page_symbols) + 4
        word_count = (end - start) // 4
        try:
            page_gpa = monitor_gva_to_gpa(sock, page_va)
            if page_gpa is None:
                continue
            read_addr = page_gpa + (start - page_va)
            reply = monitor_cmd(
                sock, "xp/%dwx 0x%08x" % (word_count, read_addr), 0.15)
        except OSError:
            continue
        words = parse_monitor_words(reply, read_addr)
        for symbol, va in page_symbols:
            index = (va - start) // 4
            if 0 <= index < len(words):
                values[symbol] = words[index]
    return values


def bind_keyboard_controller_port(toml, port):
    if port < 1 or port > 4:
        return toml

    section_match = re.search(
        r"(?ms)^\[input\.bindings\]\s*\n(.*?)(?=^\[|\Z)", toml)
    if not section_match:
        return toml

    body = section_match.group(1)
    body = re.sub(
        r"(?m)^port[1-4]\s*=\s*['\"]keyboard['\"]\s*\r?\n?", "", body)
    body = re.sub(
        r"(?m)^port%d\s*=.*\r?\n?" % port, "", body)
    driver_pattern = re.compile(
        r"(?m)^(port%d_driver\s*=.*)$" % port)
    if driver_pattern.search(body):
        body = driver_pattern.sub(
            r"\1\nport%d = 'keyboard'" % port, body, count=1)
    else:
        body = (body.rstrip() +
                "\nport%d_driver = 'usb-xbox-gamepad'\n" % port +
                "port%d = 'keyboard'\n" % port)
    return toml[:section_match.start(1)] + body + toml[section_match.end(1):]


def monitor_texture_allocator(sock, addr, command):
    header_reply = monitor_cmd(
        sock, "%s/4wx 0x%08x" % (command, addr), 0.12)
    header = parse_monitor_words(header_reply, addr)
    if len(header) < 4:
        return None

    base, used, capacity, high_water = header[:4]
    total_free = max(0, capacity - used)

    return {
        "base": base,
        "used": used,
        "capacity": capacity,
        "high_water": high_water,
        "block_count": 0,
        "total_free": total_free,
        "largest_free": total_free,
        "complete": True,
    }


def monitor_pmemsave(sock, phys_addr, byte_count, path):
    monitor_path = os.path.abspath(path).replace("\\", "/")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    monitor_cmd(sock, "pmemsave 0x%08x 0x%x %s" % (phys_addr, byte_count, monitor_path), 1.0)
    return os.path.exists(path) and os.path.getsize(path) >= byte_count


def monitor_framebuffer_dump_paused(sock, path, phys_delta=None):
    if sock is None:
        return False, "monitor disabled"

    required = [
        "_g_SPXBFramebufferData",
        "_g_SPXBFramebufferPitch",
        "_g_SPXBFramebufferWidth",
        "_g_SPXBFramebufferHeight",
    ]
    addrs = {}
    for name in required:
        runtime_va, _map_path, _linked_va = resolve_runtime_map_symbol(name)
        if runtime_va is None:
            return False, "framebuffer symbol missing: %s" % name
        addrs[name] = runtime_va

    candidate_deltas = []
    if phys_delta is not None:
        candidate_deltas.append(phys_delta)
    candidate_deltas.extend([None, 0x284000, 0x2a4000, 0x2a3000, 0x280000, 0x2a5000, 0x2a6000])

    seen_deltas = set()
    data = pitch = width = height = 0
    used_delta = None
    probe_details = []
    for candidate_delta in candidate_deltas:
        key = -1 if candidate_delta is None else candidate_delta
        if key in seen_deltas:
            continue
        seen_deltas.add(key)

        probe_data = monitor_read_u32(sock, addrs["_g_SPXBFramebufferData"], candidate_delta) or 0
        probe_pitch = monitor_read_u32(sock, addrs["_g_SPXBFramebufferPitch"], candidate_delta) or 0
        probe_width = monitor_read_u32(sock, addrs["_g_SPXBFramebufferWidth"], candidate_delta) or 0
        probe_height = monitor_read_u32(sock, addrs["_g_SPXBFramebufferHeight"], candidate_delta) or 0
        probe_details.append("%s:data=0x%08x pitch=%u size=%ux%u" % (
            "virt" if candidate_delta is None else "0x%x" % candidate_delta,
            probe_data, probe_pitch, probe_width, probe_height))

        if (probe_data != 0 and probe_pitch != 0 and probe_width != 0 and probe_height != 0 and
            probe_width <= 1920 and probe_height <= 1080 and probe_pitch >= probe_width * 4):
            data = probe_data
            pitch = probe_pitch
            width = probe_width
            height = probe_height
            used_delta = candidate_delta
            break

    if data == 0 or pitch == 0 or width == 0 or height == 0:
        return False, "framebuffer telemetry empty probes=%s" % "; ".join(probe_details[:8])
    if width > 1920 or height > 1080 or pitch < width * 4:
        return False, "framebuffer telemetry invalid data=0x%08x pitch=%u size=%ux%u probes=%s" % (
            data, pitch, width, height, "; ".join(probe_details[:8]))

    bin_path = os.path.abspath(os.path.splitext(path)[0] + ".fb.bin")
    byte_count = pitch * height
    phys_addr = data & 0x03ffffff
    if not monitor_pmemsave(sock, phys_addr, byte_count, bin_path):
        return False, "pmemsave missing data=0x%08x phys=0x%08x bytes=%u" % (data, phys_addr, byte_count)

    try:
        raw = open(bin_path, "rb").read()
        img = Image.new("RGB", (width, height))
        pixels = bytearray()
        for y in range(height):
            base = y * pitch
            for x in range(width):
                p = base + x * 4
                if p + 2 < len(raw):
                    pixels += bytes((raw[p + 2], raw[p + 1], raw[p]))
                else:
                    pixels += b"\x00\x00\x00"
        img.frombytes(bytes(pixels))
        img.save(path)
        try:
            os.remove(bin_path)
        except Exception:
            pass
        return True, "guest framebuffer %ux%u pitch=%u data=0x%08x delta=%s" % (
            width, height, pitch, data, "virt" if used_delta is None else "0x%x" % used_delta)
    except Exception as exc:
        return False, "framebuffer convert failed: %s" % exc


def monitor_framebuffer_dump(sock, path, phys_delta=None):
    """Capture one coherent guest frame while its backing buffer is immutable.

    Xemu's guest framebuffer pointer can refer to a buffer that the NV2A starts
    reusing immediately after presentation.  Reading that memory with pmemsave
    while emulation continues can therefore splice two frames together or catch
    a transition clear.  Stop/cont brackets make the debugger capture atomic;
    conversion also stays inside the bracket so every early return resumes the
    guest through the finally block.
    """
    if sock is None:
        return False, "monitor disabled"

    paused = False
    try:
        monitor_cmd(sock, "stop", 0.15)
        paused = True
        return monitor_framebuffer_dump_paused(sock, path, phys_delta)
    except OSError as exc:
        return False, "framebuffer capture monitor error: %s" % exc
    finally:
        if paused:
            try:
                monitor_cmd(sock, "cont", 0.15)
            except OSError:
                pass


def monitor_screendump(sock, path, phys_delta=None):
    return monitor_framebuffer_dump(sock, path, phys_delta)

    # Kept for reference: this Xemu build exposes a QEMU monitor without the
    # screendump command, so screenshots must come from guest framebuffer memory.
    if sock is None:
        return False, "monitor disabled"
    ppm_path = os.path.abspath(os.path.splitext(path)[0] + ".ppm")
    monitor_path = ppm_path.replace("\\", "/")
    try:
        if os.path.exists(ppm_path):
            os.remove(ppm_path)
        monitor_cmd(sock, 'screendump "%s"' % monitor_path, 1.0)
        if not os.path.exists(ppm_path) or os.path.getsize(ppm_path) == 0:
            return False, "screendump missing"
        img = Image.open(ppm_path).convert("RGB")
        img.save(path)
        try:
            os.remove(ppm_path)
        except Exception:
            pass
        return True, "monitor screendump %dx%d" % (img.width, img.height)
    except Exception as exc:
        return False, "screendump failed: %s" % exc


def dump_monitor_state(sock, prefix, tag, dump_specs, log):
    try:
        regs = monitor_cmd(sock, "info registers", 0.1)
    except OSError as exc:
        log("%s_registers_unavailable=%s" % (tag, exc))
        return ""
    regs_path = os.path.abspath("%s_%s_registers.txt" % (prefix, tag))
    with open(regs_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(regs)
    log("%s_registers=%s" % (tag, regs_path))

    for spec in dump_specs:
        parts = spec.split(":", 2)
        if len(parts) < 2:
            log("%s_dump_mem_invalid=%s" % (tag, spec))
            continue
        try:
            addr = int(parts[0], 0)
            length = int(parts[1], 0)
        except ValueError:
            log("%s_dump_mem_invalid=%s" % (tag, spec))
            continue
        name = parts[2] if len(parts) > 2 and parts[2] else "mem_%08x" % addr
        words = (length + 3) // 4
        try:
            dump = monitor_cmd(sock, "x/%dwx 0x%08x" % (words, addr), 2.0)
        except OSError as exc:
            log("%s_dump_mem_unavailable=%s err=%s" % (tag, spec, exc))
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        dump_path = os.path.abspath("%s_%s_%s.txt" % (prefix, tag, safe_name))
        with open(dump_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(dump)
        log("%s_dump_mem=%s" % (tag, dump_path))

    return regs


def dump_physical_memory(sock, prefix, specs, log):
    if sock is None:
        return
    for spec in specs:
        parts = spec.split(":", 2)
        if len(parts) < 2:
            log("final_dump_phys_invalid=%s" % spec)
            continue
        try:
            addr = int(parts[0], 0)
            length = int(parts[1], 0)
        except ValueError:
            log("final_dump_phys_invalid=%s" % spec)
            continue
        name = parts[2] if len(parts) > 2 and parts[2] else "phys_%08x" % addr
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        dump_path = os.path.abspath("%s_final_%s.bin" % (prefix, safe_name))
        monitor_path = dump_path.replace("\\", "/")
        try:
            reply = monitor_cmd(sock, 'pmemsave 0x%08x 0x%x "%s"' % (addr, length, monitor_path), 4.0)
        except OSError as exc:
            log("final_dump_phys_unavailable=%s err=%s" % (spec, exc))
            continue
        log("final_dump_phys=%s" % dump_path)
        if reply.strip():
            log("final_dump_phys_reply=present bytes=%u" % len(reply))


def extract_xblog_profiles_from_physical_memory(sock, prefix, log):
    """Recover completed in-guest frame profiles without polling relocated globals."""
    if sock is None:
        log("xblog_profile_extract skipped reason=no-monitor")
        return []

    scratch_path = os.path.abspath("%s_final_guest_ram.tmp" % prefix)
    output_path = os.path.abspath("%s_xblog_profiles.log" % prefix)
    monitor_path = scratch_path.replace("\\", "/")
    markers = (b"STEFX_HW_FRAME_PROFILE:", b"STEFX_HW_RENDER_SAMPLE:")
    records = []
    try:
        reply = monitor_cmd(
            sock,
            'pmemsave 0x00000000 0x04000000 "%s"' % monitor_path,
            20.0)
        if reply.strip():
            log("xblog_profile_extract_reply=present bytes=%u" % len(reply))
        if not os.path.isfile(scratch_path):
            log("xblog_profile_extract failed reason=ram-dump-missing")
            return []

        with open(scratch_path, "rb") as f:
            ram = f.read()
        cursor = 0
        seen = set()
        while True:
            starts = [ram.find(marker, cursor) for marker in markers]
            starts = [start for start in starts if start >= 0]
            start = min(starts) if starts else -1
            if start < 0:
                break
            cursor = start + 1
            limit = min(len(ram), start + 1024)
            end = limit
            for terminator in (b"\x00", b"\r", b"\n"):
                candidate = ram.find(terminator, start, limit)
                if candidate >= 0:
                    end = min(end, candidate)
            line = ram[start:end].decode("ascii", errors="ignore").strip()
            # The executable also contains the printf format string. Accept
            # only populated runtime records from the XBLog mirror/last-line.
            is_frame_profile = line.startswith("STEFX_HW_FRAME_PROFILE:")
            is_render_sample = line.startswith("STEFX_HW_RENDER_SAMPLE:")
            if is_frame_profile:
                if not re.search(r"\bframe=\d+\b", line):
                    continue
                if not re.search(r"\bfps=\d+\.\d+\b", line):
                    continue
            elif not is_render_sample:
                continue
            if " total=" not in line or " backend=" not in line:
                continue
            # Stack/log scratch can retain a valid prefix after the source
            # buffer is reused.  The dedicated profile ring always contains
            # the complete record through the final texture-capacity field.
            if (is_frame_profile and
                    not re.search(r"\bskinTexCapKB=\d+$", line)):
                continue
            if (is_render_sample and
                    not re.search(r"\breserveDwords=\d+/\d+$", line)):
                continue
            if line not in seen:
                seen.add(line)
                records.append(line)

        records.sort(key=lambda line: (
            int(re.search(r"\bsample=(\d+)\b", line).group(1)),
            0 if line.startswith("STEFX_HW_FRAME_PROFILE:") else 1))
        if records:
            with open(output_path, "w", encoding="ascii", errors="replace") as f:
                f.write("\n".join(records) + "\n")
            latest_sample = re.search(
                r"\bsample=(\d+)\b", records[-1]).group(1)
            log("xblog_profile_extract records=%u latest_sample=%s path=%s" %
                (len(records), latest_sample, output_path))
        else:
            marker_hits = sum(ram.count(marker) for marker in markers)
            log("xblog_profile_extract records=0 marker_hits=%u" % marker_hits)
        return records
    except (OSError, ValueError) as exc:
        log("xblog_profile_extract failed err=%s" % exc)
        return []
    finally:
        try:
            if os.path.exists(scratch_path):
                os.remove(scratch_path)
                log("xblog_profile_extract scratch_removed=%s" % scratch_path)
        except OSError as exc:
            log("xblog_profile_extract scratch_remove_failed=%s err=%s" %
                (scratch_path, exc))


def dump_virtual_memory_binary(sock, prefix, specs, log):
    if sock is None:
        return
    for spec in specs:
        parts = spec.split(":", 2)
        if len(parts) < 2:
            log("final_dump_bin_mem_invalid=%s" % spec)
            continue
        try:
            addr = int(parts[0], 0)
            length = int(parts[1], 0)
        except ValueError:
            log("final_dump_bin_mem_invalid=%s" % spec)
            continue
        name = parts[2] if len(parts) > 2 and parts[2] else "mem_%08x" % addr
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        dump_path = os.path.abspath("%s_final_%s.bin" % (prefix, safe_name))
        monitor_path = dump_path.replace("\\", "/")
        try:
            reply = monitor_cmd(sock, 'memsave 0x%08x 0x%x "%s"' % (addr, length, monitor_path), 4.0)
        except OSError as exc:
            log("final_dump_bin_mem_unavailable=%s err=%s" % (spec, exc))
            continue
        log("final_dump_bin_mem=%s" % dump_path)
        if reply.strip():
            log("final_dump_bin_mem_reply=present bytes=%u" % len(reply))


def append_xblog_auto_dumps(specs, phys_delta, log):
    if phys_delta is None:
        log("xblog_auto_dump skipped reason=no-phys-delta")
        return specs

    out = list(specs)
    for symbol, length, name in (
        ("_g_SPXBLogMirror", 0x8000, "sp_log_mirror_auto"),
        ("_g_SPXBLogLastLine", 0x200, "sp_log_lastline_auto"),
        ("_g_SPXBSVProbeMagic", 0x40, "stefx_sv_probe_auto"),
    ):
        va, map_path, linked_va = resolve_runtime_map_symbol(symbol)
        if va is None:
            log("xblog_auto_dump skipped symbol=%s reason=unresolved" % symbol)
            continue
        phys = va - phys_delta
        if phys <= 0:
            log("xblog_auto_dump skipped symbol=%s va=0x%08x delta=0x%x" % (symbol, va, phys_delta))
            continue
        out.append("0x%08x:0x%x:%s" % (phys, length, name))
        log("xblog_auto_dump symbol=%s linked=0x%08x va=0x%08x phys=0x%08x len=0x%x map=%s" %
            (symbol, linked_va or 0, va, phys, length, map_path))
        bss_phys = phys + 0x1000
        bss_name = "%s_bss" % name
        out.append("0x%08x:0x%x:%s" % (bss_phys, length, bss_name))
        log("xblog_auto_dump_bss symbol=%s va=0x%08x phys=0x%08x len=0x%x" %
            (symbol, va, bss_phys, length))
    return out


def append_xblog_auto_virtual_dumps(specs, log):
    out = list(specs)
    for symbol, length, name in (
        ("_g_SPXBLogMirror", 0x8000, "sp_log_mirror_auto_vmem"),
        ("_g_SPXBLogLastLine", 0x200, "sp_log_lastline_auto_vmem"),
        ("_g_SPXBSVProbeMagic", 0x40, "stefx_sv_probe_auto_vmem"),
    ):
        va, map_path, linked_va = resolve_runtime_map_symbol(symbol)
        if va is None:
            log("xblog_auto_vmem skipped symbol=%s reason=unresolved" % symbol)
            continue
        out.append("0x%08x:0x%x:%s" % (va, length, name))
        log("xblog_auto_vmem symbol=%s linked=0x%08x va=0x%08x len=0x%x map=%s" %
            (symbol, linked_va or 0, va, length, map_path))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--name", default="ja_xemu")
    parser.add_argument("--port", type=int, default=4460)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--first-shot-delay", type=float, default=0.0,
                        help="Seconds to wait before the first framebuffer capture.")
    parser.add_argument("--hdd", default=r"C:\Games\Emulators\Xemu\HDD\xbox_hdd.qcow2")
    parser.add_argument("--xemu-exe", default=XEMU_JA,
                        help="Xemu executable to launch. Defaults to the JA-isolated copy.")
    parser.add_argument("--config-path", default="",
                        help="Optional xemu.toml path to update and pass to Xemu with -config_path.")
    parser.add_argument("--explicit-xbox-machine", action="store_true",
                        help="Launch with explicit OG Xbox machine/BIOS/HDD/DVD args instead of relying on xemu.toml.")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--headless", action="store_true",
                        help="Run without a display window. Implies --display none and skips screenshots.")
    parser.add_argument("--no-screenshots", action="store_true",
                        help="Skip interval screenshots without changing the display backend.")
    parser.add_argument("--xemu-native-screenshots", action="store_true",
                        help="Capture through XEMU's built-in F12 screenshot action.")
    parser.add_argument("--xemu-screenshot-dir", default="",
                        help="Directory watched for XEMU native screenshot PNGs.")
    parser.add_argument("--display", choices=["xemu", "sdl", "none", "egl-headless", "nographic"],
                        default="xemu",
                        help="Xemu/QEMU display backend. Use none or egl-headless for unattended runs.")
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--monitor-keys", default="")
    parser.add_argument("--host-window-keys", action="store_true",
                        help="Send scheduled keys to XEMU's window without taking focus.")
    parser.add_argument("--smoke-keymap", action="store_true")
    parser.add_argument("--keyboard-controller-port", type=int, choices=range(1, 5),
                        default=0,
                        help="Bind XEMU keyboard input to the selected Xbox controller port.")
    parser.add_argument("--xemu-arg", action="append", default=[])
    parser.add_argument("--runtime-xbe", action="append", default=[],
                        help="Record SHA256 and STEFX_RUNTIME_BUILD_ID for an XBE packaged in this run.")
    parser.add_argument("--require-runtime-xbe-id", action="store_true",
                        help="Fail before launch if any --runtime-xbe is missing its STEFX_RUNTIME_BUILD_ID or has the wrong personality/log identity.")
    parser.add_argument("--proof-mode", choices=["sp", "coop", "mp"], default="",
                        help="Record the qualification mode represented by this smoke proof.")
    parser.add_argument("--proof-map", default="",
                        help="Record the map or boot target represented by this smoke proof.")
    parser.add_argument("--keep-net", action="store_true")
    parser.add_argument("--dump-mem", action="append", default=[],
                        help="Dump guest memory before closing monitor: addr:length[:name]")
    parser.add_argument("--dump-bin-mem", action="append", default=[],
                        help="Dump guest virtual memory bytes with monitor memsave: addr:length[:name]")
    parser.add_argument("--dump-phys", action="append", default=[],
                        help="Dump guest physical memory before closing monitor: addr:length[:name]")
    parser.add_argument("--xblog-auto-dumps", action="store_true",
                        help="Add XBLog mirror/last-line physical and virtual dumps from resolved symbols.")
    parser.add_argument("--extract-xblog-profile", action="store_true",
                        help="At run end, scan one temporary 64 MiB RAM snapshot for completed STEFX_HW_FRAME_PROFILE records.")
    parser.add_argument("--watch-cr2", default="",
                        help="Poll registers and dump memory when CR2 matches this value")
    parser.add_argument("--sample-eip-interval", type=float, default=0.0,
                        help="Sample guest EIP through the monitor at this real-time interval.")
    parser.add_argument("--poll-word-addr", default="",
                        help="Poll one guest virtual 32-bit counter address during the run.")
    parser.add_argument("--poll-word-count", type=int, default=1,
                        help="Number of adjacent guest words to snapshot (default: 1).")
    parser.add_argument("--poll-word-interval", type=float, default=1.0,
                        help="Seconds between generic guest counter polls.")
    parser.add_argument("--poll-word-label", default="counter",
                        help="Label used for generic guest counter samples and summary.")
    parser.add_argument("--poll-xblog", action="store_true",
                        help="Poll SP/MP XBLog counters through the monitor during the run.")
    parser.add_argument("--poll-xblog-perf-only", action="store_true",
                        help="Poll only the compact heartbeat/performance range.")
    parser.add_argument("--poll-xblog-start-delay", type=float, default=0.0,
                        help="Delay the first XBLog poll to avoid disturbing timed gameplay.")
    parser.add_argument("--poll-xblog-interval", type=float, default=1.0,
                        help="Seconds between guest heartbeat polls.")
    parser.add_argument("--poll-xblog-kind", choices=["auto", "sp", "mp"], default="auto",
                        help="Prefer SP or MP XBLog symbols when polling. Auto preserves the historical probe order.")
    parser.add_argument("--map-file", default="",
                        help="Prefer symbols from this linker map for telemetry and memory dumps.")
    parser.add_argument("--poll-xblog-addr", default="",
                        help="Address of g_*XBBootPhase. Empty means resolve _g_SPXBBootPhase from the current map.")
    parser.add_argument("--poll-xblog-phys-addr", default="",
                        help="Exact physical address of g_*XBBootPhase; bypasses automatic physical probing.")
    parser.add_argument("--poll-xblog-phys-delta", default="0x284000",
                        help="Auto-resolved XBLog VA minus physical monitor address. Use 0 to poll virtual x/ memory.")
    args = parser.parse_args()

    global MAP_PATH_OVERRIDE
    if args.map_file:
        MAP_PATH_OVERRIDE = os.path.abspath(args.map_file)
        if not os.path.isfile(MAP_PATH_OVERRIDE):
            parser.error("Map file not found: %s" % MAP_PATH_OVERRIDE)

    os.makedirs(OUT_DIR, exist_ok=True)
    args.iso = os.path.abspath(args.iso)
    args.hdd = os.path.abspath(args.hdd)
    if not os.path.isfile(args.iso):
        parser.error("ISO not found: %s" % args.iso)
    if not os.path.isfile(args.hdd):
        parser.error("HDD image not found: %s" % args.hdd)
    xemu_exe = os.path.abspath(args.xemu_exe)
    if os.path.normcase(xemu_exe) == os.path.normcase(os.path.abspath(XEMU_JA)):
        ensure_xemu_copy()
    config_path = os.path.abspath(args.config_path) if args.config_path else XEMU_TOML

    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = os.path.join(OUT_DIR, "%s_%s" % (args.name, stamp))
    raw_log = prefix + ".xemu.txt"
    report = prefix + ".report.txt"
    if args.xemu_screenshot_dir:
        xemu_screenshot_dir = os.path.abspath(args.xemu_screenshot_dir)
    elif config_path:
        xemu_screenshot_dir = os.path.join(os.path.dirname(config_path), "screenshots")
    else:
        xemu_screenshot_dir = os.path.abspath(prefix + "_xemu_screenshots")
    toml_backup = None

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8", errors="replace") as f:
            toml_backup = f.read()
        toml = re.sub(r"hdd_path = '.*'", lambda _m: "hdd_path = '%s'" % args.hdd, toml_backup)
        toml = re.sub(r"dvd_path = '.*'", lambda _m: "dvd_path = '%s'" % args.iso, toml)
        if not args.keep_net:
            toml = re.sub(r"(?m)^enable = true$", "enable = false", toml, count=1)
        if args.smoke_keymap:
            toml = re.sub(r"(?ms)\n?\[input\.keyboard_controller_scancode_map\]\n.*?(?=\n\[|\Z)", "", toml)
            toml = toml.rstrip() + "\n\n" + SMOKE_KEYBOARD_MAP + "\n"
        if args.keyboard_controller_port:
            toml = bind_keyboard_controller_port(
                toml, args.keyboard_controller_port)
        with open(config_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(toml)

    if args.explicit_xbox_machine:
        argv = [
            xemu_exe,
            "-machine", r"xbox,bootrom=C:\Games\Emulators\Xemu\MCPX\mcpx_1.0.bin,short-animation=on,kernel-irqchip=off,avpack=hdtv",
            "-device", r"smbus-storage,file=C:\Games\Emulators\Xemu\EEPROM\eeprom.bin",
            "-bios", r"C:\Games\Emulators\Xemu\BIOS\xbox-4627_debug.bin",
            "-m", "64",
            "-drive", "index=0,media=disk,file=%s,locked=on" % args.hdd,
            "-drive", "index=1,media=cdrom,file=%s" % args.iso,
        ]
    else:
        argv = [
            xemu_exe,
            "-dvd_path", args.iso,
        ]
    if args.config_path:
        argv += ["-config_path", config_path]
    if not args.no_monitor:
        argv += ["-monitor", "tcp:127.0.0.1:%d,server,nowait" % args.port]
    if not args.visible:
        # Keep the xemu display backend, but start it minimized-ish via QEMU's
        # normal window. We still capture frames through the monitor.
        pass
    display_mode = "none" if args.headless else args.display
    if display_mode != "xemu":
        if display_mode == "nographic":
            argv += ["-nographic"]
        else:
            argv += ["-display", display_mode]
    argv += args.xemu_arg

    lines = []
    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    xblog_auto_phys_pending = False
    if args.xblog_auto_dumps:
        phys_delta_for_auto = None
        try:
            phys_delta_for_auto = int(args.poll_xblog_phys_delta, 0)
        except ValueError:
            phys_delta_for_auto = None
        if args.poll_xblog:
            xblog_auto_phys_pending = True
            log("xblog_auto_dump deferred reason=awaiting-probed-phys-delta")
        else:
            args.dump_phys = append_xblog_auto_dumps(args.dump_phys, phys_delta_for_auto, log)
        args.dump_bin_mem = append_xblog_auto_virtual_dumps(args.dump_bin_mem, log)

    log("launch: %s" % " ".join(argv))
    if args.proof_mode or args.proof_map:
        log("proof_context mode=%s map=%s name=%s duration=%u interval=%u display=%s nativeScreenshots=%s" %
            (args.proof_mode or "<unspecified>", args.proof_map or "<unspecified>",
             args.name, args.duration, args.interval, display_mode, args.xemu_native_screenshots))
    runtime_identity_failures = 0
    if args.require_runtime_xbe_id and not args.runtime_xbe:
        log("runtime_xbe_identity required=True status=fail reason=no-runtime-xbe")
        runtime_identity_failures += 1
    for runtime_xbe in args.runtime_xbe:
        xbe_path = os.path.abspath(runtime_xbe)
        if not os.path.isfile(xbe_path):
            log("runtime_xbe_identity path=%s present=False" % xbe_path)
            if args.require_runtime_xbe_id:
                runtime_identity_failures += 1
            continue
        runtime_build_id = runtime_build_id_from_xbe(xbe_path)
        log("runtime_xbe_identity path=%s present=True bytes=%u sha256=%s runtimeBuildId=%s" %
            (xbe_path, os.path.getsize(xbe_path), file_sha256(xbe_path),
             runtime_build_id or "<missing>"))
        if args.require_runtime_xbe_id and not runtime_build_id:
            runtime_identity_failures += 1
        identity_failures = runtime_build_id_identity_failures(xbe_path, runtime_build_id)
        for fragment in identity_failures:
            log("runtime_xbe_identity path=%s status=fail reason=wrong-identity missing=%s" %
                (xbe_path, fragment))
        if args.require_runtime_xbe_id and identity_failures:
            runtime_identity_failures += 1
    if runtime_identity_failures:
        with open(report, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines) + "\n")
        print("report=%s" % report)
        return 1
    logf = open(raw_log, "w", encoding="utf-8", errors="replace")
    xemu_cwd = os.path.dirname(xemu_exe) or XEMU_DIR
    popen_options = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(
            subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        if not args.visible:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE
            popen_options["startupinfo"] = startupinfo
    proc = subprocess.Popen(
        argv,
        cwd=xemu_cwd,
        stdout=logf,
        stderr=subprocess.STDOUT,
        **popen_options)
    log("pid=%d" % proc.pid)
    try:
        affinity_mask = cap_windows_process_affinity(proc.pid)
        if affinity_mask is not None:
            log("process_affinity_mask=0x%X" % affinity_mask)
    except OSError as exc:
        log("process_affinity_warning=%s" % exc)
    monitor_key_events = parse_key_schedule(args.monitor_keys)
    shot_paths = []
    active_fps_samples = []
    gameplay_fps_samples = []
    personality_active_fps_samples = {}
    personality_gameplay_fps_samples = {}
    active_wall_fps_samples = []
    gameplay_wall_fps_samples = []
    personality_active_wall_fps_samples = {}
    personality_gameplay_wall_fps_samples = {}
    last_wall_frame_sample = {}
    wall_fps_sample = None
    emulation_speed = None
    last_fps_heartbeat_count = None
    poll_word_va = int(args.poll_word_addr, 0) if args.poll_word_addr else None
    poll_word_gpa = None
    poll_word_previous = None
    poll_word_rates = []
    next_poll_word = 0.0
    watch_cr2 = int(args.watch_cr2, 0) if args.watch_cr2 else None
    watch_done = False
    next_watch = 0.0
    xblog_addr = None
    xblog_mode = ""
    xblog_mp_pos_addr = None
    xblog_mp_write_addr = None
    xblog_va_for_probe = None
    xblog_map = None
    if args.poll_xblog:
        if args.poll_xblog_phys_addr:
            xblog_addr = int(args.poll_xblog_phys_addr, 0)
            xblog_va_for_probe, xblog_map, xblog_linked_va = \
                resolve_runtime_map_symbol("_g_SPXBBootPhase")
            xblog_cmd = "xp"
            xblog_mode = "sp"
            log("xblog_symbol=_g_SPXBBootPhase linked=0x%08x va=0x%08x poll=xp addr=0x%08x map=%s forced=1" %
                (xblog_linked_va or 0, xblog_va_for_probe or 0,
                 xblog_addr, xblog_map))
        elif args.poll_xblog_addr:
            xblog_addr = int(args.poll_xblog_addr, 0)
            xblog_cmd = "x"
            xblog_mode = "sp"
        else:
            prefer_mp = args.poll_xblog_kind == "mp"
            prefer_sp = args.poll_xblog_kind == "sp"
            if prefer_mp:
                xblog_addr = None
            else:
                xblog_addr, xblog_map, xblog_linked_va = \
                    resolve_runtime_map_symbol("_g_SPXBBootPhase")
            if prefer_sp and xblog_addr is None:
                xblog_addr = 0x00d90528
                xblog_cmd = "x"
                xblog_mode = "sp"
                log("xblog_symbol=_g_SPXBBootPhase unresolved fallback=x/0x%08x" % xblog_addr)
            elif not prefer_mp and xblog_addr is not None:
                phys_delta = int(args.poll_xblog_phys_delta, 0)
                xblog_cmd = "xp" if phys_delta else "x"
                xblog_va = xblog_addr
                xblog_va_for_probe = xblog_va
                xblog_addr = xblog_addr - phys_delta
                xblog_mode = "sp"
                log("xblog_symbol=_g_SPXBBootPhase linked=0x%08x va=0x%08x poll=%s addr=0x%08x map=%s" %
                    (xblog_linked_va or 0, xblog_va, xblog_cmd,
                     xblog_addr, xblog_map))
            else:
                mp_pos, mp_pos_map, mp_pos_linked = \
                    resolve_runtime_map_symbol("_g_XBLogMirrorPos")
                mp_write, _mp_write_map, _mp_write_linked = \
                    resolve_runtime_map_symbol("_g_XBLogWriteCount")
                if mp_pos is not None and mp_write is not None:
                    phys_delta = int(args.poll_xblog_phys_delta, 0)
                    xblog_cmd = "xp" if phys_delta else "x"
                    xblog_mode = "mp"
                    xblog_addr = mp_pos - phys_delta
                    xblog_mp_pos_addr = mp_pos - phys_delta
                    xblog_mp_write_addr = mp_write - phys_delta
                    log("xblog_symbol=_g_XBLogMirrorPos linked=0x%08x va=0x%08x poll=%s addr=0x%08x map=%s" %
                        (mp_pos_linked or 0, mp_pos, xblog_cmd,
                         xblog_mp_pos_addr, mp_pos_map))
                elif not prefer_mp:
                    xblog_addr = 0x00d90528
                    xblog_cmd = "x"
                    xblog_mode = "sp"
                    log("xblog_symbol=_g_SPXBBootPhase unresolved fallback=x/0x%08x" % xblog_addr)
                else:
                    log("xblog_symbol=_g_XBLogMirrorPos unresolved")
    next_xblog_poll = max(0.0, args.poll_xblog_start_delay)
    next_xblog_probe = 0.0
    last_xblog_write_count = None
    texture_allocator_symbol = "?gStaticTextures@@3VStaticTextureAllocator@@A"
    texture_allocator_map = (
        xblog_map if xblog_map else os.path.join(
            "build", "release", "default.map"))
    texture_allocator_va = resolve_literal_map_symbol_in(
        texture_allocator_symbol, texture_allocator_map)
    texture_allocator_runtime_va = (
        linked_pe_va_to_xbe_va(texture_allocator_va, texture_allocator_map)
        if texture_allocator_va is not None else None)
    texture_allocator_gpa = None
    next_texture_allocator_poll = 0.0
    hm_ingame_text_map = os.path.join("build", "release", "efmp.map")
    hm_ingame_text_va = resolve_runtime_map_symbol_in(
        "_STEFX_HM_CG_ingame_text", hm_ingame_text_map)
    hm_ingame_source_va = resolve_runtime_map_symbol_in(
        "_STEFX_HM_CG_ingameText", hm_ingame_text_map)
    hm_text_trace_va = resolve_runtime_map_symbol_in(
        "_g_SPXBHMTextTraceCalls", hm_ingame_text_map)
    hm_split_pose_symbols = [
        "_g_SPXBHMSplitStateOriginX",
        "_g_SPXBHMSplitStateOriginY",
        "_g_SPXBHMSplitStateOriginZ",
        "_g_SPXBHMSplitStateViewPitch",
        "_g_SPXBHMSplitStateViewYaw",
        "_g_SPXBHMSplitStateViewRoll",
        "_g_SPXBHMSplitStateTime",
    ]
    hm_split_pose_base_va = None
    hm_split_pose_offsets = {}
    if args.proof_mode == "mp":
        hm_split_pose_base_va = resolve_runtime_map_symbol_in(
            hm_split_pose_symbols[0], hm_ingame_text_map)
        hm_split_pose_offsets = resolve_symbol_offsets_in(
            hm_split_pose_symbols[0], hm_split_pose_symbols, hm_ingame_text_map)
    if texture_allocator_va is not None:
        log("texture_allocator_symbol=%s va=0x%08x xbe=0x%08x map=%s" %
            (texture_allocator_symbol, texture_allocator_va,
             texture_allocator_runtime_va or 0, texture_allocator_map))
    else:
        log("texture_allocator_symbol=%s unresolved map=%s" %
            (texture_allocator_symbol, texture_allocator_map))
    if hm_ingame_text_va is not None:
        log("xblogtext_symbol=_STEFX_HM_CG_ingame_text va=0x%08x source=0x%08x map=%s" %
            (hm_ingame_text_va, hm_ingame_source_va or 0,
             hm_ingame_text_map))
    else:
        log("xblogtext_symbol=_STEFX_HM_CG_ingame_text unresolved map=%s" %
            hm_ingame_text_map)
    if args.proof_mode == "mp":
        if hm_split_pose_base_va is not None and hm_split_pose_offsets:
            log("split_pose_symbol=%s va=0x%08x words=%u map=%s" %
                (hm_split_pose_symbols[0], hm_split_pose_base_va,
                 max(hm_split_pose_offsets.values()) + 4, hm_ingame_text_map))
        else:
            log("split_pose_symbol=%s unresolved map=%s" %
                (hm_split_pose_symbols[0], hm_ingame_text_map))
    xblog_offsets = resolve_symbol_offsets("_g_SPXBBootPhase", [
        "_g_SPXBWorkloadSurfaces",
        "_g_SPXBWorkloadBatches",
        "_g_SPXBWorkloadVertexes",
        "_g_SPXBWorkloadIndexes",
        "_g_SPXBWorkloadTotalIndexes",
        "_g_SPXBUIStateMagic",
        "_g_SPXBUIStarted",
        "_g_SPXBUIKeyCatcher",
        "_g_SPXBUIPauseActive",
        "_g_SPXBUIQmenuActive",
        "_g_SPXBUIRefreshCount",
        "_g_SPXBUIPauseOpenCount",
        "_g_SPXBUIPauseDrawCount",
        "_g_SPXBUIFontMagic",
        "_g_SPXBMiniSoakMagic",
        "_g_SPXBMiniSoakStage",
        "_g_SPXBMiniSoakTransitions",
        "_g_SPXBMiniSoakActiveMsec",
        "_g_SPXBMiniSoakFlags",
        "_g_SPXBMainLoopCount",
        "_g_SPXBComFrameCount",
        "_g_SPXBSvFrameCount",
        "_g_SPXBClFrameCount",
        "_g_SPXBClsState",
        "_g_SPXBClServerTime",
        "_g_SPXBClsFrameCount",
        "_g_SPXBPhaseLast",
        "_g_SPXBInputPollCount",
        "_g_SPXBInputPort",
        "_g_SPXBInputDigital",
        "_g_SPXBInputAnalogMask",
        "_g_SPXBInputLXLY",
        "_g_SPXBInputRXRY",
        "_g_SPXBInputMenuEdgeCount",
        "_g_SPXBInputMenuEdgeLast",
        "_g_SPXBInputCommonPressCount",
        "_g_SPXBInputCommonPressLast",
        "_g_SPXBInputFrontendQueueCount",
        "_g_SPXBInputFrontendQueueLast",
        "_g_SPXBInputDispatchCount",
        "_g_SPXBInputDispatchLast",
        "_g_SPXBInputDispatchHandled",
        "_g_SPXBHMInfoDispatchCount",
        "_g_SPXBHMInfoDispatchLast",
        "_g_SPXBHMGameCommandCount",
        "_g_SPXBHMGameCommandResult",
        "_g_SPXBHMConsoleCommandCount",
        "_g_SPXBHMConsoleCommandTag",
        "_g_SPXBHMScoresDownCount",
        "_g_SPXBHMScoresUpCount",
        "_g_SPXBHMScoreDrawCount",
        "_g_SPXBHMScoreDrawState",
        "_g_SPXBHMTextLoadLength",
        "_g_SPXBHMTextLoadCount",
        "_g_SPXBHMTextLoadState",
        "_g_SPXBHMTextTraceCalls",
        "_g_SPXBHMTextTraceStage",
        "_g_SPXBHMTextTraceOpenLength",
        "_g_SPXBHMTextTraceHandle",
        "_g_SPXBHMTextTraceRawPrefix",
        "_g_SPXBHMTextTraceParsedPrefix",
        "_g_SPXBHMTextTraceScorePrefix",
        "_g_SPXBHMTextTraceFirstPointer",
        "_g_SPXBRenderCommandHighWater",
        "_g_SPXBRenderCommandDrops",
        "_g_SPXBRenderCommandLastDrop",
        "_g_SPXBRenderCommandCalls",
        "_g_SPXBRenderCommandIssueCount",
        "_g_SPXBRenderCommandLastUsed",
        "_g_SPXBHMScoreStretchCount",
        "_g_SPXBHMScoreStretchShader",
        "_g_SPXBHMScoreStretchX",
        "_g_SPXBHMScoreStretchY",
        "_g_SPXBHMScoreStretchW",
        "_g_SPXBHMScoreStretchH",
        "_g_SPXBHMScoreScaleX",
        "_g_SPXBHMScoreScaleY",
        "_g_SPXBHMScoreWhiteShader",
        "_g_SPXBHMScoreQueuedCount",
        "_g_SPXBHMScoreQueuedShader",
        "_g_SPXBHMScoreBackendMatches",
        "_g_SPXBHMScoreBackendColor",
        "_g_SPXBRenderBackendCommandCount",
        "_g_SPXBRenderBackendStretchCount",
        "_g_SPXBRenderBackendTerminalId",
        "_g_SPXBRenderBackendBytes",
        "_g_SPXBHMScoreBackendGeometry",
        "_g_SPXBHMScoreBackendGeomShader",
        "_g_SPXBHMScoreBackendGeomColor",
        "_g_SPXBRenderBackendDoneCommands",
        "_g_SPXBRenderBackendDoneStretches",
        "_g_SPXBRenderBackendDoneTerminal",
        "_g_SPXBRenderBackendDoneBytes",
        "_g_SPXBHMScoreBackendDoneGeometry",
        "_g_SPXBHMScoreBackendDoneShader",
        "_g_SPXBHMScoreBackendDoneColor",
        "_g_SPXBHMScoreBatchPending",
        "_g_SPXBHMScoreSurfaceFlags",
        "_g_SPXBHMScoreSurfaceVerts",
        "_g_SPXBHMScoreSurfaceIndexes",
        "_g_SPXBHMScoreSurfacePasses",
        "_g_SPXBHMScoreSurfaceState",
        "_g_SPXBHMScoreSubmitArmed",
        "_g_SPXBHMScoreSubmitCalls",
        "_g_SPXBHMScoreSubmitIndexes",
        "_g_SPXBHMScoreSubmitState",
        "_g_SPXBHMScoreSubmitTexture",
        "_g_SPXBHMScoreSubmitScissor",
        "_g_SPXBHMScoreSubmitScissorXY",
        "_g_SPXBHMScoreSubmitScissorWH",
        "_g_SPXBHMScoreShaderFlags",
        "_g_SPXBHMScoreImageTex",
        "_g_SPXBHMScoreWhiteTex",
        "_g_SPXBHMScoreImageWH",
        "_g_SPXBHMScoreSurfaceMinXY",
        "_g_SPXBHMScoreSurfaceMaxXY",
        "_g_SPXBHMScoreSubmitTarget",
        "_g_SPXBHMScoreSubmitColorWrite",
        "_g_SPXBHMScoreSubmitCull",
        "_g_SPXBHMScoreSubmitBlend",
        "_g_SPXBHMScoreSubmitViewportXY",
        "_g_SPXBHMScoreSubmitViewportWH",
        "_g_SPXBHMScoreSubmitProj00",
        "_g_SPXBHMScoreSubmitProj11",
        "_g_SPXBHMScoreSubmitProj30",
        "_g_SPXBHMScoreSubmitProj31",
        "_g_SPXBHMScoreTextureData0",
        "_g_SPXBHMScoreTextureSize",
        "_g_SPXBHMScoreTextureWH",
        "_g_SPXBHMScoreTextureFormat",
        "_g_SPXBHMScoreStageColor",
        "_g_SPXBHMScoreStageAlpha",
        "_g_SPXBHMScoreDepthState",
        "_g_SPXBHMScoreVertexShader",
        "_g_SPXBHMScorePixelShader",
        "_g_SPXBHMScoreVertex0X",
        "_g_SPXBHMScoreVertex0Y",
        "_g_SPXBHMScoreVertex0Z",
        "_g_SPXBHMScoreVertex0W",
        "_g_SPXBHMScoreVertex0Color",
        "_g_SPXBHMScoreVertex0U",
        "_g_SPXBHMScoreVertex0V",
        "_g_SPXBHMScoreVertexCount",
        "_g_SPXBHMScoreVertexStride",
        "_g_SPXBHMScoreIndex012",
        "_g_SPXBUsercmdCount",
        "_g_SPXBUsercmdTime",
        "_g_SPXBUsercmdMove",
        "_g_SPXBUsercmdButtons",
        "_g_SPXBUsercmdYaw",
        "_g_SPXBSVUsercmdCount",
        "_g_SPXBSVUsercmdTime",
        "_g_SPXBSVUsercmdMove",
        "_g_SPXBSVUsercmdButtons",
        "_g_SPXBHMGetUsercmdCount",
        "_g_SPXBHMGetUsercmdTime",
        "_g_SPXBHMGetUsercmdMove",
        "_g_SPXBHMGetUsercmdButtons",
        "_g_SPXBHMClientThinkCount",
        "_g_SPXBClTailStage",
        "_g_SPXBCGameEntryCurrent",
        "_g_SPXBCGameEntryExpected",
        "_g_SPXBComTailStage",
        "_g_SPXBComFrameDepth",
        "_g_SPXBComCatchCount",
        "_g_SPXBMainTailStage",
        "_g_SPXBComSubphase",
        "_g_SPXBComSpinCount",
        "_g_SPXBComMsec",
        "_g_SPXBComFrameTime",
        "_g_SPXBComLastTime",
        "_g_SPXBCbufExecCount",
        "_g_SPXBCbufExecDepth",
        "_g_SPXBCbufReturnAddressEntry",
        "_g_SPXBCbufReturnAddressExit",
        "_g_SPXBCmdExecCount",
        "_g_SPXBCmdPhase",
        "_g_SPXBCmdHash",
        "_g_SPXBCmdArgc",
        "_g_SPXBMapPhase",
        "_g_SPXBPackedMapPhase",
        "_g_SPXBMapHash",
        "_g_SPXBGamePhase",
        "_g_SPXBGameEntityCount",
        "_g_SPXBPerfFrameMsec",
        "_g_SPXBPerfServerMsec",
        "_g_SPXBPerfClientMsec",
        "_g_SPXBPerfGameMsec",
        "_g_SPXBPerfFrontendMsec",
        "_g_SPXBPerfBackendMsec",
        "_g_SPXBPerfBackendDrawSurfsMsec",
        "_g_SPXBPerfBackendSwapMsec",
        "_g_SPXBPerfBackendOtherMsec",
        "_g_SPXBPerfAudioMsec",
        "_g_SPXBHMAudioBackendState",
        "_g_SPXBHMAudioBeginRegistrationCount",
        "_g_SPXBHMAudioRegisterSoundCount",
        "_g_SPXBHMAudioStartSoundCount",
        "_g_SPXBHMAudioStartLocalCount",
        "_g_SPXBHMAudioLoopCount",
        "_g_SPXBHMAudioRespatializeCount",
        "_g_SPXBHMAudioListenerState",
        "_g_SPXBHMAudioVoiceStartCount",
        "_g_SPXBHMAudioLipActiveCount",
        "_g_SPXBHMAudioLastEntChan",
        "_g_SPXBHMAudioLastHandle",
        "_g_SPXBHMAudioListenerUpdateMask",
        "_g_SPXBPerfServerTicks",
        "_g_SPXBPerfServerLastGameMsec",
        "_g_SPXBPerfServerMaxGameMsec",
        "_g_SPXBPerfGamePreMsec",
        "_g_SPXBPerfGameEntitiesMsec",
        "_g_SPXBPerfGamePostMsec",
        "_g_SPXBPerfGameEntitiesVisited",
        "_g_SPXBPerfGameMissiles",
        "_g_SPXBPerfGameItems",
        "_g_SPXBPerfGameMovers",
        "_g_SPXBPerfGameClients",
        "_g_SPXBPerfGameThinkDue",
        "_g_SPXBPerfGameScripted",
        "_g_SPXBPerfGameOther",
        "_g_SPXBPerfScreenDrawMsec",
        "_g_SPXBPerfEndFrameMsec",
        "_g_SPXBPerfRenderTotalMsec",
        "_g_SPXBPerfRenderSetupMsec",
        "_g_SPXBPerfRenderMarkLeavesMsec",
        "_g_SPXBPerfRenderWorldMsec",
        "_g_SPXBPerfRenderPolysMsec",
        "_g_SPXBPerfRenderProjectionMsec",
        "_g_SPXBPerfRenderEntitiesMsec",
        "_g_SPXBPerfRenderSortMsec",
        "_g_SPXBPerfRenderDebugMsec",
        "_g_SPXBPerfRenderViews",
        "_g_SPXBPerfRenderPortals",
        "_g_SPXBPerfRenderDrawSurfs",
        "_g_SPXBPerfRenderRefEntities",
        "_g_SPXBPerfRenderLeafs",
        "_g_SPXBPerfBackendSurfaces",
        "_g_SPXBPerfBackendVertexes",
        "_g_SPXBPerfBackendIndexes",
        "_g_SPXBPerfBackendTotalIndexes",
        "_g_SPXBPerfFinishMsec",
        "_g_SPXBPerfPresentMsec",
        "_g_SPXBPerfBackendBatches",
        "_g_SPXBPerfSubmitCalls",
        "_g_SPXBPerfDrawCycles",
        "_g_SPXBPerfDrawStateCycles",
        "_g_SPXBPerfDrawReserveCycles",
        "_g_SPXBPerfDrawPackCycles",
        "_g_SPXBPerfDrawIndexCycles",
        "_g_SPXBPerfDrawSubmitCycles",
        "_g_SPXBPerfIndexedSubmitCalls",
        "_g_SPXBPerfImmediateSubmitCalls",
        "_g_SPXBPerfIndexedTex1Calls",
        "_g_SPXBPerfIndexedReserveDwords",
        "_g_SPXBPerfImmediateReserveDwords",
        "_g_SPXBPerfDrawBeginPushMaxCyclesCurrent",
        "_g_SPXBPerfDrawBeginPushMaxDwordsCurrent",
        "_g_SPXBPerfDrawBeginPushOver100KCurrent",
        "_g_SPXBPerfDrawBeginPushOver1MsecCurrent",
        "_g_SPXBPerfDrawBeginPushOver10MsecCurrent",
        "_g_SPXBPerfDrawBeginPushMaxStateCurrent",
        "_g_SPXBPerfDrawSetStreamCycles",
        "_g_SPXBPerfDrawBeginPushCycles",
        "_g_SPXBPerfDrawPointerCycles",
        "_g_SPXBPerfIndexedOpaqueCallsCurrent",
        "_g_SPXBPerfIndexedBlendCallsCurrent",
        "_g_SPXBPerfIndexedAlphaTestCallsCurrent",
        "_g_SPXBPerfIndexedNoDepthWriteCallsCurrent",
        "_g_SPXBPerfIndexedNoDepthTestCallsCurrent",
        "_g_SPXBPerfIndexedTwoSidedCallsCurrent",
        "_g_SPXBPerfIndexedBlendIndexesCurrent",
        "_g_SPXBPerfIndexedAlphaTestIndexesCurrent",
        "_g_SPXBPerfIndexedNoDepthWriteIndexesCurrent",
        "_g_SPXBPerfIndexedTwoSidedIndexesCurrent",
        "_g_SPXBPerfSampleActive",
        "_g_SPXBPerfSampleSerial",
        "_g_SPXBCameraActive",
        "_g_SPXBBorgPluggedCount",
        "_g_SPXBBorgPluggedEnt",
        "_g_SPXBBorgPluggedSpawnflags",
        "_g_SPXBBorgPluggedAnim",
        "_g_SPXBBorgPluggedLegsModel",
        "_g_SPXBBorgPluggedTorsoModel",
        "_g_SPXBBorgPluggedHeadModel",
        "_g_SPXBBorgPluggedLegsSkin",
        "_g_SPXBBorgPluggedTorsoSkin",
        "_g_SPXBBorgPluggedHeadSkin",
        "_g_SPXBBorgPluggedLegsNameHash",
        "_g_SPXBBorgPluggedTorsoNameHash",
        "_g_SPXBBorgPluggedHeadNameHash",
        "_g_SPXBBorgActiveCount",
        "_g_SPXBBorgActiveEnt",
        "_g_SPXBBorgActiveSpawnflags",
        "_g_SPXBBorgActiveAnim",
        "_g_SPXBBorgActiveLegsModel",
        "_g_SPXBBorgActiveTorsoModel",
        "_g_SPXBBorgActiveHeadModel",
        "_g_SPXBBorgActiveLegsSkin",
        "_g_SPXBBorgActiveTorsoSkin",
        "_g_SPXBBorgActiveHeadSkin",
        "_g_SPXBBorgActiveLegsNameHash",
        "_g_SPXBBorgActiveTorsoNameHash",
        "_g_SPXBBorgActiveHeadNameHash",
        "_g_SPXBRenderBackendMsec",
        "_g_SPXBFakeGLPrimitiveCalls",
        "_g_SPXBFakeGLPrimitiveVerts",
        "_g_SPXBFakeGLStateFlushes",
        "_g_SPXBNativeDrawMode",
        "_g_SPXBNativeDrawCount",
        "_g_SPXBNativeDrawSourceVertices",
        "_g_SPXBNativeDrawMaxIndex",
        "_g_SPXBNativeDrawStride",
        "_g_SPXBNativeDrawIndicesPtr",
        "_g_SPXBNativeDrawVerticesPtr",
        "_g_SPXBNativeDrawPath",
        "_g_SPXBNativeDrawShader",
        "_g_SPXBNativeDrawVertexOffset",
        "_g_SPXBNativeDrawIndexOffset",
        "_g_SPXBNativeDrawVertexBytes",
        "_g_SPXBNativeDrawIndexBytes",
        "_g_SPXBNativeDrawLockFlags",
        "_g_SPXBNativeDrawMinIndex",
        "_g_SPXBNativeMultiTexAttempts",
        "_g_SPXBNativeMultiTexDraws",
        "_g_SPXBNativeMultiTexReady",
        "_g_SPXBNativeMultiTexMismatch",
        "_g_SPXBNativeIndexedDrawFailures",
        "_g_SPXBNativeStage1Applies",
        "_g_SPXBNativeStage1ApplyFailures",
        "_g_SPXBLightmapMultiTexDraws",
        "_g_SPXBLightmapBundle0Draws",
        "_g_SPXBLightmapBundle1Draws",
        "_g_SPXBLightmapLastEnv",
        "_g_SPXBLightmapLastTex0",
        "_g_SPXBLightmapLastTex1",
        "_g_SPXBLightmapLastFlags",
        "_g_SPXBLightmapUploadCount",
        "_g_SPXBLightmapUploadSourceMinMax",
        "_g_SPXBLightmapUploadSourceAvg",
        "_g_SPXBLightmapUploadEncodedMinMax",
        "_g_SPXBLightmapUploadEncodedAvg",
        "_g_SPXBLightmapUploadChecksum",
        "_g_SPXBLightmapUploadFormat",
        "_g_SPXBLightmapUploadSize",
        "_g_SPXBRenderSplitShader",
        "_g_SPXBRenderSplitFog",
        "_g_SPXBRenderSplitDlight",
        "_g_SPXBRenderSplitEntity",
        "_g_SPXBRenderSplitFinal",
        "_g_SPXBRenderSplitFlush",
        "_g_SPXBHeartbeatMemUsed",
        "_g_SPXBHeartbeatMemFree",
        "_g_SPXBHeartbeatMemLargest",
        "_g_SPXBHeartbeatMemBlocks",
        "_g_SPXBHMSplitProofMagic",
        "_g_SPXBHMSplitLaunch",
        "_g_SPXBHMSplitBotProof",
        "_g_SPXBHMSplitStateSerial",
        "_g_SPXBHMSplitStatePlayers",
        "_g_SPXBHMSplitStateBots",
        "_g_SPXBHMSplitStateClientState",
        "_g_SPXBHMSplitStateFlags",
        "_g_SPXBHMSplitStateHealth",
        "_g_SPXBHMSplitStateWeapon",
        "_g_SPXBHMSplitStateP1Dist",
        "_g_SPXBHMSplitStateOriginX",
        "_g_SPXBHMSplitStateOriginY",
        "_g_SPXBHMSplitStateOriginZ",
        "_g_SPXBHMSplitStateViewPitch",
        "_g_SPXBHMSplitStateViewYaw",
        "_g_SPXBHMSplitStateViewRoll",
        "_g_SPXBHMSplitStateTime",
        "_g_SPXBHMSplitCollision",
        "_g_SPXBHMSplitCmdSerial",
        "_g_SPXBHMSplitCmdTime",
        "_g_SPXBHMSplitCmdMoveX",
        "_g_SPXBHMSplitCmdMoveY",
        "_g_SPXBHMSplitCmdMoveZ",
        "_g_SPXBHMSplitCmdButtons",
        "_g_SPXBHMSplitCmdWeapon",
        "_g_SPXBHMSplitCmdAnglePitch",
        "_g_SPXBHMSplitCmdAngleYaw",
        "_g_SPXBHMSplitCmdAngleRoll",
        "_g_SPXBHMSplitRefdefSerial",
        "_g_SPXBHMSplitRefdefX",
        "_g_SPXBHMSplitRefdefY",
        "_g_SPXBHMSplitRefdefZ",
        "_g_SPXBHMSplitRefdefPitch",
        "_g_SPXBHMSplitRefdefYaw",
        "_g_SPXBHMSplitRefdefRoll",
        "_g_SPXBHMSplitSnapshotSerial",
        "_g_SPXBHMSplitSnapshotBefore",
        "_g_SPXBHMSplitSnapshotAfter",
        "_g_SPXBHMSplitSnapshotAdded",
        "_g_SPXBHMSplitRenderSerial",
        "_g_SPXBHMSplitRenderArmedPlayers",
        "_g_SPXBHMSplitRenderExternal",
        "_g_SPXBHMSplitRenderClient",
        "_g_SPXBHMSplitRenderRectX",
        "_g_SPXBHMSplitRenderRectY",
        "_g_SPXBHMSplitRenderRectW",
        "_g_SPXBHMSplitRenderRectH",
        "_g_SPXBHMSplitRenderViewX",
        "_g_SPXBHMSplitRenderViewY",
        "_g_SPXBHMSplitRenderViewZ",
        "_g_SPXBHMSplitRenderDoneSerial",
        "_g_SPXBHMSplitRenderDrawDelta",
        "_g_SPXBHMSplitRenderDrawAfter",
        "_g_SPXBHMSplitRenderCluster",
        "_g_SPXBHMSplitHudSerial",
        "_g_SPXBHMSplitHudRectX",
        "_g_SPXBHMSplitHudRectY",
        "_g_SPXBHMSplitHudRectW",
        "_g_SPXBHMSplitHudRectH",
        "_g_SPXBHMSplitHudStatusSerial",
        "_g_SPXBHMSplitHudStatusValid",
        "_g_SPXBHMSplitHudStatusHealth",
        "_g_SPXBHMSplitHudStatusWeapon",
        "_g_SPXBHMSplitHudStatusScore",
        "_g_SPXBHMSplitHudStatusRectX",
        "_g_SPXBHMSplitHudStatusRectY",
        "_g_SPXBHMSplitHudStatusRectW",
        "_g_SPXBHMSplitHudStatusRectH",
        "_g_SPXBHMSplitHudDividerSerial",
        "_g_SPXBHMSplitFPFilterMask",
        "_g_SPXBHMSplitSelfFilterMask",
        "_g_SPXBHMSplitSelfFilterRefNumber",
        "_g_SPXBHMSplitSelfFilterPart",
        "_g_SPXBHMSplitViewWeaponSerial",
        "_g_SPXBHMSplitViewWeaponAdded",
        "_g_SPXBHMSplitViewWeaponRenderfx",
        "_g_SPXBHMSplitViewWeaponClient",
        "_g_SPXBHMSplitViewWeaponWeapon",
        "_g_SPXBHMSplitPhaserWorldHidden",
        "_g_SPXBHMSplitPhaserBridgeFP",
        "_g_SPXBHMSplitPhaserBridgeWorld",
        "_g_SPXBHMSplitPhaserBridgeLineFP",
        "_g_SPXBHMSplitPhaserBridgeLastNumber",
        "_g_SPXBHMSplitPhaserFPSerial",
        "_g_SPXBHMSplitPhaserFPRenderfx",
        "_g_SPXBHMSplitPhaserFPStartX",
        "_g_SPXBHMSplitPhaserFPStartY",
        "_g_SPXBHMSplitPhaserFPStartZ",
        "_g_SPXBHMSplitPhaserFPViewX",
        "_g_SPXBHMSplitPhaserFPViewY",
        "_g_SPXBHMSplitPhaserFPViewZ",
        "_g_SPXBSplitSlotActive",
        "_g_SPXBSplitSlot0DrawDelta",
        "_g_SPXBSplitSlot1DrawDelta",
        "_g_SPXBSplitSlot0WorldDelta",
        "_g_SPXBSplitSlot1WorldDelta",
        "_g_SPXBSplitSlot0Cluster",
        "_g_SPXBSplitSlot1Cluster",
        "_g_SPXBSplitSlot1WorldRetryDelta",
        "_g_SPXBSplitSlot1WorldFallback",
        "_g_SPXBSplitSlot0MarkedLeaves",
        "_g_SPXBSplitSlot1MarkedLeaves",
        "_g_SPXBSplitSlot0PvsRejected",
        "_g_SPXBSplitSlot1PvsRejected",
        "_g_SPXBSplitSlot0AreaRejected",
        "_g_SPXBSplitSlot1AreaRejected",
        "_g_SPXBSplitSlot0RootVis",
        "_g_SPXBSplitSlot1RootVis",
        "_g_SPXBSplitSlot0WorldAttempts",
        "_g_SPXBSplitSlot1WorldAttempts",
        "_g_SPXBSplitSlot0WorldCulled",
        "_g_SPXBSplitSlot1WorldCulled",
        "_g_SPXBSplitSlot0WorldAlready",
        "_g_SPXBSplitSlot1WorldAlready",
        "_g_SPXBSplitSlot0WorldAdded",
        "_g_SPXBSplitSlot1WorldAdded",
        "_g_SPXBSplitP2Ent",
        "_g_SPXBSplitP2TraceFrac1000",
        "_g_SPXBSplitP2ViewX",
        "_g_SPXBSplitP2ViewY",
        "_g_SPXBSplitP2ViewZ",
        "_g_SPXBSplitP2PsX",
        "_g_SPXBSplitP2PsY",
        "_g_SPXBSplitP2PsZ",
        "_g_SPXBSplitP2CurX",
        "_g_SPXBSplitP2CurY",
        "_g_SPXBSplitP2CurZ",
        "_g_SPXBSplitP2AnglesPitch",
        "_g_SPXBSplitP2AnglesYaw",
        "_g_SPXBSplitP2RefdefValid",
        "_g_SPXBSplitP2SceneConsidered",
        "_g_SPXBSplitP2SceneAdded",
        "_g_SPXBSplitP2SceneSelfAdded",
        "_g_SPXBSplitP2ModelEnter",
        "_g_SPXBSplitP2ModelReturn",
        "_g_SPXBSplitP2ModelInfoValid",
        "_g_SPXBSplitP2ModelSubmitted",
        "_g_SPXBSplitP2ModelLegs",
        "_g_SPXBSplitP2ModelTorso",
        "_g_SPXBSplitP2ModelHead",
        "_g_SPXBSplitP2ModelRenderfx",
        "_g_SPXBSplitP2RendererRefs",
        "_g_SPXBSplitP2RendererLastModel",
        "_g_SPXBSplitP2RendererLastRenderfx",
        "_g_SPXBSplitP2RendererLastZ",
        "_g_SPXBViewWeaponP1Adds",
        "_g_SPXBViewWeaponP2Adds",
        "_g_SPXBViewWeaponP1Skips",
        "_g_SPXBViewWeaponP2Skips",
        "_g_SPXBViewWeaponP1Model",
        "_g_SPXBViewWeaponP2Model",
        "_g_SPXBViewWeaponP1Renderfx",
        "_g_SPXBViewWeaponP2Renderfx",
        "_g_SPXBViewWeaponP1RendererAdds",
        "_g_SPXBViewWeaponP2RendererAdds",
        "_g_SPXBViewWeaponP1RendererFiltered",
        "_g_SPXBViewWeaponP2RendererFiltered",
        "_g_SPXBViewWeaponP1LastSkip",
        "_g_SPXBViewWeaponP2LastSkip",
        "_g_SPXBWeaponRegWeapon",
        "_g_SPXBWeaponRegPathHash",
        "_g_SPXBWeaponRegViewModel",
        "_g_SPXBWeaponRegWorldModel",
        "_g_SPXBWeaponRegHandsModel",
        "_g_SPXBWeaponRegFailCode",
        "_g_SPXBWeaponModelTraceStage",
        "_g_SPXBWeaponModelTracePathHash",
        "_g_SPXBWeaponModelTraceDiskLen",
        "_g_SPXBWeaponModelTraceDiskSuccess",
        "_g_SPXBWeaponModelTraceIdent",
        "_g_SPXBWeaponModelTraceVersion",
        "_g_SPXBWeaponModelTraceSize",
        "_g_SPXBWeaponModelTraceLoaded",
        "_g_SPXBWeaponModelTraceHandle",
        "_g_SPXBWeaponModelTraceFailCode",
        "_g_SPXBModelProbeStage",
        "_g_SPXBModelProbePathHash",
        "_g_SPXBModelProbeNamePtr",
        "_g_SPXBModelProbeFileLen",
        "_g_SPXBFileAllocStage",
        "_g_SPXBFileAllocPathHash",
        "_g_SPXBFileAllocPathPtr",
        "_g_SPXBFileAllocLength",
        "_g_SPXBFileAllocTag",
        "_g_SPXBWeaponLoadStage",
        "_g_SPXBWeaponLoadReadLen",
        "_g_SPXBWeaponLoadTypeWeapon",
        "_g_SPXBWeaponLoadModelWeapon",
        "_g_SPXBWeaponLoadModelHash",
        "_g_SPXBWeaponLoadSlot4Hash",
        "_g_SPXBWeaponLoadSlot4Ammo",
        "_g_SPXBWeaponLoadSlot4First4",
        "_g_SPXBWeaponRegFirst4",
        "_g_SPXBWeaponRegClassHash",
        "_g_SPXBSplitCameraMode",
        "_g_SPXBSplitP1TraceFrac1000",
        "_g_SPXBSplitP1LocalX1000",
        "_g_SPXBSplitP1LocalY1000",
        "_g_SPXBSplitP1LocalZ1000",
        "_g_SPXBSplitP2LocalX1000",
        "_g_SPXBSplitP2LocalY1000",
        "_g_SPXBSplitP2LocalZ1000",
        "_g_SPXBSplitLocalDiffX1000",
        "_g_SPXBSplitLocalDiffY1000",
        "_g_SPXBSplitLocalDiffZ1000",
        "_g_SPXBDirectMapStatus",
        "_g_SPXBDirectMapHash",
        "_g_SPXBDirectMapQueuedCount",
        "_g_SPXBSkyTraceMagic",
        "_g_SPXBSkyOuterPresentMask",
        "_g_SPXBSkyOuterFallbackMask",
        "_g_SPXBSkyOuterTexMask",
        "_g_SPXBSkyOuterDrawMask",
        "_g_SPXBSkyLastPasses",
        "_g_SPXBSkyLastSort",
        "_g_SPXBSkyResolveMagic",
        "_g_SPXBSkyResolveCount",
        "_g_SPXBSkyResolveShaderNum",
        "_g_SPXBSkyResolveMapHash",
        "_g_SPXBSkyResolveResolvedHash",
        "_g_SPXBSkyResolveSurfaceFlags",
        "_g_SPXBSkyResolveDefault",
        "_g_SPXBSkyResolveExplicit",
        "_g_SPXBSkyResolveHasSky",
        "_g_SPXBSkyResolvePasses",
        "_g_SPXBSkyResolveSortX1000",
        "_g_SPXBSkyResolveLightmap0",
        "_g_SPXBShaderScanMagic",
        "_g_SPXBShaderScanScriptsFound",
        "_g_SPXBShaderScanShadersFound",
        "_g_SPXBShaderScanLoaded",
        "_g_SPXBShaderScanBytes",
        "_g_SPXBShaderScanEntries",
        "_g_SPXBShaderScanSkyLightSeen",
        "_g_SPXBShaderScanJunkSkySeen",
        "_g_SPXBShaderScanManifestActive",
        "_g_SPXBShaderScanManifestReadLen",
        "_g_SPXBShaderScanManifestCount",
        "_g_SPXBShaderScanRawBytes",
        "_g_SPXBShaderScanVoyagerListed",
        "_g_SPXBShaderScanVoyagerReadLen",
        "_g_SPXBShaderScanVoyagerSkyToken",
        "_g_SPXBShaderScanCommonReadLen",
        "_g_SPXBShaderLookupMagic",
        "_g_SPXBShaderLookupCount",
        "_g_SPXBShaderLookupHash",
        "_g_SPXBShaderLookupIndexedFound",
        "_g_SPXBShaderLookupLinearFound",
        "_g_SPXBShaderLookupEntries",
        "_g_SPXBHelmetP1Submitted",
        "_g_SPXBHelmetP2Submitted",
        "_g_SPXBHelmetP1Attached",
        "_g_SPXBHelmetP2Attached",
        "_g_SPXBHelmetP1Model",
        "_g_SPXBHelmetP2Model",
        "_g_SPXBHelmetP1Renderfx",
        "_g_SPXBHelmetP2Renderfx",
        "_g_SPXBHelmetRendererRefs",
        "_g_SPXBHelmetRendererSurfaces",
        "_g_SPXBHelmetRendererFiltered",
        "_g_SPXBHelmetRendererLastModel",
        "_g_SPXBHelmetRendererLastRenderfx",
        "_g_SPXBHelmetRendererLastEnt",
        "_g_SPXBHelmetRendererLastFilter",
        "_g_SPXBHelmetRendererLastSurfaceModel",
        "_g_SPXBHelmetGameP1Ensure",
        "_g_SPXBHelmetGameP2Ensure",
        "_g_SPXBHelmetGameP1Slot",
        "_g_SPXBHelmetGameP2Slot",
        "_g_SPXBHelmetCgameP1Slot0",
        "_g_SPXBHelmetCgameP1Slot1",
        "_g_SPXBHelmetCgameP2Slot0",
        "_g_SPXBHelmetCgameP2Slot1",
        "_g_SPXBHelmetBoltOnLoadLen",
        "_g_SPXBHelmetBoltOnCount",
        "_g_SPXBHelmetBoltOnHelmetIndex",
        "_g_SPXBHelmetAddAttempts",
        "_g_SPXBHelmetAddKnownIndex",
        "_g_SPXBHelmetAddFailCode",
        "_g_SPXBFallbackTraceMagic",
        "_g_SPXBFallbackStageCount",
        "_g_SPXBFallbackLastShaderHash",
        "_g_SPXBFallbackLastImageHash",
        "_g_SPXBFallbackLastStage",
        "_g_SPXBFallbackLastPasses",
        "_g_SPXBFallbackLastFlags",
        "_g_SPXBFallbackLastTexnum",
        "_g_SPXBFallbackLastLightmap",
        "_g_SPXBFallbackLastStateBits",
        "_g_SPXBFallbackLastIndexes",
        "_g_SPXBFallbackLastX1000",
        "_g_SPXBFallbackLastY1000",
        "_g_SPXBFallbackLastZ1000",
        "_g_SPXBSVProbeMagic",
        "_g_SPXBSVProbePhase",
        "_g_SPXBSVProbeSubphase",
        "_g_SPXBSVProbeA",
        "_g_SPXBSVProbeB",
        "_g_SPXBSVProbeC",
        "_g_SPXBSVProbeD",
        "_g_SPXBLoadingTitleMagic",
        "_g_SPXBLoadingTitleStatus",
        "_g_SPXBLoadingTitleShader",
        "_g_SPXBLoadingTitleDraws",
        "_g_SPXBLoadingTitleLastChar",
        "_g_SPXBLoadingTitleMapHash",
        "_g_SPXBLoadingTitleTextHash",
    ])
    xblog_symbol_names = list(xblog_offsets.keys())
    xblog_font_symbols = [
        "_g_SPXBUIFontLoadAttempts",
        "_g_SPXBUIFontFileLen",
        "_g_SPXBUIFontLoaded",
        "_g_SPXBUIFontTinyShader",
        "_g_SPXBUIFontMediumShader",
        "_g_SPXBUIFontBigShader",
        "_g_SPXBUIFontDrawCalls",
        "_g_SPXBUIFontDrawRejectMask",
        "_g_SPXBUIFontLastChar",
        "_g_SPXBUIFontLastShader",
        "_g_SPXBUIFontLastSX",
        "_g_SPXBUIFontLastSY",
        "_g_SPXBUIFontLastSW",
    ]
    xblog_font_base_va, xblog_font_map, xblog_font_linked_va = \
        resolve_runtime_map_symbol(xblog_font_symbols[0])
    xblog_font_offsets = resolve_symbol_offsets(xblog_font_symbols[0], xblog_font_symbols)
    xblog_title_symbols = [
        "_g_SPXBLoadingTitleMagic",
        "_g_SPXBLoadingTitleStatus",
        "_g_SPXBLoadingTitleShader",
        "_g_SPXBLoadingTitleDraws",
        "_g_SPXBLoadingTitleLastChar",
        "_g_SPXBLoadingTitleMapHash",
        "_g_SPXBLoadingTitleTextHash",
    ]
    xblog_title_base_va, _xblog_title_map, xblog_title_linked_va = \
        resolve_runtime_map_symbol(xblog_title_symbols[0])
    xblog_title_offsets = resolve_symbol_offsets(
        xblog_title_symbols[0], xblog_title_symbols)
    xblog_current_personality = "efmp" if (
        xblog_map and os.path.basename(xblog_map).lower() == "efmp.map") else "default"
    next_personality_probe = 0.0
    if xblog_font_base_va is not None:
        log("xblog_font_symbol=%s linked=0x%08x va=0x%08x map=%s" %
            (xblog_font_symbols[0], xblog_font_linked_va or 0,
             xblog_font_base_va, xblog_font_map))
    mini_soak_probes = {}
    for personality, map_path in (
            ("default", os.path.join("build", "release", "default.map")),
            ("efmp", os.path.join("build", "release", "efmp.map"))):
        mini_va = resolve_runtime_map_symbol_in(
            "_g_SPXBMiniSoakMagic", map_path)
        if mini_va is not None:
            mini_soak_probes[personality] = {
                "va": mini_va,
                "map": map_path,
                "addr": None,
                "next_probe": 0.0,
            }
            log("xblog_soak_probe personality=%s va=0x%08x map=%s" %
                (personality, mini_va, map_path))
    capture_enabled = (not args.no_screenshots) and display_mode not in ("none", "egl-headless", "nographic")

    sock = None
    try:
        if not args.no_monitor:
            sock = monitor_connect(args.port)
            log("monitor=ready port=%d" % args.port)
            if args.poll_xblog and xblog_va_for_probe is not None:
                if xblog_cmd == "x":
                    probed_addr = probe_xblog_virtual_addr(
                        sock, xblog_va_for_probe, log)
                else:
                    probed_addr = probe_xblog_physical_addr(
                        sock, xblog_va_for_probe,
                        int(args.poll_xblog_phys_delta, 0), log)
                if probed_addr is not None:
                    xblog_addr = probed_addr
                    if xblog_auto_phys_pending:
                        probed_delta = xblog_va_for_probe - xblog_addr
                        args.dump_phys = append_xblog_auto_dumps(args.dump_phys, probed_delta, log)
                        xblog_auto_phys_pending = False
                else:
                    # Normal frontend boot can map the title after the monitor
                    # connection is ready. Retry from the main loop instead of
                    # polling the preferred address forever.
                    xblog_addr = None
                    next_xblog_probe = max(1.0, float(args.interval))
        else:
            log("monitor=disabled")
        start = time.time()
        next_shot = max(0.0, float(args.first_shot_delay))
        next_eip_sample = 0.0
        shot = 0
        while time.time() - start < args.duration:
            elapsed = time.time() - start
            rc = proc.poll()
            if rc is not None:
                log("process_exit t=%.1f rc=%s" % (elapsed, rc))
                break

            if (sock is not None and args.sample_eip_interval > 0 and
                    elapsed >= next_eip_sample):
                next_eip_sample = elapsed + max(0.1, args.sample_eip_interval)
                regs = monitor_cmd(sock, "info registers", 0.05)
                m = re.search(r"EIP=([0-9a-fA-F]{8})", regs)
                if m:
                    log("eipsample t=%.2f eip=0x%08x" %
                        (elapsed, int(m.group(1), 16)))

            if (sock is not None and poll_word_va is not None and
                    elapsed >= next_poll_word):
                next_poll_word = elapsed + max(0.05, args.poll_word_interval)
                try:
                    if poll_word_gpa is None:
                        poll_word_gpa = monitor_gva_to_gpa(sock, poll_word_va)
                        if poll_word_gpa is not None:
                            log("word_counter_mapped label=%s va=0x%08x gpa=0x%08x" %
                                (args.poll_word_label, poll_word_va,
                                 poll_word_gpa))
                    if poll_word_gpa is not None:
                        poll_word_count = max(1, min(512, args.poll_word_count))
                        reply = monitor_cmd(
                            sock, "xp/%dwx 0x%08x" %
                            (poll_word_count, poll_word_gpa), 0.15)
                        words = parse_monitor_words(reply, poll_word_gpa)
                        if words:
                            if poll_word_count > 1:
                                values = words[:poll_word_count]
                                log("word_snapshot t=%.2f label=%s va=0x%08x values=%s" %
                                    (elapsed, args.poll_word_label, poll_word_va,
                                     ",".join(str(value) for value in values)))
                            else:
                                value = words[0]
                                if poll_word_previous is not None:
                                    previous_elapsed, previous_value = poll_word_previous
                                    wall_seconds = elapsed - previous_elapsed
                                    delta = (value - previous_value) & 0xffffffff
                                    rate = (delta / wall_seconds
                                            if wall_seconds > 0.0 else 0.0)
                                    log("word_counter t=%.2f label=%s va=0x%08x value=%u delta=%u wall=%.3f rate=%.2f" %
                                        (elapsed, args.poll_word_label, poll_word_va,
                                         value, delta, wall_seconds, rate))
                                    if 0.0 < rate < 10000.0:
                                        poll_word_rates.append(rate)
                                else:
                                    log("word_counter t=%.2f label=%s va=0x%08x value=%u baseline=1" %
                                        (elapsed, args.poll_word_label,
                                         poll_word_va, value))
                                poll_word_previous = (elapsed, value)
                except OSError as exc:
                    log("word_counter_unavailable label=%s va=0x%08x err=%s" %
                        (args.poll_word_label, poll_word_va, exc))
                    poll_word_gpa = None

            if sock is not None and watch_cr2 is not None and not watch_done and elapsed >= next_watch:
                next_watch = elapsed + 0.08
                regs = monitor_cmd(sock, "info registers", 0.05)
                m = re.search(r"CR2=([0-9a-fA-F]{8})", regs)
                if m and int(m.group(1), 16) == watch_cr2:
                    log("watch_cr2_hit t=%.2f cr2=0x%08x" % (elapsed, watch_cr2))
                    watch_regs_path = os.path.abspath("%s_watch_cr2_registers.txt" % prefix)
                    with open(watch_regs_path, "w", encoding="utf-8", errors="replace") as f:
                        f.write(regs)
                    log("watch_cr2_registers=%s" % watch_regs_path)
                    dump_monitor_state(sock, prefix, "watch_cr2", args.dump_mem, log)
                    watch_done = True

            if (sock is not None and args.poll_xblog and
                    xblog_va_for_probe is not None and xblog_addr is None and
                    elapsed >= next_xblog_probe):
                next_xblog_probe = elapsed + max(1.0, float(args.interval))
                if xblog_cmd == "x":
                    probed_addr = probe_xblog_virtual_addr(
                        sock, xblog_va_for_probe, log)
                else:
                    probed_addr = probe_xblog_physical_addr(
                        sock, xblog_va_for_probe,
                        int(args.poll_xblog_phys_delta, 0), log)
                if probed_addr is not None:
                    xblog_addr = probed_addr
                    next_xblog_poll = max(elapsed, args.poll_xblog_start_delay)
                    if xblog_auto_phys_pending:
                        probed_delta = xblog_va_for_probe - xblog_addr
                        args.dump_phys = append_xblog_auto_dumps(
                            args.dump_phys, probed_delta, log)
                        xblog_auto_phys_pending = False
                elif elapsed >= next_personality_probe:
                    next_personality_probe = elapsed + max(
                        2.0, float(args.interval))
                    alternate_personality = (
                        "efmp" if xblog_current_personality == "default" else "default")
                    alternate_map = os.path.join(
                        "build", "release", alternate_personality + ".map")
                    alternate_va = resolve_runtime_map_symbol_in(
                        "_g_SPXBBootPhase", alternate_map)
                    alternate_addr = None
                    if alternate_va is not None:
                        if xblog_cmd == "x":
                            alternate_addr = probe_xblog_virtual_addr(
                                sock, alternate_va, log)
                        else:
                            alternate_addr = probe_xblog_physical_addr(
                                sock, alternate_va,
                                int(args.poll_xblog_phys_delta, 0), log)
                    if alternate_addr is not None:
                        xblog_addr = alternate_addr
                        xblog_va_for_probe = alternate_va
                        xblog_offsets = resolve_symbol_offsets_in(
                            "_g_SPXBBootPhase", xblog_symbol_names,
                            alternate_map)
                        xblog_font_base_va = resolve_runtime_map_symbol_in(
                            xblog_font_symbols[0], alternate_map)
                        xblog_font_offsets = resolve_symbol_offsets_in(
                            xblog_font_symbols[0], xblog_font_symbols,
                            alternate_map)
                        xblog_title_base_va = resolve_runtime_map_symbol_in(
                            xblog_title_symbols[0], alternate_map)
                        xblog_title_offsets = resolve_symbol_offsets_in(
                            xblog_title_symbols[0], xblog_title_symbols,
                            alternate_map)
                        texture_allocator_map = alternate_map
                        texture_allocator_va = resolve_literal_map_symbol_in(
                            texture_allocator_symbol, texture_allocator_map)
                        texture_allocator_runtime_va = (
                            linked_pe_va_to_xbe_va(
                                texture_allocator_va, texture_allocator_map)
                            if texture_allocator_va is not None else None)
                        texture_allocator_gpa = None
                        xblog_current_personality = alternate_personality
                        last_fps_heartbeat_count = None
                        next_xblog_poll = max(
                            elapsed, args.poll_xblog_start_delay)
                        if xblog_auto_phys_pending:
                            probed_delta = xblog_va_for_probe - xblog_addr
                            args.dump_phys = append_xblog_auto_dumps(
                                args.dump_phys, probed_delta, log)
                            xblog_auto_phys_pending = False
                        log("xblog_personality_late_attach t=%.1f personality=%s "
                            "va=0x%08x phys=0x%08x delta=0x%x map=%s" %
                            (elapsed, alternate_personality, alternate_va,
                             alternate_addr,
                             alternate_va - alternate_addr, alternate_map))

            if sock is not None and xblog_addr is not None and elapsed >= next_xblog_poll:
                next_xblog_poll = elapsed + max(0.2, args.poll_xblog_interval)
                try:
                    runtime_delta = int(args.poll_xblog_phys_delta, 0)
                    if xblog_va_for_probe is not None and xblog_addr is not None:
                        runtime_delta = xblog_va_for_probe - xblog_addr
                    header_reply = monitor_cmd(
                        sock, "%s/9wx 0x%08x" % (xblog_cmd, xblog_addr), 0.3)
                    header_words = parse_monitor_words(header_reply, xblog_addr)
                    if ((len(header_words) < 4 or
                         header_words[3] != 0x48424653) and
                            elapsed >= next_personality_probe):
                        next_personality_probe = elapsed + max(
                            2.0, float(args.interval))
                        default_map = os.path.join(
                            "build", "release", "default.map")
                        efmp_map = os.path.join(
                            "build", "release", "efmp.map")
                        personality_candidates = [
                            ("default",
                             resolve_runtime_map_symbol_in(
                                 "_g_SPXBBootPhase", default_map),
                             default_map),
                            ("efmp",
                             resolve_runtime_map_symbol_in(
                                 "_g_SPXBBootPhase", efmp_map),
                             efmp_map),
                        ]
                        if xblog_current_personality == "default":
                            personality_candidates.reverse()
                        for personality, boot_va, personality_map in personality_candidates:
                            if boot_va is None:
                                continue
                            if xblog_cmd == "x":
                                probed_addr = probe_xblog_virtual_addr(
                                    sock, boot_va, log)
                            else:
                                probed_addr = probe_xblog_physical_addr(
                                    sock, boot_va, runtime_delta, log)
                            if probed_addr is None:
                                continue
                            xblog_addr = probed_addr
                            xblog_va_for_probe = boot_va
                            xblog_offsets = resolve_symbol_offsets_in(
                                "_g_SPXBBootPhase", xblog_symbol_names,
                                personality_map)
                            xblog_font_base_va = resolve_runtime_map_symbol_in(
                                xblog_font_symbols[0], personality_map)
                            xblog_font_offsets = resolve_symbol_offsets_in(
                                xblog_font_symbols[0], xblog_font_symbols,
                                personality_map)
                            xblog_title_base_va = resolve_runtime_map_symbol_in(
                                xblog_title_symbols[0], personality_map)
                            xblog_title_offsets = resolve_symbol_offsets_in(
                                xblog_title_symbols[0], xblog_title_symbols,
                                personality_map)
                            texture_allocator_map = personality_map
                            texture_allocator_va = resolve_literal_map_symbol_in(
                                texture_allocator_symbol,
                                texture_allocator_map)
                            texture_allocator_runtime_va = (
                                linked_pe_va_to_xbe_va(
                                    texture_allocator_va,
                                    texture_allocator_map)
                                if texture_allocator_va is not None else None)
                            texture_allocator_gpa = None
                            xblog_current_personality = personality
                            last_fps_heartbeat_count = None
                            runtime_delta = xblog_va_for_probe - xblog_addr
                            log("xblog_personality_switch t=%.1f personality=%s "
                                "va=0x%08x phys=0x%08x delta=0x%x map=%s" %
                                (elapsed, personality, boot_va, xblog_addr,
                                 runtime_delta, personality_map))
                            break
                    mini_probe = mini_soak_probes.get(
                        xblog_current_personality)
                    if mini_probe is not None:
                        if (mini_probe["addr"] is None and
                                elapsed >= mini_probe["next_probe"]):
                            mini_probe["next_probe"] = elapsed + max(
                                5.0, args.poll_xblog_interval)
                            mini_probe["addr"] = probe_magic_runtime_addr(
                                sock, mini_probe["va"], 0x4D534F4B,
                                xblog_cmd, runtime_delta, log,
                                "xblogsoak_%s" % xblog_current_personality)
                        mini_addr = mini_probe["addr"]
                        if mini_addr is not None:
                            mini_reply = monitor_cmd(
                                sock, "%s/5wx 0x%08x" %
                                (xblog_cmd, mini_addr), 0.25)
                            mini_words = parse_monitor_words(
                                mini_reply, mini_addr)
                            if (len(mini_words) >= 5 and
                                    mini_words[0] == 0x4D534F4B):
                                log("xblogsoak-live t=%.1f personality=%s stage=%u transitions=%u activeMsec=%u flags=0x%08x" %
                                    (elapsed, xblog_current_personality,
                                     mini_words[1], mini_words[2],
                                     mini_words[3], mini_words[4]))
                            else:
                                mini_probe["addr"] = None
                    if xblog_mode == "mp":
                        reply = monitor_cmd(sock, "%s/1wx 0x%08x" % (xblog_cmd, xblog_mp_pos_addr), 0.4)
                        words = parse_monitor_words(reply, xblog_mp_pos_addr)
                        reply_write = monitor_cmd(sock, "%s/1wx 0x%08x" % (xblog_cmd, xblog_mp_write_addr), 0.4)
                        write_words = parse_monitor_words(reply_write, xblog_mp_write_addr)
                        if words and write_words:
                            mirror_pos = words[0]
                            write_count = write_words[0]
                            delta = 0 if last_xblog_write_count is None else write_count - last_xblog_write_count
                            last_xblog_write_count = write_count
                            log("xblog t=%.1f mp mirror=%u writes=%u delta=%d" %
                                (elapsed, mirror_pos, write_count, delta))
                        else:
                            debug_path = os.path.abspath("%s_xblog_%04d.txt" % (prefix, int(elapsed * 10)))
                            with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
                                f.write(reply)
                                f.write("\n--- write ---\n")
                                f.write(reply_write)
                            log("xblog t=%.1f mp unreadable raw=%s" % (elapsed, debug_path))
                        continue

                    if args.poll_xblog_perf_only:
                        perf_poll_symbols = ["_g_SPXBClsState"] + [
                            name for name in xblog_offsets
                            if name.startswith("_g_SPXBPerf")
                        ]
                        perf_poll_offsets = [
                            xblog_offsets[name] for name in perf_poll_symbols
                            if name in xblog_offsets
                        ]
                        poll_words = max(16, max(perf_poll_offsets or [0]) + 2)
                    else:
                        poll_words = max(64, max(xblog_offsets.values() or [0]) + 8)
                    reply = monitor_cmd(sock, "%s/%dwx 0x%08x" % (xblog_cmd, poll_words, xblog_addr), 0.4)
                    words = parse_monitor_words(reply, xblog_addr)
                    perf_aux_words = {}
                    # The writable telemetry pages may not retain the title's
                    # initial physical layout. Translate each page through the
                    # live guest tables before overriding the compact probe.
                    if (xblog_addr is not None and
                            xblog_va_for_probe is not None):
                        xblog_base_page = xblog_va_for_probe & ~0xFFF
                        hm_split_single_symbols = set([
                            "_g_SPXBHMSplitProofMagic",
                            "_g_SPXBHMSplitRenderArmedPlayers",
                            "_g_SPXBHMSplitHudDividerSerial",
                            "_g_SPXBHMSplitFPFilterMask",
                            "_g_SPXBHMSplitSelfFilterMask",
                        ])
                        hm_split_aux_symbols = [
                            name for name in xblog_offsets
                            if (name.startswith("_g_SPXBHMSplit") or
                                name.startswith("_g_SPXBHeartbeatMem"))
                        ]
                        hm_split_extra_words = {}
                        for name in hm_split_aux_symbols:
                            if name == "_g_SPXBHMSplitLaunch":
                                hm_split_extra_words[name] = 9
                            elif name == "_g_SPXBHMSplitBotProof":
                                hm_split_extra_words[name] = 32
                            elif name == "_g_SPXBHMSplitCollision":
                                hm_split_extra_words[name] = 48
                            elif (name.startswith("_g_SPXBHMSplit") and
                                  name not in hm_split_single_symbols):
                                hm_split_extra_words[name] = 4
                        if hm_split_aux_symbols:
                            perf_aux_words.update(
                                monitor_read_virtual_symbol_pages(
                                    sock, xblog_va_for_probe, xblog_offsets,
                                    hm_split_aux_symbols,
                                    hm_split_extra_words))
                        perf_virtual_symbols = [
                            name for name in xblog_offsets
                            if (name.startswith("_g_SPXBPerf") and
                                ((xblog_va_for_probe +
                                  xblog_offsets[name] * 4) & ~0xFFF) !=
                                xblog_base_page)
                        ]
                        if perf_virtual_symbols:
                            perf_aux_words.update(
                                monitor_read_virtual_symbol_pages(
                                    sock, xblog_va_for_probe, xblog_offsets,
                                    perf_virtual_symbols))
                    font_words = []
                    title_words = []
                    if (not args.poll_xblog_perf_only and
                            xblog_font_base_va is not None and
                            xblog_va_for_probe is not None):
                        runtime_delta = xblog_va_for_probe - xblog_addr
                        xblog_font_addr = (
                            xblog_font_base_va if xblog_cmd == "x" else
                            xblog_font_base_va - runtime_delta)
                        font_poll_words = max(16, max(xblog_font_offsets.values() or [0]) + 2)
                        font_reply = monitor_cmd(
                            sock,
                            "%s/%dwx 0x%08x" %
                            (xblog_cmd, font_poll_words, xblog_font_addr),
                            0.4)
                        font_words = parse_monitor_words(font_reply, xblog_font_addr)
                    if (not args.poll_xblog_perf_only and
                            xblog_title_base_va is not None and
                            xblog_va_for_probe is not None):
                        runtime_delta = xblog_va_for_probe - xblog_addr
                        xblog_title_addr = (
                            xblog_title_base_va if xblog_cmd == "x" else
                            xblog_title_base_va - runtime_delta)
                        title_poll_words = max(
                            7, max(xblog_title_offsets.values() or [0]) + 1)
                        title_reply = monitor_cmd(
                            sock,
                            "%s/%dwx 0x%08x" % (
                                xblog_cmd, title_poll_words, xblog_title_addr),
                            0.4)
                        title_words = parse_monitor_words(
                            title_reply, xblog_title_addr)
                    if (not args.poll_xblog_perf_only and
                            texture_allocator_runtime_va is not None and
                            elapsed >= next_texture_allocator_poll):
                        next_texture_allocator_poll = elapsed + max(
                            5.0, float(args.interval))
                        if texture_allocator_gpa is None:
                            texture_allocator_gpa = monitor_gva_to_gpa(
                                sock, texture_allocator_runtime_va)
                            if texture_allocator_gpa is not None:
                                log("texture_allocator_mapped va=0x%08x gpa=0x%08x" %
                                    (texture_allocator_runtime_va,
                                     texture_allocator_gpa))
                        texture_allocator = monitor_texture_allocator(
                            sock, texture_allocator_gpa, "xp") \
                            if texture_allocator_gpa is not None else None
                        if texture_allocator is None:
                            log("xblogtex t=%.1f unavailable va=0x%08x phys=0x%08x" %
                                (elapsed, texture_allocator_va,
                                 texture_allocator_gpa or 0))
                        else:
                            log("xblogtex t=%.1f base=0x%08x used=%u capacity=%u "
                                "high=%u blocks=%u totalFree=%u largestFree=%u "
                                "complete=%u" %
                                (elapsed, texture_allocator["base"],
                                 texture_allocator["used"],
                                 texture_allocator["capacity"],
                                 texture_allocator["high_water"],
                                 texture_allocator["block_count"],
                                 texture_allocator["total_free"],
                                 texture_allocator["largest_free"],
                                 1 if texture_allocator["complete"] else 0))
                    if len(words) >= 9:
                        telemetry_words = words
                        boot_phase = words[0]
                        mirror_pos = words[1]
                        write_count = words[2]
                        heartbeat_magic = words[3]
                        heartbeat_count = words[4]
                        heartbeat_frame = words[5]
                        heartbeat_rt = words[6]
                        heartbeat_st = words[7]
                        heartbeat_fps10 = words[8]
                        delta = 0 if last_xblog_write_count is None else write_count - last_xblog_write_count
                        last_xblog_write_count = write_count
                        def word_for(symbol):
                            if symbol in perf_aux_words:
                                return perf_aux_words[symbol]
                            idx = xblog_offsets.get(symbol)
                            if idx is None or idx >= len(telemetry_words):
                                return 0
                            return telemetry_words[idx]
                        def word_array(symbol, slot):
                            aux_key = (symbol, slot)
                            if aux_key in perf_aux_words:
                                return perf_aux_words[aux_key]
                            if slot == 0 and symbol in perf_aux_words:
                                return perf_aux_words[symbol]
                            idx = xblog_offsets.get(symbol)
                            if idx is None:
                                return 0
                            idx += slot
                            if idx < 0 or idx >= len(telemetry_words):
                                return 0
                            return telemetry_words[idx]
                        def signed32(value):
                            return value - 0x100000000 if value & 0x80000000 else value
                        def font_word_for(symbol):
                            idx = xblog_font_offsets.get(symbol)
                            if idx is None or idx >= len(font_words):
                                return 0
                            return font_words[idx]
                        def title_word_for(symbol):
                            idx = xblog_title_offsets.get(symbol)
                            if idx is None or idx >= len(title_words):
                                return 0
                            return title_words[idx]
                        render_backend = word_for("_g_SPXBRenderBackendMsec")
                        primitive_calls = word_for("_g_SPXBFakeGLPrimitiveCalls")
                        primitive_verts = word_for("_g_SPXBFakeGLPrimitiveVerts")
                        state_flushes = word_for("_g_SPXBFakeGLStateFlushes")
                        native_draw_mode = word_for("_g_SPXBNativeDrawMode")
                        native_draw_count = word_for("_g_SPXBNativeDrawCount")
                        native_draw_source_vertices = word_for(
                            "_g_SPXBNativeDrawSourceVertices")
                        native_draw_max_index = word_for(
                            "_g_SPXBNativeDrawMaxIndex")
                        native_draw_stride = word_for("_g_SPXBNativeDrawStride")
                        native_draw_indices = word_for(
                            "_g_SPXBNativeDrawIndicesPtr")
                        native_draw_vertices = word_for(
                            "_g_SPXBNativeDrawVerticesPtr")
                        native_draw_path = word_for("_g_SPXBNativeDrawPath")
                        native_draw_shader = word_for("_g_SPXBNativeDrawShader")
                        native_draw_vertex_offset = word_for(
                            "_g_SPXBNativeDrawVertexOffset")
                        native_draw_index_offset = word_for(
                            "_g_SPXBNativeDrawIndexOffset")
                        native_draw_vertex_bytes = word_for(
                            "_g_SPXBNativeDrawVertexBytes")
                        native_draw_index_bytes = word_for(
                            "_g_SPXBNativeDrawIndexBytes")
                        native_draw_lock_flags = word_for(
                            "_g_SPXBNativeDrawLockFlags")
                        native_draw_min_index = word_for(
                            "_g_SPXBNativeDrawMinIndex")
                        native_multitex_attempts = word_for(
                            "_g_SPXBNativeMultiTexAttempts")
                        native_multitex_draws = word_for(
                            "_g_SPXBNativeMultiTexDraws")
                        native_multitex_ready = word_for(
                            "_g_SPXBNativeMultiTexReady")
                        native_multitex_mismatch = word_for(
                            "_g_SPXBNativeMultiTexMismatch")
                        native_indexed_draw_failures = word_for(
                            "_g_SPXBNativeIndexedDrawFailures")
                        native_stage1_applies = word_for(
                            "_g_SPXBNativeStage1Applies")
                        native_stage1_failures = word_for(
                            "_g_SPXBNativeStage1ApplyFailures")
                        lightmap_multitex_draws = word_for(
                            "_g_SPXBLightmapMultiTexDraws")
                        lightmap_bundle0_draws = word_for(
                            "_g_SPXBLightmapBundle0Draws")
                        lightmap_bundle1_draws = word_for(
                            "_g_SPXBLightmapBundle1Draws")
                        lightmap_last_env = word_for(
                            "_g_SPXBLightmapLastEnv")
                        lightmap_last_tex0 = word_for(
                            "_g_SPXBLightmapLastTex0")
                        lightmap_last_tex1 = word_for(
                            "_g_SPXBLightmapLastTex1")
                        lightmap_last_flags = word_for(
                            "_g_SPXBLightmapLastFlags")
                        lightmap_upload_count = word_for(
                            "_g_SPXBLightmapUploadCount")
                        lightmap_upload_source_minmax = word_for(
                            "_g_SPXBLightmapUploadSourceMinMax")
                        lightmap_upload_source_avg = word_for(
                            "_g_SPXBLightmapUploadSourceAvg")
                        lightmap_upload_encoded_minmax = word_for(
                            "_g_SPXBLightmapUploadEncodedMinMax")
                        lightmap_upload_encoded_avg = word_for(
                            "_g_SPXBLightmapUploadEncodedAvg")
                        lightmap_upload_checksum = word_for(
                            "_g_SPXBLightmapUploadChecksum")
                        lightmap_upload_format = word_for(
                            "_g_SPXBLightmapUploadFormat")
                        lightmap_upload_size = word_for(
                            "_g_SPXBLightmapUploadSize")
                        workload_surfaces = word_for(
                            "_g_SPXBWorkloadSurfaces")
                        workload_batches = word_for(
                            "_g_SPXBWorkloadBatches")
                        workload_vertexes = word_for(
                            "_g_SPXBWorkloadVertexes")
                        workload_indexes = word_for(
                            "_g_SPXBWorkloadIndexes")
                        workload_total_indexes = word_for(
                            "_g_SPXBWorkloadTotalIndexes")
                        main_loop_count = word_for("_g_SPXBMainLoopCount")
                        com_frame_count = word_for("_g_SPXBComFrameCount")
                        sv_frame_count = word_for("_g_SPXBSvFrameCount")
                        cl_frame_count = word_for("_g_SPXBClFrameCount")
                        cls_state = word_for("_g_SPXBClsState")
                        cl_server_time = word_for("_g_SPXBClServerTime")
                        cls_frame_count = word_for("_g_SPXBClsFrameCount")
                        phase_last = word_for("_g_SPXBPhaseLast")
                        input_poll_count = word_for("_g_SPXBInputPollCount")
                        input_port = word_for("_g_SPXBInputPort")
                        input_digital = word_for("_g_SPXBInputDigital")
                        input_analog = word_for("_g_SPXBInputAnalogMask")
                        input_lxly = word_for("_g_SPXBInputLXLY")
                        input_rxry = word_for("_g_SPXBInputRXRY")
                        input_menu_edges = word_for("_g_SPXBInputMenuEdgeCount")
                        input_menu_edge_last = word_for("_g_SPXBInputMenuEdgeLast")
                        input_common_presses = word_for("_g_SPXBInputCommonPressCount")
                        input_common_press_last = word_for("_g_SPXBInputCommonPressLast")
                        input_frontend_queues = word_for("_g_SPXBInputFrontendQueueCount")
                        input_frontend_queue_last = word_for("_g_SPXBInputFrontendQueueLast")
                        input_dispatches = word_for("_g_SPXBInputDispatchCount")
                        input_dispatch_last = word_for("_g_SPXBInputDispatchLast")
                        input_dispatch_handled = word_for("_g_SPXBInputDispatchHandled")
                        hm_info_dispatches = word_for("_g_SPXBHMInfoDispatchCount")
                        hm_info_dispatch_last = word_for("_g_SPXBHMInfoDispatchLast")
                        hm_game_commands = word_for("_g_SPXBHMGameCommandCount")
                        hm_game_command_result = word_for("_g_SPXBHMGameCommandResult")
                        hm_console_commands = word_for("_g_SPXBHMConsoleCommandCount")
                        hm_console_command_tag = word_for("_g_SPXBHMConsoleCommandTag")
                        hm_scores_down = word_for("_g_SPXBHMScoresDownCount")
                        hm_scores_up = word_for("_g_SPXBHMScoresUpCount")
                        hm_score_draws = word_for("_g_SPXBHMScoreDrawCount")
                        hm_score_draw_state = word_for("_g_SPXBHMScoreDrawState")
                        hm_text_length = word_for("_g_SPXBHMTextLoadLength")
                        hm_text_count = word_for("_g_SPXBHMTextLoadCount")
                        hm_text_state = word_for("_g_SPXBHMTextLoadState")
                        hm_text_trace_calls = word_for("_g_SPXBHMTextTraceCalls")
                        hm_text_trace_stage = word_for("_g_SPXBHMTextTraceStage")
                        hm_text_trace_open_length = word_for("_g_SPXBHMTextTraceOpenLength")
                        hm_text_trace_handle = word_for("_g_SPXBHMTextTraceHandle")
                        hm_text_trace_raw = word_for("_g_SPXBHMTextTraceRawPrefix")
                        hm_text_trace_parsed = word_for("_g_SPXBHMTextTraceParsedPrefix")
                        hm_text_trace_score = word_for("_g_SPXBHMTextTraceScorePrefix")
                        hm_text_trace_pointer = word_for("_g_SPXBHMTextTraceFirstPointer")
                        render_command_high_water = word_for("_g_SPXBRenderCommandHighWater")
                        render_command_drops = word_for("_g_SPXBRenderCommandDrops")
                        render_command_last_drop = word_for("_g_SPXBRenderCommandLastDrop")
                        render_command_calls = word_for("_g_SPXBRenderCommandCalls")
                        render_command_issue_count = word_for("_g_SPXBRenderCommandIssueCount")
                        render_command_last_used = word_for("_g_SPXBRenderCommandLastUsed")
                        hm_score_stretch_count = word_for("_g_SPXBHMScoreStretchCount")
                        hm_score_stretch_shader = word_for("_g_SPXBHMScoreStretchShader")
                        hm_score_stretch_x = word_for("_g_SPXBHMScoreStretchX")
                        hm_score_stretch_y = word_for("_g_SPXBHMScoreStretchY")
                        hm_score_stretch_w = word_for("_g_SPXBHMScoreStretchW")
                        hm_score_stretch_h = word_for("_g_SPXBHMScoreStretchH")
                        hm_score_scale_x = word_for("_g_SPXBHMScoreScaleX")
                        hm_score_scale_y = word_for("_g_SPXBHMScoreScaleY")
                        hm_score_white_shader = word_for("_g_SPXBHMScoreWhiteShader")
                        hm_score_queued_count = word_for("_g_SPXBHMScoreQueuedCount")
                        hm_score_queued_shader = word_for("_g_SPXBHMScoreQueuedShader")
                        hm_score_backend_matches = word_for("_g_SPXBHMScoreBackendMatches")
                        hm_score_backend_color = word_for("_g_SPXBHMScoreBackendColor")
                        render_backend_commands = word_for("_g_SPXBRenderBackendCommandCount")
                        render_backend_stretches = word_for("_g_SPXBRenderBackendStretchCount")
                        render_backend_terminal = word_for("_g_SPXBRenderBackendTerminalId")
                        render_backend_bytes = word_for("_g_SPXBRenderBackendBytes")
                        hm_score_backend_geometry = word_for("_g_SPXBHMScoreBackendGeometry")
                        hm_score_backend_geom_shader = word_for("_g_SPXBHMScoreBackendGeomShader")
                        hm_score_backend_geom_color = word_for("_g_SPXBHMScoreBackendGeomColor")
                        render_backend_done_commands = word_for("_g_SPXBRenderBackendDoneCommands")
                        render_backend_done_stretches = word_for("_g_SPXBRenderBackendDoneStretches")
                        render_backend_done_terminal = word_for("_g_SPXBRenderBackendDoneTerminal")
                        render_backend_done_bytes = word_for("_g_SPXBRenderBackendDoneBytes")
                        hm_score_backend_done_geometry = word_for("_g_SPXBHMScoreBackendDoneGeometry")
                        hm_score_backend_done_shader = word_for("_g_SPXBHMScoreBackendDoneShader")
                        hm_score_backend_done_color = word_for("_g_SPXBHMScoreBackendDoneColor")
                        hm_score_batch_pending = word_for("_g_SPXBHMScoreBatchPending")
                        hm_score_surface_flags = word_for("_g_SPXBHMScoreSurfaceFlags")
                        hm_score_surface_verts = word_for("_g_SPXBHMScoreSurfaceVerts")
                        hm_score_surface_indexes = word_for("_g_SPXBHMScoreSurfaceIndexes")
                        hm_score_surface_passes = word_for("_g_SPXBHMScoreSurfacePasses")
                        hm_score_surface_state = word_for("_g_SPXBHMScoreSurfaceState")
                        hm_score_submit_armed = word_for("_g_SPXBHMScoreSubmitArmed")
                        hm_score_submit_calls = word_for("_g_SPXBHMScoreSubmitCalls")
                        hm_score_submit_indexes = word_for("_g_SPXBHMScoreSubmitIndexes")
                        hm_score_submit_state = word_for("_g_SPXBHMScoreSubmitState")
                        hm_score_submit_texture = word_for("_g_SPXBHMScoreSubmitTexture")
                        hm_score_submit_scissor = word_for("_g_SPXBHMScoreSubmitScissor")
                        hm_score_submit_scissor_xy = word_for("_g_SPXBHMScoreSubmitScissorXY")
                        hm_score_submit_scissor_wh = word_for("_g_SPXBHMScoreSubmitScissorWH")
                        hm_score_shader_flags = word_for("_g_SPXBHMScoreShaderFlags")
                        hm_score_image_tex = word_for("_g_SPXBHMScoreImageTex")
                        hm_score_white_tex = word_for("_g_SPXBHMScoreWhiteTex")
                        hm_score_image_wh = word_for("_g_SPXBHMScoreImageWH")
                        hm_score_surface_min_xy = word_for("_g_SPXBHMScoreSurfaceMinXY")
                        hm_score_surface_max_xy = word_for("_g_SPXBHMScoreSurfaceMaxXY")
                        hm_score_submit_target = word_for("_g_SPXBHMScoreSubmitTarget")
                        hm_score_submit_color_write = word_for("_g_SPXBHMScoreSubmitColorWrite")
                        hm_score_submit_cull = word_for("_g_SPXBHMScoreSubmitCull")
                        hm_score_submit_blend = word_for("_g_SPXBHMScoreSubmitBlend")
                        hm_score_submit_viewport_xy = word_for("_g_SPXBHMScoreSubmitViewportXY")
                        hm_score_submit_viewport_wh = word_for("_g_SPXBHMScoreSubmitViewportWH")
                        hm_score_submit_proj00 = word_for("_g_SPXBHMScoreSubmitProj00")
                        hm_score_submit_proj11 = word_for("_g_SPXBHMScoreSubmitProj11")
                        hm_score_submit_proj30 = word_for("_g_SPXBHMScoreSubmitProj30")
                        hm_score_submit_proj31 = word_for("_g_SPXBHMScoreSubmitProj31")
                        hm_score_texture_data0 = word_for("_g_SPXBHMScoreTextureData0")
                        hm_score_texture_size = word_for("_g_SPXBHMScoreTextureSize")
                        hm_score_texture_wh = word_for("_g_SPXBHMScoreTextureWH")
                        hm_score_texture_format = word_for("_g_SPXBHMScoreTextureFormat")
                        hm_score_stage_color = word_for("_g_SPXBHMScoreStageColor")
                        hm_score_stage_alpha = word_for("_g_SPXBHMScoreStageAlpha")
                        hm_score_depth_state = word_for("_g_SPXBHMScoreDepthState")
                        hm_score_vertex_shader = word_for("_g_SPXBHMScoreVertexShader")
                        hm_score_pixel_shader = word_for("_g_SPXBHMScorePixelShader")
                        hm_score_vertex0_x = word_for("_g_SPXBHMScoreVertex0X")
                        hm_score_vertex0_y = word_for("_g_SPXBHMScoreVertex0Y")
                        hm_score_vertex0_z = word_for("_g_SPXBHMScoreVertex0Z")
                        hm_score_vertex0_w = word_for("_g_SPXBHMScoreVertex0W")
                        hm_score_vertex0_color = word_for("_g_SPXBHMScoreVertex0Color")
                        hm_score_vertex0_u = word_for("_g_SPXBHMScoreVertex0U")
                        hm_score_vertex0_v = word_for("_g_SPXBHMScoreVertex0V")
                        hm_score_vertex_count = word_for("_g_SPXBHMScoreVertexCount")
                        hm_score_vertex_stride = word_for("_g_SPXBHMScoreVertexStride")
                        hm_score_index012 = word_for("_g_SPXBHMScoreIndex012")
                        usercmd_count = word_for("_g_SPXBUsercmdCount")
                        usercmd_time = word_for("_g_SPXBUsercmdTime")
                        usercmd_move = word_for("_g_SPXBUsercmdMove")
                        usercmd_buttons = word_for("_g_SPXBUsercmdButtons")
                        usercmd_yaw = word_for("_g_SPXBUsercmdYaw")
                        sv_usercmd_count = word_for("_g_SPXBSVUsercmdCount")
                        sv_usercmd_time = word_for("_g_SPXBSVUsercmdTime")
                        sv_usercmd_move = word_for("_g_SPXBSVUsercmdMove")
                        sv_usercmd_buttons = word_for("_g_SPXBSVUsercmdButtons")
                        hm_getcmd_count = word_for("_g_SPXBHMGetUsercmdCount")
                        hm_getcmd_time = word_for("_g_SPXBHMGetUsercmdTime")
                        hm_getcmd_move = word_for("_g_SPXBHMGetUsercmdMove")
                        hm_getcmd_buttons = word_for("_g_SPXBHMGetUsercmdButtons")
                        hm_think_count = word_for("_g_SPXBHMClientThinkCount")
                        cl_tail_stage = word_for("_g_SPXBClTailStage")
                        cgame_entry_current = word_for("_g_SPXBCGameEntryCurrent")
                        cgame_entry_expected = word_for("_g_SPXBCGameEntryExpected")
                        com_tail_stage = word_for("_g_SPXBComTailStage")
                        com_frame_depth = word_for("_g_SPXBComFrameDepth")
                        com_catch_count = word_for("_g_SPXBComCatchCount")
                        main_tail_stage = word_for("_g_SPXBMainTailStage")
                        com_subphase = word_for("_g_SPXBComSubphase")
                        com_spin_count = word_for("_g_SPXBComSpinCount")
                        com_msec = word_for("_g_SPXBComMsec")
                        com_frame_time = word_for("_g_SPXBComFrameTime")
                        com_last_time = word_for("_g_SPXBComLastTime")
                        cbuf_exec_count = word_for("_g_SPXBCbufExecCount")
                        cbuf_exec_depth = word_for("_g_SPXBCbufExecDepth")
                        cbuf_return_entry = word_for("_g_SPXBCbufReturnAddressEntry")
                        cbuf_return_exit = word_for("_g_SPXBCbufReturnAddressExit")
                        cmd_exec_count = word_for("_g_SPXBCmdExecCount")
                        cmd_phase = word_for("_g_SPXBCmdPhase")
                        cmd_hash = word_for("_g_SPXBCmdHash")
                        cmd_argc = word_for("_g_SPXBCmdArgc")
                        map_phase = word_for("_g_SPXBMapPhase")
                        packed_map_phase = word_for("_g_SPXBPackedMapPhase")
                        map_hash = word_for("_g_SPXBMapHash")
                        game_phase = word_for("_g_SPXBGamePhase")
                        game_entity_count = word_for("_g_SPXBGameEntityCount")
                        perf_frame = word_for("_g_SPXBPerfFrameMsec")
                        perf_server = word_for("_g_SPXBPerfServerMsec")
                        perf_client = word_for("_g_SPXBPerfClientMsec")
                        perf_game = word_for("_g_SPXBPerfGameMsec")
                        perf_frontend = word_for("_g_SPXBPerfFrontendMsec")
                        perf_backend = word_for("_g_SPXBPerfBackendMsec")
                        perf_backend_draw_surfs = word_for(
                            "_g_SPXBPerfBackendDrawSurfsMsec")
                        perf_backend_swap = word_for(
                            "_g_SPXBPerfBackendSwapMsec")
                        perf_backend_other = word_for(
                            "_g_SPXBPerfBackendOtherMsec")
                        perf_audio = word_for("_g_SPXBPerfAudioMsec")
                        hm_audio_backend = word_for("_g_SPXBHMAudioBackendState")
                        hm_audio_begin_registration = word_for(
                            "_g_SPXBHMAudioBeginRegistrationCount")
                        hm_audio_register = word_for("_g_SPXBHMAudioRegisterSoundCount")
                        hm_audio_start = word_for("_g_SPXBHMAudioStartSoundCount")
                        hm_audio_local = word_for("_g_SPXBHMAudioStartLocalCount")
                        hm_audio_loop = word_for("_g_SPXBHMAudioLoopCount")
                        hm_audio_respatialize = word_for(
                            "_g_SPXBHMAudioRespatializeCount")
                        hm_audio_listener = word_for("_g_SPXBHMAudioListenerState")
                        hm_audio_voice = word_for("_g_SPXBHMAudioVoiceStartCount")
                        hm_audio_lip = word_for("_g_SPXBHMAudioLipActiveCount")
                        hm_audio_last_ent_chan = word_for("_g_SPXBHMAudioLastEntChan")
                        hm_audio_last_handle = word_for("_g_SPXBHMAudioLastHandle")
                        hm_audio_listener_mask = word_for(
                            "_g_SPXBHMAudioListenerUpdateMask")
                        heartbeat_mem_used = word_for("_g_SPXBHeartbeatMemUsed")
                        heartbeat_mem_free = word_for("_g_SPXBHeartbeatMemFree")
                        heartbeat_mem_largest = word_for("_g_SPXBHeartbeatMemLargest")
                        heartbeat_mem_blocks = word_for("_g_SPXBHeartbeatMemBlocks")
                        hm_split_proof_magic = word_for("_g_SPXBHMSplitProofMagic")
                        hm_split_render_armed = word_for("_g_SPXBHMSplitRenderArmedPlayers")
                        hm_split_hud_divider = word_for("_g_SPXBHMSplitHudDividerSerial")
                        hm_split_fp_filter_mask = word_for("_g_SPXBHMSplitFPFilterMask")
                        hm_split_self_filter_mask = word_for("_g_SPXBHMSplitSelfFilterMask")
                        perf_server_ticks = word_for("_g_SPXBPerfServerTicks")
                        perf_server_last_game = word_for("_g_SPXBPerfServerLastGameMsec")
                        perf_server_max_game = word_for("_g_SPXBPerfServerMaxGameMsec")
                        perf_game_pre = word_for("_g_SPXBPerfGamePreMsec")
                        perf_game_entities = word_for("_g_SPXBPerfGameEntitiesMsec")
                        perf_game_post = word_for("_g_SPXBPerfGamePostMsec")
                        perf_game_entities_visited = word_for("_g_SPXBPerfGameEntitiesVisited")
                        perf_game_missiles = word_for("_g_SPXBPerfGameMissiles")
                        perf_game_items = word_for("_g_SPXBPerfGameItems")
                        perf_game_movers = word_for("_g_SPXBPerfGameMovers")
                        perf_game_clients = word_for("_g_SPXBPerfGameClients")
                        perf_game_think_due = word_for("_g_SPXBPerfGameThinkDue")
                        perf_game_scripted = word_for("_g_SPXBPerfGameScripted")
                        perf_game_other = word_for("_g_SPXBPerfGameOther")
                        perf_screen_draw = word_for("_g_SPXBPerfScreenDrawMsec")
                        perf_end_frame = word_for("_g_SPXBPerfEndFrameMsec")
                        perf_render_total = word_for("_g_SPXBPerfRenderTotalMsec")
                        perf_render_setup = word_for("_g_SPXBPerfRenderSetupMsec")
                        perf_render_mark = word_for("_g_SPXBPerfRenderMarkLeavesMsec")
                        perf_render_world = word_for("_g_SPXBPerfRenderWorldMsec")
                        perf_render_polys = word_for("_g_SPXBPerfRenderPolysMsec")
                        perf_render_projection = word_for("_g_SPXBPerfRenderProjectionMsec")
                        perf_render_entities = word_for("_g_SPXBPerfRenderEntitiesMsec")
                        perf_render_sort = word_for("_g_SPXBPerfRenderSortMsec")
                        perf_render_debug = word_for("_g_SPXBPerfRenderDebugMsec")
                        perf_render_views = word_for("_g_SPXBPerfRenderViews")
                        perf_render_portals = word_for("_g_SPXBPerfRenderPortals")
                        perf_render_draw_surfs = word_for("_g_SPXBPerfRenderDrawSurfs")
                        perf_render_ref_entities = word_for("_g_SPXBPerfRenderRefEntities")
                        perf_render_leafs = word_for("_g_SPXBPerfRenderLeafs")
                        perf_backend_surfaces = word_for("_g_SPXBPerfBackendSurfaces")
                        perf_backend_vertexes = word_for("_g_SPXBPerfBackendVertexes")
                        perf_backend_indexes = word_for("_g_SPXBPerfBackendIndexes")
                        perf_backend_total_indexes = word_for("_g_SPXBPerfBackendTotalIndexes")
                        perf_finish_msec = word_for("_g_SPXBPerfFinishMsec")
                        perf_present_msec = word_for("_g_SPXBPerfPresentMsec")
                        perf_backend_batches = word_for("_g_SPXBPerfBackendBatches")
                        perf_submit_calls = word_for("_g_SPXBPerfSubmitCalls")
                        perf_draw_cycles = word_for("_g_SPXBPerfDrawCycles")
                        perf_draw_state_cycles = word_for("_g_SPXBPerfDrawStateCycles")
                        perf_draw_reserve_cycles = word_for("_g_SPXBPerfDrawReserveCycles")
                        perf_draw_pack_cycles = word_for("_g_SPXBPerfDrawPackCycles")
                        perf_draw_index_cycles = word_for("_g_SPXBPerfDrawIndexCycles")
                        perf_draw_submit_cycles = word_for("_g_SPXBPerfDrawSubmitCycles")
                        perf_indexed_submits = word_for("_g_SPXBPerfIndexedSubmitCalls")
                        perf_immediate_submits = word_for("_g_SPXBPerfImmediateSubmitCalls")
                        perf_indexed_tex1 = word_for("_g_SPXBPerfIndexedTex1Calls")
                        perf_indexed_dwords = word_for("_g_SPXBPerfIndexedReserveDwords")
                        perf_immediate_dwords = word_for("_g_SPXBPerfImmediateReserveDwords")
                        perf_begin_push_max_cycles = word_for("_g_SPXBPerfDrawBeginPushMaxCyclesCurrent")
                        perf_begin_push_max_dwords = word_for("_g_SPXBPerfDrawBeginPushMaxDwordsCurrent")
                        perf_begin_push_over_100k = word_for("_g_SPXBPerfDrawBeginPushOver100KCurrent")
                        perf_begin_push_over_1ms = word_for("_g_SPXBPerfDrawBeginPushOver1MsecCurrent")
                        perf_begin_push_over_10ms = word_for("_g_SPXBPerfDrawBeginPushOver10MsecCurrent")
                        perf_begin_push_max_state = word_for("_g_SPXBPerfDrawBeginPushMaxStateCurrent")
                        perf_draw_set_stream_cycles = word_for("_g_SPXBPerfDrawSetStreamCycles")
                        perf_draw_begin_push_cycles = word_for("_g_SPXBPerfDrawBeginPushCycles")
                        perf_draw_pointer_cycles = word_for("_g_SPXBPerfDrawPointerCycles")
                        perf_indexed_opaque = word_for("_g_SPXBPerfIndexedOpaqueCallsCurrent")
                        perf_indexed_blend = word_for("_g_SPXBPerfIndexedBlendCallsCurrent")
                        perf_indexed_alpha_test = word_for("_g_SPXBPerfIndexedAlphaTestCallsCurrent")
                        perf_indexed_no_depth_write = word_for("_g_SPXBPerfIndexedNoDepthWriteCallsCurrent")
                        perf_indexed_no_depth_test = word_for("_g_SPXBPerfIndexedNoDepthTestCallsCurrent")
                        perf_indexed_two_sided = word_for("_g_SPXBPerfIndexedTwoSidedCallsCurrent")
                        perf_indexed_blend_indexes = word_for("_g_SPXBPerfIndexedBlendIndexesCurrent")
                        perf_indexed_alpha_test_indexes = word_for("_g_SPXBPerfIndexedAlphaTestIndexesCurrent")
                        perf_indexed_no_depth_write_indexes = word_for("_g_SPXBPerfIndexedNoDepthWriteIndexesCurrent")
                        perf_indexed_two_sided_indexes = word_for("_g_SPXBPerfIndexedTwoSidedIndexesCurrent")
                        perf_sample_active = word_for("_g_SPXBPerfSampleActive")
                        perf_sample_serial = word_for("_g_SPXBPerfSampleSerial")
                        camera_active = word_for("_g_SPXBCameraActive")
                        borg_plugged_count = word_for("_g_SPXBBorgPluggedCount")
                        borg_plugged_ent = word_for("_g_SPXBBorgPluggedEnt")
                        borg_plugged_spawnflags = word_for("_g_SPXBBorgPluggedSpawnflags")
                        borg_plugged_anim = word_for("_g_SPXBBorgPluggedAnim")
                        borg_plugged_legs_model = word_for("_g_SPXBBorgPluggedLegsModel")
                        borg_plugged_torso_model = word_for("_g_SPXBBorgPluggedTorsoModel")
                        borg_plugged_head_model = word_for("_g_SPXBBorgPluggedHeadModel")
                        borg_plugged_legs_skin = word_for("_g_SPXBBorgPluggedLegsSkin")
                        borg_plugged_torso_skin = word_for("_g_SPXBBorgPluggedTorsoSkin")
                        borg_plugged_head_skin = word_for("_g_SPXBBorgPluggedHeadSkin")
                        borg_plugged_legs_hash = word_for("_g_SPXBBorgPluggedLegsNameHash")
                        borg_plugged_torso_hash = word_for("_g_SPXBBorgPluggedTorsoNameHash")
                        borg_plugged_head_hash = word_for("_g_SPXBBorgPluggedHeadNameHash")
                        borg_active_count = word_for("_g_SPXBBorgActiveCount")
                        borg_active_ent = word_for("_g_SPXBBorgActiveEnt")
                        borg_active_spawnflags = word_for("_g_SPXBBorgActiveSpawnflags")
                        borg_active_anim = word_for("_g_SPXBBorgActiveAnim")
                        borg_active_legs_model = word_for("_g_SPXBBorgActiveLegsModel")
                        borg_active_torso_model = word_for("_g_SPXBBorgActiveTorsoModel")
                        borg_active_head_model = word_for("_g_SPXBBorgActiveHeadModel")
                        borg_active_legs_skin = word_for("_g_SPXBBorgActiveLegsSkin")
                        borg_active_torso_skin = word_for("_g_SPXBBorgActiveTorsoSkin")
                        borg_active_head_skin = word_for("_g_SPXBBorgActiveHeadSkin")
                        borg_active_legs_hash = word_for("_g_SPXBBorgActiveLegsNameHash")
                        borg_active_torso_hash = word_for("_g_SPXBBorgActiveTorsoNameHash")
                        borg_active_head_hash = word_for("_g_SPXBBorgActiveHeadNameHash")
                        split_shader = word_for("_g_SPXBRenderSplitShader")
                        split_fog = word_for("_g_SPXBRenderSplitFog")
                        split_dlight = word_for("_g_SPXBRenderSplitDlight")
                        split_entity = word_for("_g_SPXBRenderSplitEntity")
                        split_final = word_for("_g_SPXBRenderSplitFinal")
                        split_flush = word_for("_g_SPXBRenderSplitFlush")
                        split_slot_active = word_for("_g_SPXBSplitSlotActive")
                        split_slot0_draw = word_for("_g_SPXBSplitSlot0DrawDelta")
                        split_slot1_draw = word_for("_g_SPXBSplitSlot1DrawDelta")
                        split_slot0_world = word_for("_g_SPXBSplitSlot0WorldDelta")
                        split_slot1_world = word_for("_g_SPXBSplitSlot1WorldDelta")
                        split_slot0_cluster = word_for("_g_SPXBSplitSlot0Cluster")
                        split_slot1_cluster = word_for("_g_SPXBSplitSlot1Cluster")
                        split_slot1_retry = word_for("_g_SPXBSplitSlot1WorldRetryDelta")
                        split_slot1_fallback = word_for("_g_SPXBSplitSlot1WorldFallback")
                        split_slot0_marked = word_for("_g_SPXBSplitSlot0MarkedLeaves")
                        split_slot1_marked = word_for("_g_SPXBSplitSlot1MarkedLeaves")
                        split_slot0_pvsrej = word_for("_g_SPXBSplitSlot0PvsRejected")
                        split_slot1_pvsrej = word_for("_g_SPXBSplitSlot1PvsRejected")
                        split_slot0_arearej = word_for("_g_SPXBSplitSlot0AreaRejected")
                        split_slot1_arearej = word_for("_g_SPXBSplitSlot1AreaRejected")
                        split_slot0_rootvis = word_for("_g_SPXBSplitSlot0RootVis")
                        split_slot1_rootvis = word_for("_g_SPXBSplitSlot1RootVis")
                        split_slot0_attempt = word_for("_g_SPXBSplitSlot0WorldAttempts")
                        split_slot1_attempt = word_for("_g_SPXBSplitSlot1WorldAttempts")
                        split_slot0_culled = word_for("_g_SPXBSplitSlot0WorldCulled")
                        split_slot1_culled = word_for("_g_SPXBSplitSlot1WorldCulled")
                        split_slot0_already = word_for("_g_SPXBSplitSlot0WorldAlready")
                        split_slot1_already = word_for("_g_SPXBSplitSlot1WorldAlready")
                        split_slot0_added = word_for("_g_SPXBSplitSlot0WorldAdded")
                        split_slot1_added = word_for("_g_SPXBSplitSlot1WorldAdded")
                        split_p2_ent = word_for("_g_SPXBSplitP2Ent")
                        split_p2_trace = word_for("_g_SPXBSplitP2TraceFrac1000")
                        split_p2_view_x = word_for("_g_SPXBSplitP2ViewX")
                        split_p2_view_y = word_for("_g_SPXBSplitP2ViewY")
                        split_p2_view_z = word_for("_g_SPXBSplitP2ViewZ")
                        split_p2_ps_x = word_for("_g_SPXBSplitP2PsX")
                        split_p2_ps_y = word_for("_g_SPXBSplitP2PsY")
                        split_p2_ps_z = word_for("_g_SPXBSplitP2PsZ")
                        split_p2_cur_x = word_for("_g_SPXBSplitP2CurX")
                        split_p2_cur_y = word_for("_g_SPXBSplitP2CurY")
                        split_p2_cur_z = word_for("_g_SPXBSplitP2CurZ")
                        split_p2_pitch = word_for("_g_SPXBSplitP2AnglesPitch")
                        split_p2_yaw = word_for("_g_SPXBSplitP2AnglesYaw")
                        split_p2_refdef = word_for("_g_SPXBSplitP2RefdefValid")
                        split_p2_scene_considered = word_for("_g_SPXBSplitP2SceneConsidered")
                        split_p2_scene_added = word_for("_g_SPXBSplitP2SceneAdded")
                        split_p2_scene_self = word_for("_g_SPXBSplitP2SceneSelfAdded")
                        split_p2_model_enter = word_for("_g_SPXBSplitP2ModelEnter")
                        split_p2_model_return = word_for("_g_SPXBSplitP2ModelReturn")
                        split_p2_model_info = word_for("_g_SPXBSplitP2ModelInfoValid")
                        split_p2_model_submitted = word_for("_g_SPXBSplitP2ModelSubmitted")
                        split_p2_model_legs = word_for("_g_SPXBSplitP2ModelLegs")
                        split_p2_model_torso = word_for("_g_SPXBSplitP2ModelTorso")
                        split_p2_model_head = word_for("_g_SPXBSplitP2ModelHead")
                        split_p2_model_renderfx = word_for("_g_SPXBSplitP2ModelRenderfx")
                        split_p2_renderer_refs = word_for("_g_SPXBSplitP2RendererRefs")
                        split_p2_renderer_model = word_for("_g_SPXBSplitP2RendererLastModel")
                        split_p2_renderer_renderfx = word_for("_g_SPXBSplitP2RendererLastRenderfx")
                        split_p2_renderer_z = word_for("_g_SPXBSplitP2RendererLastZ")
                        vw_p1_adds = word_for("_g_SPXBViewWeaponP1Adds")
                        vw_p2_adds = word_for("_g_SPXBViewWeaponP2Adds")
                        vw_p1_skips = word_for("_g_SPXBViewWeaponP1Skips")
                        vw_p2_skips = word_for("_g_SPXBViewWeaponP2Skips")
                        vw_p1_model = word_for("_g_SPXBViewWeaponP1Model")
                        vw_p2_model = word_for("_g_SPXBViewWeaponP2Model")
                        vw_p1_rf = word_for("_g_SPXBViewWeaponP1Renderfx")
                        vw_p2_rf = word_for("_g_SPXBViewWeaponP2Renderfx")
                        vw_p1_render_adds = word_for("_g_SPXBViewWeaponP1RendererAdds")
                        vw_p2_render_adds = word_for("_g_SPXBViewWeaponP2RendererAdds")
                        vw_p1_render_filtered = word_for("_g_SPXBViewWeaponP1RendererFiltered")
                        vw_p2_render_filtered = word_for("_g_SPXBViewWeaponP2RendererFiltered")
                        vw_p1_last_skip = word_for("_g_SPXBViewWeaponP1LastSkip")
                        vw_p2_last_skip = word_for("_g_SPXBViewWeaponP2LastSkip")
                        weapon_reg_weapon = word_for("_g_SPXBWeaponRegWeapon")
                        weapon_reg_hash = word_for("_g_SPXBWeaponRegPathHash")
                        weapon_reg_view = word_for("_g_SPXBWeaponRegViewModel")
                        weapon_reg_world = word_for("_g_SPXBWeaponRegWorldModel")
                        weapon_reg_hands = word_for("_g_SPXBWeaponRegHandsModel")
                        weapon_reg_fail = word_for("_g_SPXBWeaponRegFailCode")
                        weapon_model_stage = word_for("_g_SPXBWeaponModelTraceStage")
                        weapon_model_hash = word_for("_g_SPXBWeaponModelTracePathHash")
                        weapon_model_disk_len = word_for("_g_SPXBWeaponModelTraceDiskLen")
                        weapon_model_disk_success = word_for("_g_SPXBWeaponModelTraceDiskSuccess")
                        weapon_model_ident = word_for("_g_SPXBWeaponModelTraceIdent")
                        weapon_model_version = word_for("_g_SPXBWeaponModelTraceVersion")
                        weapon_model_size = word_for("_g_SPXBWeaponModelTraceSize")
                        weapon_model_loaded = word_for("_g_SPXBWeaponModelTraceLoaded")
                        weapon_model_handle = word_for("_g_SPXBWeaponModelTraceHandle")
                        weapon_model_fail = word_for("_g_SPXBWeaponModelTraceFailCode")
                        model_probe_stage = word_for("_g_SPXBModelProbeStage")
                        model_probe_hash = word_for("_g_SPXBModelProbePathHash")
                        model_probe_name = word_for("_g_SPXBModelProbeNamePtr")
                        model_probe_len = word_for("_g_SPXBModelProbeFileLen")
                        file_alloc_stage = word_for("_g_SPXBFileAllocStage")
                        file_alloc_hash = word_for("_g_SPXBFileAllocPathHash")
                        file_alloc_path = word_for("_g_SPXBFileAllocPathPtr")
                        file_alloc_len = word_for("_g_SPXBFileAllocLength")
                        file_alloc_tag = word_for("_g_SPXBFileAllocTag")
                        file_alloc_mutex = word_for("_g_SPXBFileAllocMutex")
                        file_alloc_wait = word_for("_g_SPXBFileAllocWaitResult")
                        file_alloc_release = word_for("_g_SPXBFileAllocReleaseResult")
                        weapon_load_stage = word_for("_g_SPXBWeaponLoadStage")
                        weapon_load_read_len = word_for("_g_SPXBWeaponLoadReadLen")
                        weapon_load_type_weapon = word_for("_g_SPXBWeaponLoadTypeWeapon")
                        weapon_load_model_weapon = word_for("_g_SPXBWeaponLoadModelWeapon")
                        weapon_load_model_hash = word_for("_g_SPXBWeaponLoadModelHash")
                        weapon_load_slot4_hash = word_for("_g_SPXBWeaponLoadSlot4Hash")
                        weapon_load_slot4_ammo = word_for("_g_SPXBWeaponLoadSlot4Ammo")
                        weapon_load_slot4_first4 = word_for("_g_SPXBWeaponLoadSlot4First4")
                        weapon_reg_first4 = word_for("_g_SPXBWeaponRegFirst4")
                        weapon_reg_class_hash = word_for("_g_SPXBWeaponRegClassHash")
                        split_camera_mode = word_for("_g_SPXBSplitCameraMode")
                        split_p1_trace = word_for("_g_SPXBSplitP1TraceFrac1000")
                        split_p1_local_x = word_for("_g_SPXBSplitP1LocalX1000")
                        split_p1_local_y = word_for("_g_SPXBSplitP1LocalY1000")
                        split_p1_local_z = word_for("_g_SPXBSplitP1LocalZ1000")
                        split_p2_local_x = word_for("_g_SPXBSplitP2LocalX1000")
                        split_p2_local_y = word_for("_g_SPXBSplitP2LocalY1000")
                        split_p2_local_z = word_for("_g_SPXBSplitP2LocalZ1000")
                        split_local_diff_x = word_for("_g_SPXBSplitLocalDiffX1000")
                        split_local_diff_y = word_for("_g_SPXBSplitLocalDiffY1000")
                        split_local_diff_z = word_for("_g_SPXBSplitLocalDiffZ1000")
                        direct_status = word_for("_g_SPXBDirectMapStatus")
                        direct_hash = word_for("_g_SPXBDirectMapHash")
                        direct_queued = word_for("_g_SPXBDirectMapQueuedCount")
                        sky_magic = word_for("_g_SPXBSkyTraceMagic")
                        sky_present = word_for("_g_SPXBSkyOuterPresentMask")
                        sky_fallback = word_for("_g_SPXBSkyOuterFallbackMask")
                        sky_tex = word_for("_g_SPXBSkyOuterTexMask")
                        sky_draw = word_for("_g_SPXBSkyOuterDrawMask")
                        sky_passes = word_for("_g_SPXBSkyLastPasses")
                        sky_sort = word_for("_g_SPXBSkyLastSort")
                        skyres_magic = word_for("_g_SPXBSkyResolveMagic")
                        skyres_count = word_for("_g_SPXBSkyResolveCount")
                        skyres_shader_num = word_for("_g_SPXBSkyResolveShaderNum")
                        skyres_map_hash = word_for("_g_SPXBSkyResolveMapHash")
                        skyres_resolved_hash = word_for("_g_SPXBSkyResolveResolvedHash")
                        skyres_surface_flags = word_for("_g_SPXBSkyResolveSurfaceFlags")
                        skyres_default = word_for("_g_SPXBSkyResolveDefault")
                        skyres_explicit = word_for("_g_SPXBSkyResolveExplicit")
                        skyres_has_sky = word_for("_g_SPXBSkyResolveHasSky")
                        skyres_passes = word_for("_g_SPXBSkyResolvePasses")
                        skyres_sort_x1000 = word_for("_g_SPXBSkyResolveSortX1000")
                        skyres_lightmap0 = word_for("_g_SPXBSkyResolveLightmap0")
                        shader_scan_magic = word_for("_g_SPXBShaderScanMagic")
                        shader_scan_scripts = word_for("_g_SPXBShaderScanScriptsFound")
                        shader_scan_shaders = word_for("_g_SPXBShaderScanShadersFound")
                        shader_scan_loaded = word_for("_g_SPXBShaderScanLoaded")
                        shader_scan_bytes = word_for("_g_SPXBShaderScanBytes")
                        shader_scan_entries = word_for("_g_SPXBShaderScanEntries")
                        shader_scan_sky_light = word_for("_g_SPXBShaderScanSkyLightSeen")
                        shader_scan_junk_sky = word_for("_g_SPXBShaderScanJunkSkySeen")
                        shader_scan_manifest_active = word_for("_g_SPXBShaderScanManifestActive")
                        shader_scan_manifest_read_len = word_for("_g_SPXBShaderScanManifestReadLen")
                        shader_scan_manifest_count = word_for("_g_SPXBShaderScanManifestCount")
                        shader_scan_raw_bytes = word_for("_g_SPXBShaderScanRawBytes")
                        shader_scan_voyager_listed = word_for("_g_SPXBShaderScanVoyagerListed")
                        shader_scan_voyager_read_len = word_for("_g_SPXBShaderScanVoyagerReadLen")
                        shader_scan_voyager_sky_token = word_for("_g_SPXBShaderScanVoyagerSkyToken")
                        shader_scan_common_read_len = word_for("_g_SPXBShaderScanCommonReadLen")
                        shader_lookup_magic = word_for("_g_SPXBShaderLookupMagic")
                        shader_lookup_count = word_for("_g_SPXBShaderLookupCount")
                        shader_lookup_hash = word_for("_g_SPXBShaderLookupHash")
                        shader_lookup_indexed = word_for("_g_SPXBShaderLookupIndexedFound")
                        shader_lookup_linear = word_for("_g_SPXBShaderLookupLinearFound")
                        shader_lookup_entries = word_for("_g_SPXBShaderLookupEntries")
                        helmet_p1_submitted = word_for("_g_SPXBHelmetP1Submitted")
                        helmet_p2_submitted = word_for("_g_SPXBHelmetP2Submitted")
                        helmet_p1_attached = word_for("_g_SPXBHelmetP1Attached")
                        helmet_p2_attached = word_for("_g_SPXBHelmetP2Attached")
                        helmet_p1_model = word_for("_g_SPXBHelmetP1Model")
                        helmet_p2_model = word_for("_g_SPXBHelmetP2Model")
                        helmet_p1_rf = word_for("_g_SPXBHelmetP1Renderfx")
                        helmet_p2_rf = word_for("_g_SPXBHelmetP2Renderfx")
                        helmet_renderer_refs = word_for("_g_SPXBHelmetRendererRefs")
                        helmet_renderer_surfaces = word_for("_g_SPXBHelmetRendererSurfaces")
                        helmet_renderer_filtered = word_for("_g_SPXBHelmetRendererFiltered")
                        helmet_renderer_last_model = word_for("_g_SPXBHelmetRendererLastModel")
                        helmet_renderer_last_rf = word_for("_g_SPXBHelmetRendererLastRenderfx")
                        helmet_renderer_last_ent = word_for("_g_SPXBHelmetRendererLastEnt")
                        helmet_renderer_last_filter = word_for("_g_SPXBHelmetRendererLastFilter")
                        helmet_renderer_last_surface_model = word_for("_g_SPXBHelmetRendererLastSurfaceModel")
                        helmet_game_p1_ensure = word_for("_g_SPXBHelmetGameP1Ensure")
                        helmet_game_p2_ensure = word_for("_g_SPXBHelmetGameP2Ensure")
                        helmet_game_p1_slot = word_for("_g_SPXBHelmetGameP1Slot")
                        helmet_game_p2_slot = word_for("_g_SPXBHelmetGameP2Slot")
                        helmet_cgame_p1_slot0 = word_for("_g_SPXBHelmetCgameP1Slot0")
                        helmet_cgame_p1_slot1 = word_for("_g_SPXBHelmetCgameP1Slot1")
                        helmet_cgame_p2_slot0 = word_for("_g_SPXBHelmetCgameP2Slot0")
                        helmet_cgame_p2_slot1 = word_for("_g_SPXBHelmetCgameP2Slot1")
                        helmet_bolton_load_len = word_for("_g_SPXBHelmetBoltOnLoadLen")
                        helmet_bolton_count = word_for("_g_SPXBHelmetBoltOnCount")
                        helmet_bolton_index = word_for("_g_SPXBHelmetBoltOnHelmetIndex")
                        helmet_add_attempts = word_for("_g_SPXBHelmetAddAttempts")
                        helmet_add_known_index = word_for("_g_SPXBHelmetAddKnownIndex")
                        helmet_add_fail = word_for("_g_SPXBHelmetAddFailCode")
                        fb_magic = word_for("_g_SPXBFallbackTraceMagic")
                        fb_count = word_for("_g_SPXBFallbackStageCount")
                        fb_shader = word_for("_g_SPXBFallbackLastShaderHash")
                        fb_image = word_for("_g_SPXBFallbackLastImageHash")
                        fb_stage = word_for("_g_SPXBFallbackLastStage")
                        fb_passes = word_for("_g_SPXBFallbackLastPasses")
                        fb_flags = word_for("_g_SPXBFallbackLastFlags")
                        fb_texnum = word_for("_g_SPXBFallbackLastTexnum")
                        fb_lightmap = word_for("_g_SPXBFallbackLastLightmap")
                        fb_state = word_for("_g_SPXBFallbackLastStateBits")
                        fb_indexes = word_for("_g_SPXBFallbackLastIndexes")
                        fb_x = word_for("_g_SPXBFallbackLastX1000")
                        fb_y = word_for("_g_SPXBFallbackLastY1000")
                        fb_z = word_for("_g_SPXBFallbackLastZ1000")
                        sv_probe_magic = word_for("_g_SPXBSVProbeMagic")
                        sv_probe_phase = word_for("_g_SPXBSVProbePhase")
                        sv_probe_subphase = word_for("_g_SPXBSVProbeSubphase")
                        sv_probe_a = word_for("_g_SPXBSVProbeA")
                        sv_probe_b = word_for("_g_SPXBSVProbeB")
                        sv_probe_c = word_for("_g_SPXBSVProbeC")
                        sv_probe_d = word_for("_g_SPXBSVProbeD")
                        loading_title_magic = title_word_for("_g_SPXBLoadingTitleMagic")
                        loading_title_status = title_word_for("_g_SPXBLoadingTitleStatus")
                        loading_title_shader = title_word_for("_g_SPXBLoadingTitleShader")
                        loading_title_draws = title_word_for("_g_SPXBLoadingTitleDraws")
                        loading_title_last_char = title_word_for("_g_SPXBLoadingTitleLastChar")
                        loading_title_map_hash = title_word_for("_g_SPXBLoadingTitleMapHash")
                        loading_title_text_hash = title_word_for("_g_SPXBLoadingTitleTextHash")
                        ui_magic = word_for("_g_SPXBUIStateMagic")
                        ui_started = word_for("_g_SPXBUIStarted")
                        ui_catcher = word_for("_g_SPXBUIKeyCatcher")
                        ui_pause = word_for("_g_SPXBUIPauseActive")
                        ui_qmenu = word_for("_g_SPXBUIQmenuActive")
                        ui_refresh = word_for("_g_SPXBUIRefreshCount")
                        ui_pause_open = word_for("_g_SPXBUIPauseOpenCount")
                        ui_pause_draw = word_for("_g_SPXBUIPauseDrawCount")
                        ui_font_magic = word_for("_g_SPXBUIFontMagic")
                        ui_font_loads = font_word_for("_g_SPXBUIFontLoadAttempts")
                        ui_font_len = font_word_for("_g_SPXBUIFontFileLen")
                        ui_font_loaded = font_word_for("_g_SPXBUIFontLoaded")
                        ui_font_tiny = font_word_for("_g_SPXBUIFontTinyShader")
                        ui_font_medium = font_word_for("_g_SPXBUIFontMediumShader")
                        ui_font_big = font_word_for("_g_SPXBUIFontBigShader")
                        ui_font_draws = font_word_for("_g_SPXBUIFontDrawCalls")
                        ui_font_reject = font_word_for("_g_SPXBUIFontDrawRejectMask")
                        ui_font_char = font_word_for("_g_SPXBUIFontLastChar")
                        ui_font_shader = font_word_for("_g_SPXBUIFontLastShader")
                        ui_font_sx = font_word_for("_g_SPXBUIFontLastSX")
                        ui_font_sy = font_word_for("_g_SPXBUIFontLastSY")
                        ui_font_sw = font_word_for("_g_SPXBUIFontLastSW")
                        mini_soak_magic = word_for("_g_SPXBMiniSoakMagic")
                        mini_soak_stage = word_for("_g_SPXBMiniSoakStage")
                        mini_soak_transitions = word_for("_g_SPXBMiniSoakTransitions")
                        mini_soak_active_msec = word_for("_g_SPXBMiniSoakActiveMsec")
                        mini_soak_flags = word_for("_g_SPXBMiniSoakFlags")
                        if (heartbeat_count != last_fps_heartbeat_count and
                                heartbeat_fps10 > 0):
                            last_fps_heartbeat_count = heartbeat_count
                            fps_sample = heartbeat_fps10 / 10.0
                            wall_fps_sample = None
                            emulation_speed = None
                            previous_wall_sample = last_wall_frame_sample.get(
                                xblog_current_personality)
                            if previous_wall_sample is not None:
                                previous_elapsed, previous_frame, previous_rt = previous_wall_sample
                                wall_delta = elapsed - previous_elapsed
                                frame_delta = (heartbeat_frame - previous_frame) & 0xffffffff
                                rt_delta = (heartbeat_rt - previous_rt) & 0xffffffff
                                if wall_delta > 0.0 and frame_delta < 0x80000000:
                                    wall_fps_sample = frame_delta / wall_delta
                                if wall_delta > 0.0 and rt_delta < 0x80000000:
                                    emulation_speed = rt_delta / (wall_delta * 1000.0)
                            last_wall_frame_sample[xblog_current_personality] = (
                                elapsed, heartbeat_frame, heartbeat_rt)
                            if cls_state == 7:
                                active_fps_samples.append(fps_sample)
                                personality_active_fps_samples.setdefault(
                                    xblog_current_personality, []).append(fps_sample)
                                if wall_fps_sample is not None:
                                    active_wall_fps_samples.append(wall_fps_sample)
                                    personality_active_wall_fps_samples.setdefault(
                                        xblog_current_personality, []).append(wall_fps_sample)
                                if camera_active == 0:
                                    gameplay_fps_samples.append(fps_sample)
                                    personality_gameplay_fps_samples.setdefault(
                                        xblog_current_personality, []).append(fps_sample)
                                    if wall_fps_sample is not None:
                                        gameplay_wall_fps_samples.append(wall_fps_sample)
                                        personality_gameplay_wall_fps_samples.setdefault(
                                            xblog_current_personality, []).append(wall_fps_sample)
                        if args.poll_xblog_perf_only:
                            def detail_log(message):
                                if message.startswith(("xblogperf ",
                                                       "xblogaudio ",
                                                       "xblogrender ",
                                                       "xblogsubmit ",
                                                       "xblogdrawphase ",
                                                       "xblogdrawclass ")):
                                    log(message)
                        elif xblog_current_personality == "default":
                            detail_log = log
                        else:
                            detail_log = lambda _message: None
                        if (xblog_current_personality == "efmp" or
                                args.poll_xblog_perf_only):
                             log("xblog t=%.1f personality=%s boot=0x%08x "
                                 "mirror=%u writes=%u delta=%d hb=0x%08x "
                                  "count=%u frame=%u rt=%u st=%u fps=%.1f wallfps=%s speed=%s cls=%u "
                                  "phase=0x%08x cltail=0x%08x comtail=0x%08x depth=%u "
                                  "cgame=0x%08x/0x%08x input=%u/%u/0x%04x/0x%02x/0x%08x/0x%08x "
                                  "cmd=%u/%u/0x%08x/0x%08x/%u svcmd=%u/%u/0x%08x/0x%08x "
                                  "hmcmd=%u/%u/0x%08x/0x%08x think=%u" %
                                 (elapsed, xblog_current_personality,
                                  boot_phase, mirror_pos, write_count,
                                  delta, heartbeat_magic, heartbeat_count,
                                  heartbeat_frame, heartbeat_rt, heartbeat_st,
                                  heartbeat_fps10 / 10.0,
                                  "%.1f" % wall_fps_sample if wall_fps_sample is not None else "n/a",
                                  "%.3f" % emulation_speed if emulation_speed is not None else "n/a",
                                  cls_state, phase_last, cl_tail_stage,
                                  com_tail_stage, com_frame_depth,
                                  cgame_entry_current, cgame_entry_expected,
                                  input_poll_count, input_port, input_digital,
                                  input_analog, input_lxly, input_rxry,
                                  usercmd_count, usercmd_time, usercmd_move,
                                  usercmd_buttons, usercmd_yaw,
                                  sv_usercmd_count, sv_usercmd_time,
                                  sv_usercmd_move, sv_usercmd_buttons,
                                  hm_getcmd_count, hm_getcmd_time,
                                  hm_getcmd_move, hm_getcmd_buttons,
                                  hm_think_count))
                             log("JA: FRAME_HEARTBEAT completedFrame=%u realtime=%u serverTime=%u fps=%.1f path=%u mem=%u/%u/%u/%u" %
                                 (heartbeat_frame, heartbeat_rt, heartbeat_st,
                                  heartbeat_fps10 / 10.0,
                                  native_draw_path or 1,
                                  heartbeat_mem_used, heartbeat_mem_free,
                                  heartbeat_mem_largest, heartbeat_mem_blocks))
                        if (xblog_current_personality == "efmp" and
                                hm_split_proof_magic == 0x48345046):
                            def hm_part_name(code):
                                return {1: "lower", 2: "upper", 3: "head", 4: "local"}.get(code, "unknown")
                            launch_active = word_array("_g_SPXBHMSplitLaunch", 0)
                            if launch_active:
                                split_enabled = word_array("_g_SPXBHMSplitLaunch", 1)
                                split_players = word_array("_g_SPXBHMSplitLaunch", 2)
                                local_players = word_array("_g_SPXBHMSplitLaunch", 3)
                                virtual_enabled = word_array("_g_SPXBHMSplitLaunch", 4)
                                virtual_p1 = word_array("_g_SPXBHMSplitLaunch", 5)
                                economy = word_array("_g_SPXBHMSplitLaunch", 6)
                                source_code = word_array("_g_SPXBHMSplitLaunch", 7)
                                launch_source = {1: "xbe", 2: "menu", 3: "direct"}.get(source_code, "unknown")
                                log("STEFX_HM_SPLIT_LAUNCH: source=%s map='%s' split=%u players=%u mode='holomatch' localPlayers=%u virtual=%u virtualP1=%u economy=%u" %
                                    (launch_source, args.proof_map or "hm_borg1", split_enabled,
                                     split_players, local_players, virtual_enabled,
                                     virtual_p1, economy))
                            bot_frames = word_array("_g_SPXBHMSplitBotProof", 0)
                            if bot_frames:
                                log("STEFX_HM_SPLIT_BOT: frames=%u checks=%u min=%u gameType=%u maxClients=%u humans=%u bots=%u requests=%u queued=%u allocAttempts=%u allocClient=%d addAttempts=%u info=%u connect=%u begins=%u active=%u setupAttempts=%u setupStage=%u character=%u goalState=%u itemErr=%u weaponState=%u weaponErr=%u chatErr=%u probeLen=%d probeHandle=%u path0=0x%08x path1=0x%08x parseCalls=%u parseStage=%u parseDetail=0x%08x parseSkill=%d" %
                                    tuple(word_array("_g_SPXBHMSplitBotProof", index)
                                          if index not in (10, 24, 31) else
                                          (signed32(word_array("_g_SPXBHMSplitBotProof", index)) - 1
                                           if index == 10 else
                                           signed32(word_array("_g_SPXBHMSplitBotProof", index)))
                                          for index in range(32)))
                            if hm_split_render_armed:
                                log("STEFX_HM_SPLIT_RENDER: armed players=%u source=0,0 640x480 gl=0,0 640x480 fov=90/73" %
                                    hm_split_render_armed)
                            for proof_slot in range(4):
                                state_serial = word_array("_g_SPXBHMSplitStateSerial", proof_slot)
                                if state_serial:
                                    state_flags = word_array("_g_SPXBHMSplitStateFlags", proof_slot)
                                    log("STEFX_HM_SPLIT_STATE: slot=%u players=%u bots=%u state=%u local=%u bot=%u svFlags=0x0 health=%d weapon=%u area=0 cluster=0 p1Area=0 p1Cluster=0 p1Pvs=1 p1Dist=%u origin=(%d,%d,%d) view=(%d,%d,%d) time=%u sample=%u interval=500" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitStatePlayers", proof_slot),
                                         word_array("_g_SPXBHMSplitStateBots", proof_slot),
                                         word_array("_g_SPXBHMSplitStateClientState", proof_slot),
                                         1 if (state_flags & 1) else 0,
                                         1 if (state_flags & 2) else 0,
                                         signed32(word_array("_g_SPXBHMSplitStateHealth", proof_slot)),
                                         word_array("_g_SPXBHMSplitStateWeapon", proof_slot),
                                         word_array("_g_SPXBHMSplitStateP1Dist", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitStateOriginX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitStateOriginY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitStateOriginZ", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitStateViewPitch", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitStateViewYaw", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitStateViewRoll", proof_slot)),
                                         word_array("_g_SPXBHMSplitStateTime", proof_slot),
                                         state_serial))
                                    collision_base = proof_slot * 12
                                    if word_array("_g_SPXBHMSplitCollision", collision_base):
                                        log("STEFX_HM_SPLIT_COLLISION: slot=%u cmClusters=%u inlineModels=%u leaf=%d area=%d cluster=%d originContents=0x%x viewContents=0x%x entContents=0x%x linked=%u pmType=%d ground=%d origin=(%d,%d,%d) view=(%d,%d,%d)" %
                                            (proof_slot,
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 1),
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 2),
                                             signed32(word_array("_g_SPXBHMSplitCollision", collision_base + 3)),
                                             signed32(word_array("_g_SPXBHMSplitCollision", collision_base + 4)),
                                             signed32(word_array("_g_SPXBHMSplitCollision", collision_base + 5)),
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 6),
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 7),
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 8),
                                             word_array("_g_SPXBHMSplitCollision", collision_base + 9),
                                             signed32(word_array("_g_SPXBHMSplitCollision", collision_base + 10)),
                                             signed32(word_array("_g_SPXBHMSplitCollision", collision_base + 11)),
                                             signed32(word_array("_g_SPXBHMSplitStateOriginX", proof_slot)),
                                             signed32(word_array("_g_SPXBHMSplitStateOriginY", proof_slot)),
                                             signed32(word_array("_g_SPXBHMSplitStateOriginZ", proof_slot)),
                                             signed32(word_array("_g_SPXBHMSplitRefdefX", proof_slot)),
                                             signed32(word_array("_g_SPXBHMSplitRefdefY", proof_slot)),
                                             signed32(word_array("_g_SPXBHMSplitRefdefZ", proof_slot))))
                                if word_array("_g_SPXBHMSplitCmdSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_CMD: client=%u time=%u move=(%d,%d,%d) buttons=0x%x weapon=%u angles=(%d,%d,%d) monitor=1" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitCmdTime", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitCmdMoveX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitCmdMoveY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitCmdMoveZ", proof_slot)),
                                         word_array("_g_SPXBHMSplitCmdButtons", proof_slot),
                                         word_array("_g_SPXBHMSplitCmdWeapon", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitCmdAnglePitch", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitCmdAngleYaw", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitCmdAngleRoll", proof_slot))))
                                if proof_slot > 0 and word_array("_g_SPXBHMSplitRefdefSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_REFDEF: slot=%u client=%u time=%u origin=(%d,%d,%d) angles=(%d,%d,%d)" %
                                        (proof_slot, proof_slot,
                                         word_array("_g_SPXBHMSplitStateTime", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitRefdefX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefZ", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefPitch", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefYaw", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefRoll", proof_slot))))
                                if proof_slot > 0 and word_array("_g_SPXBHMSplitSnapshotSerial", proof_slot):
                                    snap_before = word_array("_g_SPXBHMSplitSnapshotBefore", proof_slot)
                                    snap_after = word_array("_g_SPXBHMSplitSnapshotAfter", proof_slot)
                                    snap_added = word_array("_g_SPXBHMSplitSnapshotAdded", proof_slot)
                                    log("STEFX_HM_SPLIT_SNAPSHOT: slot=%u entsBefore=%u entsAfter=%u added=%d areaBytes=1 view=(%d,%d,%d) state=%u local=1" %
                                        (proof_slot, snap_before, snap_after,
                                         signed32(snap_added),
                                         signed32(word_array("_g_SPXBHMSplitRefdefX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRefdefZ", proof_slot)),
                                         word_array("_g_SPXBHMSplitStateClientState", proof_slot)))
                                if word_array("_g_SPXBHMSplitRenderSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_RENDER: slot=%u external=%u externalClient=%d drawBase=0 ref=%u,%u %ux%u gl=%u,%u %ux%u fov=90/53 view=(%d,%d,%d) pvs=(%d,%d,%d)" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitRenderExternal", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitRenderClient", proof_slot)),
                                         word_array("_g_SPXBHMSplitRenderRectX", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectY", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectW", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectH", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectX", proof_slot),
                                         480 - word_array("_g_SPXBHMSplitRenderRectY", proof_slot) - word_array("_g_SPXBHMSplitRenderRectH", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectW", proof_slot),
                                         word_array("_g_SPXBHMSplitRenderRectH", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewZ", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewZ", proof_slot))))
                                if word_array("_g_SPXBHMSplitRenderDoneSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_RENDER_DONE: slot=%u external=%u externalClient=%d drawDelta=%d drawAfter=%u cluster=%d cluster2=-1 view=(%d,%d,%d)" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitRenderExternal", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitRenderClient", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderDrawDelta", proof_slot)),
                                         word_array("_g_SPXBHMSplitRenderDrawAfter", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitRenderCluster", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitRenderViewZ", proof_slot))))
                                if word_array("_g_SPXBHMSplitHudSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_HUD: slot=%u players=4 shared=0 shader=1 src=(0,0 640x480) dst=(%u,%u %ux%u)" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitHudRectX", proof_slot),
                                         word_array("_g_SPXBHMSplitHudRectY", proof_slot),
                                         word_array("_g_SPXBHMSplitHudRectW", proof_slot),
                                         word_array("_g_SPXBHMSplitHudRectH", proof_slot)))
                                if word_array("_g_SPXBHMSplitHudStatusSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_HUD_STATUS: slot=%u players=4 valid=%u health=%d weapon=%u score=%d dst=(%u,%u %ux%u)" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitHudStatusValid", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitHudStatusHealth", proof_slot)),
                                         word_array("_g_SPXBHMSplitHudStatusWeapon", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitHudStatusScore", proof_slot)),
                                         word_array("_g_SPXBHMSplitHudStatusRectX", proof_slot),
                                         word_array("_g_SPXBHMSplitHudStatusRectY", proof_slot),
                                         word_array("_g_SPXBHMSplitHudStatusRectW", proof_slot),
                                         word_array("_g_SPXBHMSplitHudStatusRectH", proof_slot)))
                                if word_array("_g_SPXBHMSplitViewWeaponSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_VIEWWEAPON: slot=%u client=%d weapon=%u added=%u source=cgame renderfx=0x%x" %
                                        (proof_slot,
                                         signed32(word_array("_g_SPXBHMSplitViewWeaponClient", proof_slot)),
                                         word_array("_g_SPXBHMSplitViewWeaponWeapon", proof_slot),
                                         word_array("_g_SPXBHMSplitViewWeaponAdded", proof_slot),
                                         word_array("_g_SPXBHMSplitViewWeaponRenderfx", proof_slot)))
                                if word_array("_g_SPXBHMSplitPhaserWorldHidden", proof_slot):
                                    log("STEFX_HM_SPLIT_PHASER_FILTER: slot=%u hidden=%u source=world-beam" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitPhaserWorldHidden", proof_slot)))
                                if (word_array("_g_SPXBHMSplitPhaserBridgeFP", proof_slot) or
                                        word_array("_g_SPXBHMSplitPhaserBridgeWorld", proof_slot) or
                                        word_array("_g_SPXBHMSplitPhaserBridgeLineFP", proof_slot)):
                                    log("STEFX_HM_SPLIT_PHASER_BRIDGE: slot=%u fp=%u world=%u fpLines=%u lastNumber=0x%08x" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitPhaserBridgeFP", proof_slot),
                                         word_array("_g_SPXBHMSplitPhaserBridgeWorld", proof_slot),
                                         word_array("_g_SPXBHMSplitPhaserBridgeLineFP", proof_slot),
                                         word_array("_g_SPXBHMSplitPhaserBridgeLastNumber", proof_slot)))
                                if word_array("_g_SPXBHMSplitPhaserFPSerial", proof_slot):
                                    log("STEFX_HM_SPLIT_PHASER_MUZZLE: slot=%u serial=%u renderfx=0x%x start=(%d,%d,%d) view=(%d,%d,%d)" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitPhaserFPSerial", proof_slot),
                                         word_array("_g_SPXBHMSplitPhaserFPRenderfx", proof_slot),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPStartX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPStartY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPStartZ", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPViewX", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPViewY", proof_slot)),
                                         signed32(word_array("_g_SPXBHMSplitPhaserFPViewZ", proof_slot))))
                            for proof_slot in range(4):
                                if hm_split_fp_filter_mask & (1 << proof_slot):
                                    log("STEFX_HM_SPLIT_FP_FILTER: slot=%u entity=0 renderfx=0x0 hModel=0 ownerSlot=0" % proof_slot)
                                if hm_split_self_filter_mask & (1 << proof_slot):
                                    log("STEFX_HM_SPLIT_SELF_FILTER: slot=%u entity=0 refNumber=%u renderfx=0x0 hModel=0 modelPart=%s origin=(0,0,0) view=(0,0,0) xyDist=0 zDelta=0" %
                                        (proof_slot,
                                         word_array("_g_SPXBHMSplitSelfFilterRefNumber", proof_slot),
                                         hm_part_name(word_array("_g_SPXBHMSplitSelfFilterPart", proof_slot))))
                            if hm_split_hud_divider:
                                log("STEFX_HM_SPLIT_HUD_DIVIDER: players=4 vertical=(318,0 4x480) horizontal=(0,238 640x4)")
                        if (xblog_current_personality == "efmp" and
                                not args.poll_xblog_perf_only):
                            log("xbloginput t=%.1f edge=%u/0x%08x common=%u/0x%08x frontend=%u/0x%08x dispatch=%u/%u/0x%08x" %
                                (elapsed, input_menu_edges, input_menu_edge_last,
                                 input_common_presses, input_common_press_last,
                                 input_frontend_queues, input_frontend_queue_last,
                                 input_dispatches, input_dispatch_handled,
                                 input_dispatch_last))
                            log("xblogscore t=%.1f info=%u/0x%08x gamecmd=%u/%u console=%u/%u scores=%u/%u draw=%u/0x%08x" %
                                (elapsed, hm_info_dispatches, hm_info_dispatch_last,
                                 hm_game_commands, hm_game_command_result,
                                 hm_console_commands, hm_console_command_tag,
                                 hm_scores_down, hm_scores_up,
                                 hm_score_draws, hm_score_draw_state))
                            log("xblogtext t=%.1f len=%u parsed=%u state=0x%08x" %
                                (elapsed, hm_text_length, hm_text_count, hm_text_state))
                            log("xblogtexttrace t=%.1f calls=%u stage=%u open=%u/%u raw=0x%08x parsed=0x%08x score=0x%08x first=0x%08x" %
                                (elapsed, hm_text_trace_calls,
                                 hm_text_trace_stage,
                                 hm_text_trace_open_length,
                                 hm_text_trace_handle,
                                 hm_text_trace_raw,
                                 hm_text_trace_parsed,
                                 hm_text_trace_score,
                                 hm_text_trace_pointer))
                            if hm_text_trace_va is not None:
                                trace_words = monitor_read_virtual_words(
                                    sock, hm_text_trace_va, 8)
                                trace_bss_words = monitor_read_virtual_words(
                                    sock, hm_text_trace_va + 0x1000, 8)
                                log("xblogtexttraceva t=%.1f va=0x%08x nominal=%s bss=%s" %
                                    (elapsed, hm_text_trace_va,
                                     "/".join("%08x" % value for value in trace_words),
                                     "/".join("%08x" % value for value in trace_bss_words)))
                            if hm_ingame_text_va is not None and hm_text_count:
                                text_slots = (
                                    (1, "outofammo"), (2, "lowammo"),
                                    (18, "score"), (19, "ping"),
                                    (20, "time"), (21, "name"),
                                    (29, "freeforall"), (116, "rank"),
                                    (178, "players"),
                                )
                                text_pointers = monitor_read_virtual_words(
                                    sock, hm_ingame_text_va, 185)
                                slot_values = []
                                for text_index, text_label in text_slots:
                                    text_pointer = (
                                        text_pointers[text_index]
                                        if text_index < len(text_pointers) else 0)
                                    text_value = monitor_read_virtual_cstring(
                                        sock, text_pointer, 64)
                                    slot_values.append(
                                        "%s[%u]=0x%08x:%r" %
                                        (text_label, text_index, text_pointer,
                                         text_value))
                                log("xblogtextslots t=%.1f %s" %
                                    (elapsed, " ".join(slot_values)))
                                source_value = monitor_read_virtual_cstring(
                                    sock, hm_ingame_source_va, 96)
                                log("xblogtextarena t=%.1f source=%r" %
                                    (elapsed, source_value))
                            log("xblogrcmd t=%.1f high=%u drops=%u last=0x%08x calls=%u issues=%u used=%u" %
                                (elapsed, render_command_high_water,
                                 render_command_drops, render_command_last_drop,
                                 render_command_calls, render_command_issue_count,
                                 render_command_last_used))
                            log("xblogscorepic t=%.1f count=%u shader=%u xywh=%08x/%08x/%08x/%08x "
                                "scale=%08x/%08x white=%u queued=%u/0x%08x backend=%u color=0x%08x" %
                                (elapsed, hm_score_stretch_count, hm_score_stretch_shader,
                                 hm_score_stretch_x, hm_score_stretch_y,
                                 hm_score_stretch_w, hm_score_stretch_h,
                                 hm_score_scale_x, hm_score_scale_y,
                                 hm_score_white_shader, hm_score_queued_count,
                                 hm_score_queued_shader, hm_score_backend_matches,
                                 hm_score_backend_color))
                            log("xblogrcmdback t=%.1f commands=%u stretches=%u terminal=0x%08x bytes=%u "
                                "scoregeom=%u/0x%08x/0x%08x" %
                                (elapsed, render_backend_commands,
                                 render_backend_stretches, render_backend_terminal,
                                 render_backend_bytes, hm_score_backend_geometry,
                                 hm_score_backend_geom_shader,
                                 hm_score_backend_geom_color))
                            log("xblogrcmddone t=%.1f commands=%u stretches=%u terminal=0x%08x bytes=%u "
                                "scoregeom=%u/0x%08x/0x%08x" %
                                (elapsed, render_backend_done_commands,
                                 render_backend_done_stretches,
                                 render_backend_done_terminal,
                                 render_backend_done_bytes,
                                 hm_score_backend_done_geometry,
                                 hm_score_backend_done_shader,
                                 hm_score_backend_done_color))
                            log("xblogscoresubmit t=%.1f pending=%u surface=0x%08x/%u/%u/%u/0x%08x "
                                "armed=%u submit=%u/%u/0x%08x/0x%08x scissor=%u/%08x/%08x" %
                                (elapsed, hm_score_batch_pending,
                                 hm_score_surface_flags, hm_score_surface_verts,
                                 hm_score_surface_indexes, hm_score_surface_passes,
                                 hm_score_surface_state, hm_score_submit_armed,
                                 hm_score_submit_calls, hm_score_submit_indexes,
                                 hm_score_submit_state, hm_score_submit_texture,
                                 hm_score_submit_scissor, hm_score_submit_scissor_xy,
                                 hm_score_submit_scissor_wh))
                            log("xblogscorestate t=%.1f shader=0x%08x image=%u white=%u wh=%08x "
                                "bounds=%08x/%08x target=0x%08x color=0x%08x cull=%u blend=%08x "
                                "viewport=%08x/%08x proj=%08x/%08x/%08x/%08x" %
                                (elapsed, hm_score_shader_flags, hm_score_image_tex,
                                 hm_score_white_tex, hm_score_image_wh,
                                 hm_score_surface_min_xy, hm_score_surface_max_xy,
                                 hm_score_submit_target, hm_score_submit_color_write,
                                 hm_score_submit_cull, hm_score_submit_blend,
                                 hm_score_submit_viewport_xy, hm_score_submit_viewport_wh,
                                 hm_score_submit_proj00, hm_score_submit_proj11,
                                 hm_score_submit_proj30, hm_score_submit_proj31))
                            log("xblogscorepipe t=%.1f texture=%08x/%u/%08x/%08x "
                                "stage=%08x/%08x depth=%08x shaders=%08x/%08x "
                                "vertex=%u/%u xyzw=%08x/%08x/%08x/%08x "
                                "color=%08x uv=%08x/%08x index012=%08x" %
                                (elapsed, hm_score_texture_data0, hm_score_texture_size,
                                 hm_score_texture_wh, hm_score_texture_format,
                                 hm_score_stage_color, hm_score_stage_alpha,
                                 hm_score_depth_state, hm_score_vertex_shader,
                                 hm_score_pixel_shader, hm_score_vertex_count,
                                 hm_score_vertex_stride, hm_score_vertex0_x,
                                 hm_score_vertex0_y, hm_score_vertex0_z,
                                 hm_score_vertex0_w, hm_score_vertex0_color,
                                 hm_score_vertex0_u, hm_score_vertex0_v,
                                 hm_score_index012))
                            log("xblogsky t=%.1f magic=0x%08x outer=%u/%u/%u draw=%u passes=%u sort=%u "
                                "resolve=0x%08x/%u shader=%u map=0x%08x resolved=0x%08x surf=0x%08x "
                                "default=%u explicit=%u hasSky=%u resolvePasses=%u sort1000=%d lm0=%d" %
                                (elapsed, sky_magic, sky_present, sky_fallback, sky_tex,
                                 sky_draw, sky_passes, sky_sort,
                                 skyres_magic, skyres_count, skyres_shader_num,
                                 skyres_map_hash, skyres_resolved_hash,
                                 skyres_surface_flags, skyres_default,
                                 skyres_explicit, skyres_has_sky,
                                 skyres_passes, signed32(skyres_sort_x1000),
                                 signed32(skyres_lightmap0)))
                        detail_log("xblogcomtail t=%.1f stage=0x%08x cgame=0x%08x/0x%08x frameDepth=%u catches=%u "
                                   "mainStage=0x%08x cbufDepth=%u return=0x%08x/0x%08x" %
                                   (elapsed, com_tail_stage, cgame_entry_current,
                                    cgame_entry_expected, com_frame_depth,
                                    com_catch_count, main_tail_stage,
                                   cbuf_exec_depth, cbuf_return_entry,
                                   cbuf_return_exit))
                        detail_log("xblogpacked t=%.1f mapPhase=%u packedPhase=0x%08x mapHash=0x%08x" %
                                   (elapsed, map_phase, packed_map_phase, map_hash))
                        detail_log("xblog t=%.1f boot=0x%08x mirror=%u writes=%u delta=%d hb=0x%08x count=%u frame=%u rt=%u st=%u fps=%.1f main=%u com=%u sv=%u cl=%u cls=%u clst=%u clsfr=%u phase=0x%08x cltail=0x%08x sub=%u spin=%u msec=%u ctime=%u ltime=%u cbuf=%u cmd=%u cmdp=%u cmdh=0x%08x argc=%u mapp=%u maph=0x%08x gamep=%u ents=%u be=%u prim=%u verts=%u state=%u split=%u/%u/%u/%u final=%u flush=%u splitSlot=%u draw=%u/%u world=%u/%u retry=%u fallback=%u cluster=%d/%d mark=%d/%d pvsrej=%u/%u arearej=%u/%u root=%d/%d surf=%u/%u/%u/%u/%u/%u/%u/%u p2=%u trace=%u view=%d/%d/%d ps=%d/%d/%d cur=%d/%d/%d ang=%d/%d cam=%u p1trace=%u p1loc=%d/%d/%d p2loc=%d/%d/%d diff=%d/%d/%d p2dbg=ref=%u scene=%u/%u/%u model=%u/%u/%u/%u h=%u/%u/%u rf=0x%08x renderer=%u/%u/0x%08x/%d vw=%u/%u/%u/%u model=%u/%u rf=0x%08x/0x%08x rend=%u/%u filt=%u/%u skip=%u/%u wreg=%u/0x%08x/0x%08x/0x%08x/%u/%u/%u/%u wload=%u/%u/%u/%u/0x%08x/0x%08x/%u/0x%08x wm=%u/0x%08x/%u/%u/0x%08x/%u/%u/%u/%u/%u sky=0x%08x/%u/%u/%u/%u/%u/%u direct=%u/0x%08x/%u svp=0x%08x/0x%08x/%u/%u/%u/%u/%u" %
                            (elapsed, boot_phase, mirror_pos, write_count, delta,
                             heartbeat_magic, heartbeat_count, heartbeat_frame,
                             heartbeat_rt, heartbeat_st, heartbeat_fps10 / 10.0,
                             main_loop_count, com_frame_count, sv_frame_count,
                             cl_frame_count, cls_state, cl_server_time, cls_frame_count,
                             phase_last, cl_tail_stage, com_subphase, com_spin_count, com_msec,
                             com_frame_time, com_last_time, cbuf_exec_count,
                             cmd_exec_count, cmd_phase, cmd_hash, cmd_argc,
                             map_phase, map_hash, game_phase, game_entity_count,
                             render_backend, primitive_calls, primitive_verts,
                             state_flushes, split_shader, split_fog, split_dlight,
                             split_entity, split_final, split_flush,
                             split_slot_active, split_slot0_draw, split_slot1_draw,
                             split_slot0_world, split_slot1_world,
                             split_slot1_retry, split_slot1_fallback,
                             signed32(split_slot0_cluster), signed32(split_slot1_cluster),
                             signed32(split_slot0_marked), signed32(split_slot1_marked),
                             split_slot0_pvsrej, split_slot1_pvsrej,
                             split_slot0_arearej, split_slot1_arearej,
                             signed32(split_slot0_rootvis), signed32(split_slot1_rootvis),
                             split_slot0_attempt, split_slot1_attempt,
                             split_slot0_culled, split_slot1_culled,
                             split_slot0_already, split_slot1_already,
                             split_slot0_added, split_slot1_added,
                             split_p2_ent, split_p2_trace,
                              signed32(split_p2_view_x), signed32(split_p2_view_y), signed32(split_p2_view_z),
                              signed32(split_p2_ps_x), signed32(split_p2_ps_y), signed32(split_p2_ps_z),
                              signed32(split_p2_cur_x), signed32(split_p2_cur_y), signed32(split_p2_cur_z),
                              signed32(split_p2_pitch), signed32(split_p2_yaw),
                              split_camera_mode, split_p1_trace,
                              signed32(split_p1_local_x), signed32(split_p1_local_y), signed32(split_p1_local_z),
                              signed32(split_p2_local_x), signed32(split_p2_local_y), signed32(split_p2_local_z),
                              signed32(split_local_diff_x), signed32(split_local_diff_y), signed32(split_local_diff_z),
                              split_p2_refdef,
                              split_p2_scene_considered, split_p2_scene_added, split_p2_scene_self,
                              split_p2_model_enter, split_p2_model_return, split_p2_model_info,
                              split_p2_model_submitted,
                              split_p2_model_legs, split_p2_model_torso, split_p2_model_head,
                              split_p2_model_renderfx,
                              split_p2_renderer_refs, split_p2_renderer_model,
                              split_p2_renderer_renderfx, signed32(split_p2_renderer_z),
                              vw_p1_adds, vw_p2_adds, vw_p1_skips, vw_p2_skips,
                              vw_p1_model, vw_p2_model,
                              vw_p1_rf, vw_p2_rf,
                               vw_p1_render_adds, vw_p2_render_adds,
                               vw_p1_render_filtered, vw_p2_render_filtered,
                               vw_p1_last_skip, vw_p2_last_skip,
                               weapon_reg_weapon, weapon_reg_hash,
                               weapon_reg_first4, weapon_reg_class_hash,
                               weapon_reg_view, weapon_reg_world, weapon_reg_hands,
                               weapon_reg_fail, weapon_load_stage, weapon_load_read_len,
                               weapon_load_type_weapon, weapon_load_model_weapon,
                               weapon_load_model_hash, weapon_load_slot4_hash,
                               weapon_load_slot4_ammo, weapon_load_slot4_first4,
                               weapon_model_stage, weapon_model_hash,
                               weapon_model_disk_len, weapon_model_disk_success,
                               weapon_model_ident, weapon_model_version, weapon_model_size,
                               weapon_model_loaded, weapon_model_handle, weapon_model_fail,
                               sky_magic, sky_present, sky_fallback, sky_tex,
                               sky_draw, sky_passes, sky_sort,
                             direct_status, direct_hash, direct_queued,
                             sv_probe_magic, sv_probe_phase, sv_probe_subphase,
                             sv_probe_a, sv_probe_b, sv_probe_c, sv_probe_d))
                        detail_log("xblogui t=%.1f magic=0x%08x started=%u catcher=0x%08x pause=%u qmenu=%u refresh=%u open=%u draw=%u" %
                            (elapsed, ui_magic, ui_started, ui_catcher, ui_pause,
                             ui_qmenu, ui_refresh, ui_pause_open, ui_pause_draw))
                        detail_log("xbloginput t=%.1f edge=%u/0x%08x common=%u/0x%08x frontend=%u/0x%08x dispatch=%u/%u/0x%08x" %
                            (elapsed, input_menu_edges, input_menu_edge_last,
                             input_common_presses, input_common_press_last,
                             input_frontend_queues, input_frontend_queue_last,
                             input_dispatches, input_dispatch_handled,
                             input_dispatch_last))
                        workload_passes = (float(workload_total_indexes) /
                                           float(workload_indexes)
                                           if workload_indexes else 0.0)
                        detail_log("xblogwork t=%.1f surfaces=%u batches=%u verts=%u indexes=%u totalIndexes=%u passes=%.2f" %
                                   (elapsed, workload_surfaces, workload_batches,
                                    workload_vertexes, workload_indexes,
                                    workload_total_indexes, workload_passes))
                        detail_log("xblogperf t=%.1f frame=%u server=%u client=%u game=%u frontend=%u backend=%u audio=%u camera=%u ticks=%u lastGame=%u maxGame=%u screen=%u/%u gamePhases=%u/%u/%u entities=%u categories=missile:%u item:%u mover:%u client:%u think:%u script:%u other:%u" %
                            (elapsed, perf_frame, perf_server, perf_client,
                             perf_game, perf_frontend, perf_backend, perf_audio,
                             camera_active, perf_server_ticks,
                             perf_server_last_game, perf_server_max_game,
                             perf_screen_draw, perf_end_frame,
                             perf_game_pre, perf_game_entities, perf_game_post,
                             perf_game_entities_visited, perf_game_missiles,
                             perf_game_items, perf_game_movers, perf_game_clients,
                             perf_game_think_due, perf_game_scripted,
                             perf_game_other))
                        detail_log("xblogaudio t=%.1f backend=0x%08x begin=%u register=%u start=%u local=%u loop=%u respat=%u listener=0x%08x listenerMask=0x%08x voice=%u lipActive=%u last=0x%08x/%u" %
                            (elapsed, hm_audio_backend,
                             hm_audio_begin_registration, hm_audio_register,
                             hm_audio_start, hm_audio_local, hm_audio_loop,
                             hm_audio_respatialize, hm_audio_listener,
                             hm_audio_listener_mask, hm_audio_voice, hm_audio_lip,
                             hm_audio_last_ent_chan, hm_audio_last_handle))
                        detail_log("xblogrender t=%.1f sample=%u active=%u total=%u setup=%u mark=%u world=%u polys=%u projection=%u entities=%u sort=%u debug=%u views=%u portals=%u drawSurfs=%u refEntities=%u leafs=%u inputSurfs=%u batches=%u submits=%u verts=%u indexes=%u totalIndexes=%u backendPhases=%u/%u/%u wait=%u/%u drawCycles=%u/%u/%u/%u/%u/%u" %
                            (elapsed, perf_sample_serial, perf_sample_active,
                             perf_render_total, perf_render_setup,
                             perf_render_mark, perf_render_world,
                             perf_render_polys, perf_render_projection,
                             perf_render_entities, perf_render_sort,
                             perf_render_debug, perf_render_views,
                             perf_render_portals, perf_render_draw_surfs,
                             perf_render_ref_entities, perf_render_leafs,
                             perf_backend_surfaces, perf_backend_batches,
                             perf_submit_calls, perf_backend_vertexes,
                             perf_backend_indexes, perf_backend_total_indexes,
                             perf_backend_draw_surfs, perf_backend_swap,
                             perf_backend_other,
                             perf_finish_msec, perf_present_msec,
                             perf_draw_cycles, perf_draw_state_cycles,
                             perf_draw_reserve_cycles, perf_draw_pack_cycles,
                             perf_draw_index_cycles, perf_draw_submit_cycles))
                        detail_log("xblogsubmit t=%.1f indexed=%u immediate=%u tex1=%u reserveDwords=%u/%u" %
                            (elapsed, perf_indexed_submits,
                             perf_immediate_submits, perf_indexed_tex1,
                             perf_indexed_dwords, perf_immediate_dwords))
                        detail_log("xblogdrawphase t=%.1f setStream=%u beginPush=%u pointer=%u" %
                            (elapsed, perf_draw_set_stream_cycles,
                             perf_draw_begin_push_cycles,
                             perf_draw_pointer_cycles))
                        detail_log("xblogdrawclass t=%.1f opaque=%u blend=%u/%u alphaTest=%u/%u noDepthWrite=%u/%u noDepthTest=%u twoSided=%u/%u beginPushMax=%u/%u over=%u/%u/%u maxState=0x%08x" %
                            (elapsed, perf_indexed_opaque,
                             perf_indexed_blend, perf_indexed_blend_indexes,
                             perf_indexed_alpha_test,
                             perf_indexed_alpha_test_indexes,
                             perf_indexed_no_depth_write,
                             perf_indexed_no_depth_write_indexes,
                             perf_indexed_no_depth_test,
                             perf_indexed_two_sided,
                             perf_indexed_two_sided_indexes,
                             perf_begin_push_max_cycles,
                             perf_begin_push_max_dwords,
                             perf_begin_push_over_100k,
                             perf_begin_push_over_1ms,
                             perf_begin_push_over_10ms,
                             perf_begin_push_max_state))
                        detail_log("xblognative t=%.1f path=%u mode=%u count=%u sourceVerts=%u indexRange=%u..%u stride=%u shader=0x%08x vbOff=%u ibOff=%u vbBytes=%u ibBytes=%u locks=0x%08x indices=0x%08x vertices=0x%08x" %
                            (elapsed, native_draw_path, native_draw_mode,
                             native_draw_count, native_draw_source_vertices,
                             native_draw_min_index, native_draw_max_index,
                             native_draw_stride, native_draw_shader,
                             native_draw_vertex_offset,
                             native_draw_index_offset,
                             native_draw_vertex_bytes,
                             native_draw_index_bytes,
                             native_draw_lock_flags,
                              native_draw_indices, native_draw_vertices))
                        detail_log("xblogmt t=%.1f attempts=%u draws=%u ready=%u mismatch=%u drawFailures=%u stage1=%u failures=%u" %
                            (elapsed, native_multitex_attempts,
                             native_multitex_draws, native_multitex_ready,
                             native_multitex_mismatch,
                             native_indexed_draw_failures,
                             native_stage1_applies,
                             native_stage1_failures))
                        detail_log("xbloglm t=%.1f draws=%u bundle0=%u bundle1=%u env=0x%08x tex0=%u tex1=%u flags=0x%08x" %
                            (elapsed, lightmap_multitex_draws,
                             lightmap_bundle0_draws, lightmap_bundle1_draws,
                             lightmap_last_env, lightmap_last_tex0,
                             lightmap_last_tex1, lightmap_last_flags))
                        detail_log("xbloglmu t=%.1f uploads=%u source=%u..%u avg=%u encoded=%u..%u avg=%u checksum=0x%08x format=0x%08x size=%ux%u" %
                            (elapsed, lightmap_upload_count,
                             lightmap_upload_source_minmax & 0xffff,
                             (lightmap_upload_source_minmax >> 16) & 0xffff,
                             lightmap_upload_source_avg,
                             lightmap_upload_encoded_minmax & 0xffff,
                             (lightmap_upload_encoded_minmax >> 16) & 0xffff,
                             lightmap_upload_encoded_avg,
                             lightmap_upload_checksum,
                             lightmap_upload_format,
                             lightmap_upload_size & 0xffff,
                             (lightmap_upload_size >> 16) & 0xffff))
                        detail_log("xblogmodel t=%.1f stage=%u hash=0x%08x name=0x%08x len=%d" %
                            (elapsed, model_probe_stage, model_probe_hash,
                             model_probe_name, signed32(model_probe_len)))
                        detail_log("xblogfilealloc t=%.1f stage=0x%02x hash=0x%08x path=0x%08x len=%u tag=%u mutex=0x%08x wait=0x%08x release=0x%08x" %
                            (elapsed, file_alloc_stage, file_alloc_hash,
                             file_alloc_path, file_alloc_len, file_alloc_tag,
                             file_alloc_mutex, file_alloc_wait,
                             file_alloc_release))
                        detail_log("xblogborg t=%.1f plugged=count=%u ent=%u flags=0x%08x anim=%u/%u models=%u/%u/%u skins=%u/%u/%u names=%08x/%08x/%08x active=count=%u ent=%u flags=0x%08x anim=%u/%u models=%u/%u/%u skins=%u/%u/%u names=%08x/%08x/%08x" %
                            (elapsed,
                             borg_plugged_count, borg_plugged_ent,
                             borg_plugged_spawnflags,
                             borg_plugged_anim & 0xffff,
                             (borg_plugged_anim >> 16) & 0xffff,
                             borg_plugged_legs_model, borg_plugged_torso_model,
                             borg_plugged_head_model, borg_plugged_legs_skin,
                             borg_plugged_torso_skin, borg_plugged_head_skin,
                             borg_plugged_legs_hash, borg_plugged_torso_hash,
                             borg_plugged_head_hash,
                             borg_active_count, borg_active_ent,
                             borg_active_spawnflags,
                             borg_active_anim & 0xffff,
                             (borg_active_anim >> 16) & 0xffff,
                             borg_active_legs_model, borg_active_torso_model,
                             borg_active_head_model, borg_active_legs_skin,
                             borg_active_torso_skin, borg_active_head_skin,
                             borg_active_legs_hash, borg_active_torso_hash,
                             borg_active_head_hash))
                        detail_log("xblogfont t=%.1f magic=0x%08x loads=%u len=%u loaded=%u shaders=%u/%u/%u draws=%u reject=0x%08x last=%u/%u/%u/%u/%u" %
                            (elapsed, ui_font_magic, ui_font_loads, ui_font_len,
                             ui_font_loaded, ui_font_tiny, ui_font_medium,
                             ui_font_big, ui_font_draws, ui_font_reject,
                             ui_font_char, ui_font_shader, ui_font_sx,
                             ui_font_sy, ui_font_sw))
                        detail_log("xblogsoak t=%.1f magic=0x%08x stage=%u transitions=%u activeMsec=%u flags=0x%08x" %
                            (elapsed, mini_soak_magic, mini_soak_stage,
                             mini_soak_transitions, mini_soak_active_msec,
                             mini_soak_flags))
                        detail_log("xblogloadtitle t=%.1f magic=0x%08x status=0x%08x shader=%u draws=%u last=%u map=0x%08x text=0x%08x" %
                            (elapsed, loading_title_magic, loading_title_status,
                             loading_title_shader, loading_title_draws,
                             loading_title_last_char, loading_title_map_hash,
                             loading_title_text_hash))
                        if (helmet_game_p1_ensure or helmet_game_p2_ensure or
                                helmet_cgame_p1_slot0 or helmet_cgame_p1_slot1 or
                                helmet_cgame_p2_slot0 or helmet_cgame_p2_slot1 or
                                helmet_p1_submitted or helmet_p2_submitted or
                                helmet_renderer_refs or helmet_renderer_surfaces or
                                helmet_renderer_filtered or helmet_bolton_load_len or
                                helmet_bolton_count or helmet_add_attempts):
                            detail_log("xbloghelmet t=%.1f load=%u/%u/%u add=%u/%u/%u game=%u/%u/%u/%u cgame=%u/%u/%u/%u p1=%u/%u/%u/0x%08x p2=%u/%u/%u/0x%08x rend=%u/%u/%u/%u/0x%08x/%u/%u/%u" %
                                (elapsed,
                                 helmet_bolton_load_len, helmet_bolton_count,
                                 helmet_bolton_index, helmet_add_attempts,
                                 helmet_add_known_index, helmet_add_fail,
                                 helmet_game_p1_ensure, helmet_game_p2_ensure,
                                 helmet_game_p1_slot, helmet_game_p2_slot,
                                 helmet_cgame_p1_slot0, helmet_cgame_p1_slot1,
                                 helmet_cgame_p2_slot0, helmet_cgame_p2_slot1,
                                 helmet_p1_submitted, helmet_p1_attached,
                                 helmet_p1_model, helmet_p1_rf,
                                 helmet_p2_submitted, helmet_p2_attached,
                                 helmet_p2_model, helmet_p2_rf,
                                 helmet_renderer_refs, helmet_renderer_surfaces,
                                 helmet_renderer_filtered, helmet_renderer_last_model,
                                 helmet_renderer_last_rf, helmet_renderer_last_ent,
                                 helmet_renderer_last_filter,
                                 helmet_renderer_last_surface_model))
                        if fb_count or fb_shader or fb_image:
                            detail_log("xblogfb t=%.1f magic=0x%08x count=%u shader=0x%08x image=0x%08x stage=%u passes=%u flags=0x%08x tex=%u lm=%u state=0x%08x idx=%u xyz=%d/%d/%d" %
                                (elapsed, fb_magic, fb_count, fb_shader, fb_image,
                                 fb_stage, fb_passes, fb_flags, fb_texnum,
                                 fb_lightmap, fb_state, fb_indexes,
                                 signed32(fb_x), signed32(fb_y), signed32(fb_z)))
                        if skyres_count or skyres_map_hash or skyres_resolved_hash:
                            detail_log("xblogskyres t=%.1f magic=0x%08x count=%u shaderNum=%u map=0x%08x resolved=0x%08x surf=0x%08x default=%u explicit=%u hasSky=%u passes=%u sort1000=%d lm0=%d" %
                                (elapsed, skyres_magic, skyres_count, skyres_shader_num,
                                 skyres_map_hash, skyres_resolved_hash, skyres_surface_flags,
                                 skyres_default, skyres_explicit, skyres_has_sky,
                                 skyres_passes, signed32(skyres_sort_x1000),
                                 signed32(skyres_lightmap0)))
                        if shader_scan_magic:
                            detail_log("xblogshader t=%.1f scan=0x%08x scripts=%u shaders=%u loaded=%u bytes=%u raw=%u entries=%u skyLight=%u junkSky=%u manifest=%u/%u/%u voyager=%u/%u/%u common=%u lookup=0x%08x count=%u hash=0x%08x indexed=%u linear=%u lookupEntries=%u" %
                                (elapsed, shader_scan_magic, shader_scan_scripts,
                                 shader_scan_shaders, shader_scan_loaded, shader_scan_bytes,
                                 shader_scan_raw_bytes, shader_scan_entries,
                                 shader_scan_sky_light, shader_scan_junk_sky,
                                 shader_scan_manifest_active,
                                 shader_scan_manifest_read_len,
                                 shader_scan_manifest_count,
                                 shader_scan_voyager_listed,
                                 shader_scan_voyager_read_len,
                                 shader_scan_voyager_sky_token,
                                 shader_scan_common_read_len,
                                 shader_lookup_magic, shader_lookup_count, shader_lookup_hash,
                                 shader_lookup_indexed, shader_lookup_linear, shader_lookup_entries))
                    elif len(words) >= 3:
                        boot_phase = words[0]
                        mirror_pos = words[1]
                        write_count = words[2]
                        delta = 0 if last_xblog_write_count is None else write_count - last_xblog_write_count
                        last_xblog_write_count = write_count
                        log("xblog t=%.1f boot=0x%08x mirror=%u writes=%u delta=%d heartbeat=unreadable" %
                            (elapsed, boot_phase, mirror_pos, write_count, delta))
                    else:
                        debug_path = os.path.abspath("%s_xblog_%04d.txt" % (prefix, int(elapsed * 10)))
                        with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
                            f.write(reply)
                        log("xblog t=%.1f unreadable=%s raw=%s" %
                            (elapsed, reply.strip().replace("\n", "\\n")[:240], debug_path))
                except OSError as exc:
                    log("xblog t=%.1f unavailable=%s" % (elapsed, exc))
                    xblog_addr = None

            if elapsed >= next_shot:
                if capture_enabled:
                    time.sleep(0.1)
                    if (sock is not None and hm_split_pose_base_va is not None and
                            hm_split_pose_offsets):
                        pose_word_count = max(hm_split_pose_offsets.values()) + 4
                        pose_words = monitor_read_virtual_words(
                            sock, hm_split_pose_base_va, pose_word_count)
                        if pose_words:
                            def pose_word(symbol, slot):
                                index = hm_split_pose_offsets[symbol] + slot
                                return pose_words[index] if index < len(pose_words) else 0

                            for pose_slot in range(4):
                                log("shot_pose shot=%02d slot=%d origin=(%d,%d,%d) view=(%d,%d,%d) time=%u" %
                                    (shot, pose_slot,
                                     signed_u32(pose_word("_g_SPXBHMSplitStateOriginX", pose_slot)),
                                     signed_u32(pose_word("_g_SPXBHMSplitStateOriginY", pose_slot)),
                                     signed_u32(pose_word("_g_SPXBHMSplitStateOriginZ", pose_slot)),
                                     signed_u32(pose_word("_g_SPXBHMSplitStateViewPitch", pose_slot)),
                                     signed_u32(pose_word("_g_SPXBHMSplitStateViewYaw", pose_slot)),
                                     signed_u32(pose_word("_g_SPXBHMSplitStateViewRoll", pose_slot)),
                                     pose_word("_g_SPXBHMSplitStateTime", pose_slot)))
                    png = os.path.abspath("%s_%02d.png" % (prefix, shot))
                    xblog_phys_delta = None
                    if xblog_va_for_probe is not None and xblog_addr is not None:
                        xblog_phys_delta = xblog_va_for_probe - xblog_addr
                    if args.xemu_native_screenshots:
                        ok, detail, native_path = xemu_trigger_native_screenshot(
                            proc.pid, xemu_exe, xemu_screenshot_dir)
                        if ok and native_path:
                            png = native_path
                    else:
                        ok, detail = monitor_screendump(sock, png, xblog_phys_delta)
                    if ok:
                        shot_paths.append(png)
                    log("shot=%02d t=%.1f ok=%s bytes=%s detail=%s" %
                        (shot, elapsed, ok, os.path.getsize(png) if ok and os.path.exists(png) else 0, detail))
                else:
                    log("shot=%02d t=%.1f skipped display=%s" % (shot, elapsed, display_mode))
                shot += 1
                next_shot += args.interval

            for event in monitor_key_events:
                if not event[3] and elapsed >= event[0]:
                    if args.host_window_keys:
                        ok, key_detail = send_window_key(
                            proc.pid, event[1], event[2])
                    elif sock is None:
                        ok = False
                        key_detail = "monitor unavailable"
                    else:
                        monitor_cmd(sock, "sendkey %s" % event[1], max(0.05, event[2]))
                        ok = True
                        key_detail = "qemu monitor"
                    event[3] = True
                    log("monitor_key t=%.1f key=%s hold=%.2f ok=%s detail=%s" %
                        (elapsed, event[1], event[2], ok, key_detail))

            time.sleep(0.25)

        if proc.poll() is None:
            log("alive_at_end pid=%d" % proc.pid)

        if sock is not None and proc.poll() is None:
            # Freeze the guest before collecting final diagnostics. Large memory
            # dumps can take several seconds, otherwise ring buffers and related
            # counters continue changing underneath the capture.
            try:
                monitor_cmd(sock, "stop", 0.2)
                log("emulation_stopped_for_final_diagnostics")
                if args.extract_xblog_profile:
                    extract_xblog_profiles_from_physical_memory(
                        sock, prefix, log)
                dump_monitor_state(sock, prefix, "final", args.dump_mem, log)
                dump_virtual_memory_binary(sock, prefix, args.dump_bin_mem, log)
                dump_physical_memory(sock, prefix, args.dump_phys, log)
            except OSError as exc:
                log("final_diagnostics_unavailable=%s" % exc)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()
            time.sleep(1.0)
            if proc.poll() is None:
                proc.kill()
        logf.close()
        if toml_backup is not None:
            with open(config_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(toml_backup)

    contact = prefix + "_contact.png"
    if write_contact_sheet(shot_paths, contact):
        log("contact=%s" % os.path.abspath(contact))

    def log_fps_summary(label, samples):
        if not samples:
            log("fps_summary bucket=%s samples=0" % label)
            return
        ordered = sorted(samples)
        p10_index = max(0, int((len(ordered) - 1) * 0.10))
        below_30 = sum(1 for sample in samples if sample < 30.0)
        log("fps_summary bucket=%s samples=%u avg=%.1f min=%.1f p10=%.1f max=%.1f below30=%u below30_pct=%.1f" %
            (label, len(samples), sum(samples) / len(samples), ordered[0],
             ordered[p10_index], ordered[-1], below_30,
             (below_30 * 100.0) / len(samples)))

    log_fps_summary("active", active_fps_samples)
    log_fps_summary("gameplay", gameplay_fps_samples)
    for personality in sorted(personality_active_fps_samples):
        log_fps_summary(
            "%s_active" % personality,
            personality_active_fps_samples[personality])
    for personality in sorted(personality_gameplay_fps_samples):
        log_fps_summary(
            "%s_gameplay" % personality,
            personality_gameplay_fps_samples[personality])
    log_fps_summary("wall_active", active_wall_fps_samples)
    log_fps_summary("wall_gameplay", gameplay_wall_fps_samples)
    log_fps_summary("word_%s" % args.poll_word_label, poll_word_rates)
    for personality in sorted(personality_active_wall_fps_samples):
        log_fps_summary(
            "%s_wall_active" % personality,
            personality_active_wall_fps_samples[personality])
    for personality in sorted(personality_gameplay_wall_fps_samples):
        log_fps_summary(
            "%s_wall_gameplay" % personality,
            personality_gameplay_wall_fps_samples[personality])

    try:
        with open(raw_log, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-80:]
        log("--- xemu tail ---")
        for line in tail:
            log(line.rstrip())
    except Exception as exc:
        log("raw_log_read_failed=%s" % exc)

    with open(report, "w", encoding="utf-8", errors="replace") as f:
        f.write("\n".join(lines) + "\n")
    print("report=%s" % report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
