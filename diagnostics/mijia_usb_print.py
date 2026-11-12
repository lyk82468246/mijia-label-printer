#!/usr/bin/env python3
"""Print to a Xiaomi Mijia MJBQDYJ1-WC label printer over USB.

Uses the reverse-engineered Mijia label-printer command framing (AES-CBC)
through the Windows USB printer-class device interface. This adapter sends the
same logical commands that the official app sends over BLE.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import time
from Crypto.Cipher import AES
from PIL import Image, ImageDraw, ImageFont

KEY = bytes.fromhex("99B829436CDD5647AADB8816F73E8644")
IV = bytes.fromhex("0001020F3CF899ABABCD25318DF446B1")
CRC_SEED = 0x76953521
PIC_CHUNK_MAX = 1800
DEVICE_PATH = r"\\?\usb#vid_2717&pid_507d#000000000004#{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
kernel32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_bool]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

GENERIC_WRITE = 0x40000000
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 0x102


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def pad16(data: bytes) -> bytes:
    return data + b"\x00" * ((16 - len(data) % 16) % 16)


def crc32_custom(buf: bytes, init: int = CRC_SEED) -> int:
    crc = (~init) & 0xFFFFFFFF
    for byte in buf:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
            crc &= 0xFFFFFFFF
    return (~crc) & 0xFFFFFFFF


def frame_command(payload: bytes) -> bytes:
    encrypted = AES.new(KEY, AES.MODE_CBC, IV).encrypt(pad16(payload))
    return b"\xA3\x20" + struct.pack("<H", len(encrypted)) + encrypted + struct.pack("<I", crc32_custom(encrypted))


def cmd_set_connected() -> bytes:
    return frame_command(bytes.fromhex("11011E010001"))


def cmd_query_status() -> bytes:
    return frame_command(bytes.fromhex("1101130000"))


def cmd_sync_time(epoch_seconds: int | None = None) -> bytes:
    if epoch_seconds is None:
        epoch_seconds = int(time.time())
    return frame_command(bytes.fromhex("1101190400") + struct.pack("<I", epoch_seconds + 28800))


def cmd_print_start(label_length_dots: int, width_dots: int = 96) -> bytes:
    return frame_command(bytes.fromhex("11050B0700") + struct.pack("<HH", width_dots, label_length_dots) + b"\x01\x00\x00")


def cmd_print_finalize(more_pages: bool = False) -> bytes:
    return frame_command(bytes.fromhex("11050C09000102000000020100") + (b"\x01" if more_pages else b"\x00"))


def cmd_print_data_chunks(raster: bytes) -> list[bytes]:
    total = max(1, (len(raster) + PIC_CHUNK_MAX - 1) // PIC_CHUNK_MAX)
    frames = []
    for idx in range(total):
        chunk = raster[idx * PIC_CHUNK_MAX:(idx + 1) * PIC_CHUNK_MAX]
        payload_len = len(chunk) + 11
        header = bytes.fromhex("11050D") + struct.pack("<HHH", payload_len, idx + 1, total) + bytes.fromhex("100C0000000000")
        frames.append(frame_command(header + chunk))
    return frames


def image_to_raster(source, width_dots: int = 96) -> tuple[bytes, int]:
    img = Image.open(source).convert("L")
    w, h = img.size
    new_h = max(1, round(h * width_dots / w))
    img = img.resize((width_dots, new_h)).convert("1")
    pixels = img.load()
    bytes_per_row = width_dots // 8
    out = bytearray(bytes_per_row * new_h)
    for y in range(new_h):
        for x in range(width_dots):
            if pixels[x, y] == 0:
                out[y * bytes_per_row + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out), new_h


def make_test_label(path: str = "mijia_usb_test_label.png") -> str:
    img = Image.new("L", (96, 52), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 95, 51), outline=0)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((8, 7), "USB TEST", font=font, fill=0)
    draw.text((18, 29), "MIJIA", font=font, fill=0)
    img.save(path)
    return path


class UsbWriter:
    def __init__(self, path: str):
        self.handle = kernel32.CreateFileW(path, GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle == INVALID_HANDLE_VALUE or self.handle is None:
            raise OSError(f"open failed: WinError {ctypes.get_last_error()} {path}")
        self.path = path

    def close(self):
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def write(self, data: bytes, timeout: float = 5.0) -> int:
        event = kernel32.CreateEventW(None, True, False, None)
        ov = OVERLAPPED()
        ov.hEvent = event
        written = ctypes.c_uint32(0)
        ok = kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                kernel32.CloseHandle(event)
                raise OSError(f"WriteFile failed: WinError {err}")
            wait = kernel32.WaitForSingleObject(event, int(timeout * 1000))
            if wait == WAIT_TIMEOUT:
                kernel32.CancelIoEx(self.handle, ctypes.byref(ov))
                kernel32.CloseHandle(event)
                raise TimeoutError("USB write timed out")
            ok = kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(written), False)
            if not ok:
                err = ctypes.get_last_error()
                kernel32.CloseHandle(event)
                raise OSError(f"write result failed: WinError {err}")
        kernel32.CloseHandle(event)
        return written.value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    parser.add_argument("--path", default=DEVICE_PATH)
    parser.add_argument("--width", type=int, default=96, choices=[96], help="print head width in dots")
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--data-delay", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    image = args.image or make_test_label()
    raster, height = image_to_raster(image, args.width)
    frames: list[tuple[str, bytes]] = [
        ("set_connected", cmd_set_connected()),
        ("query_status", cmd_query_status()),
        ("sync_time", cmd_sync_time()),
        ("print_start", cmd_print_start(height, args.width)),
    ]
    frames.extend((f"data_{i+1}", frame) for i, frame in enumerate(cmd_print_data_chunks(raster)))
    frames.append(("finalize", cmd_print_finalize(False)))

    total = sum(len(frame) for _, frame in frames)
    print(f"image={image} raster={len(raster)} bytes height={height} dots frames={len(frames)} total={total} bytes")
    if args.dry_run:
        for name, frame in frames:
            print(f"DRY {name}: {len(frame)} bytes {frame.hex()}")
        return 0

    writer = UsbWriter(args.path)
    print("opened:", args.path)
    try:
        for name, frame in frames:
            n = writer.write(frame, timeout=5.0)
            print(f"TX {name}: {n} bytes")
            time.sleep(args.data_delay if name.startswith("data_") else args.delay)
        print("print job sent")
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
