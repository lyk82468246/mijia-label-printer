#!/usr/bin/env python3
"""USB probe for the Xiaomi Mijia Label Printer (VID 2717 / PID 507D).

This uses the same AES-framed command protocol that the official app uses over
BLE, but sends it through the Windows USB printer-class device interface. The
initial probe only sends harmless control/status commands; it does not feed or
print paper.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES

# Protocol constants recovered from the public reverse-engineering work for
# Mijia MJBQDYJ1-WC. These frame/encrypt commands; USB transport is our adapter.
KEY = bytes.fromhex("99B829436CDD5647AADB8816F73E8644")
IV = bytes.fromhex("0001020F3CF899ABABCD25318DF446B1")
CRC_SEED = 0x76953521
PIC_CHUNK_MAX = 1800

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

DEVICE_PATHS = [
    r"\\?\usb#vid_2717&pid_507d#000000000004#{28d78fad-5a12-11d1-ae5b-0000f803a8c2}",
    r"\\.\USB003",
]


def pad16(data: bytes) -> bytes:
    return data + b"\x00" * ((16 - len(data) % 16) % 16)


def encrypt_payload(payload: bytes) -> bytes:
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(pad16(payload))


def decrypt_payload(ciphertext: bytes) -> bytes:
    return AES.new(KEY, AES.MODE_CBC, IV).decrypt(ciphertext)


def crc32_custom(buf: bytes, init: int = CRC_SEED) -> int:
    crc = (~init) & 0xFFFFFFFF
    for byte in buf:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
            crc &= 0xFFFFFFFF
    return (~crc) & 0xFFFFFFFF


def frame_command(payload: bytes) -> bytes:
    encrypted = encrypt_payload(payload)
    return b"\xA3\x20" + struct.pack("<H", len(encrypted)) + encrypted + struct.pack("<I", crc32_custom(encrypted))


def unframe_packet(wire: bytes) -> bytes | None:
    if len(wire) < 8 or wire[0] != 0xA3 or wire[1] not in (0x00, 0x20):
        return None
    body_len = struct.unpack_from("<H", wire, 2)[0]
    if len(wire) < 4 + body_len + 4:
        return None
    body = wire[4:4 + body_len]
    got_crc = struct.unpack_from("<I", wire, 4 + body_len)[0]
    if crc32_custom(body) != got_crc:
        return None
    return decrypt_payload(body) if wire[1] == 0x20 else body


def cmd_set_connected() -> bytes:
    return frame_command(bytes.fromhex("11011E010001"))


def cmd_get_battery() -> bytes:
    return frame_command(bytes.fromhex("11011A0000"))


def cmd_query_status() -> bytes:
    return frame_command(bytes.fromhex("1101130000"))


def cmd_sync_time(epoch_seconds: int | None = None) -> bytes:
    if epoch_seconds is None:
        epoch_seconds = int(time.time())
    return frame_command(bytes.fromhex("1101190400") + struct.pack("<I", epoch_seconds + 28800))


@dataclass
class PrinterState:
    buffer_free: int = -1
    cover_open: bool = False
    lack_paper: bool = False
    jam: bool = False
    hot: bool = False
    printing: bool = False
    battery_pct: int = -1

    def __str__(self) -> str:
        return (
            f"buffer_free={self.buffer_free} battery={self.battery_pct}% "
            f"cover_open={self.cover_open} lack_paper={self.lack_paper} "
            f"jam={self.jam} hot={self.hot} printing={self.printing}"
        )


def parse_notification(plain: bytes, state: PrinterState) -> None:
    if len(plain) < 3:
        return
    grp, cmd = plain[1], plain[2]
    if grp == 0x01 and cmd == 0x1F:
        if len(plain) >= 12:
            state.buffer_free = struct.unpack_from("<I", plain, 8)[0]
        if len(plain) >= 17:
            flags = plain[15]
            state.cover_open = bool(flags & 1)
            state.lack_paper = bool(flags & 2)
            state.jam = bool(flags & 4)
            state.hot = bool(flags & 8)
            state.printing = bool(flags & 0x80)
        if len(plain) >= 23:
            state.battery_pct = struct.unpack_from(">H", plain, 21)[0] // 10
    elif grp == 0x01 and cmd in (0x12, 0x13):
        if len(plain) >= 10:
            flags = plain[8]
            state.cover_open = bool(flags & 1)
            state.lack_paper = bool(flags & 2)
            state.jam = bool(flags & 4)
            state.hot = bool(flags & 8)
            state.printing = bool(flags & 0x80)


class UsbPrinter:
    def __init__(self, path: str | None = None):
        self.path = path
        self.handle = None
        self.rx = bytearray()
        self.state = PrinterState()

    def open(self) -> str:
        paths = [self.path] if self.path else DEVICE_PATHS
        errors = []
        for path in paths:
            handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if handle != INVALID_HANDLE_VALUE and handle is not None:
                self.handle = handle
                self.path = path
                return path
            errors.append(f"{path}: WinError {ctypes.get_last_error()}")
        raise OSError("could not open Mijia USB printer: " + "; ".join(errors))

    def close(self) -> None:
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def write(self, data: bytes) -> int:
        if self.handle is None:
            raise OSError("device not open")
        written = ctypes.c_uint32(0)
        if not kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), None):
            raise OSError(f"WriteFile failed: WinError {ctypes.get_last_error()}")
        return written.value

    def read_once(self, size: int = 4096) -> bytes:
        if self.handle is None:
            raise OSError("device not open")
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_uint32(0)
        if not kernel32.ReadFile(self.handle, buf, size, ctypes.byref(read), None):
            raise OSError(f"ReadFile failed: WinError {ctypes.get_last_error()}")
        return buf.raw[:read.value]

    def drain(self, seconds: float = 1.5) -> list[bytes]:
        """Read available bytes in a daemon thread and decode complete frames."""
        result: list[bytes] = []

        def reader():
            try:
                result.append(self.read_once())
            except Exception as exc:
                result.append(f"__READ_ERROR__ {exc}".encode())

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(seconds)
        if not thread.is_alive() and result:
            data = result[0]
            if data.startswith(b"__READ_ERROR__"):
                print(data.decode(errors="replace"), file=sys.stderr)
                return []
            self.rx.extend(data)
            frames = []
            while len(self.rx) >= 4 and self.rx[0] == 0xA3 and self.rx[1] in (0x00, 0x20):
                body_len = struct.unpack_from("<H", self.rx, 2)[0]
                total = 4 + body_len + 4
                if len(self.rx) < total:
                    break
                frame = bytes(self.rx[:total])
                del self.rx[:total]
                plain = unframe_packet(frame)
                if plain is not None:
                    frames.append(plain)
                    parse_notification(plain, self.state)
            while self.rx and self.rx[0] != 0xA3:
                del self.rx[:0]
            return frames
        print("read timeout", file=sys.stderr)
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", help="override USB device interface path")
    parser.add_argument("--settle", type=float, default=0.25)
    args = parser.parse_args()

    printer = UsbPrinter(args.path)
    path = printer.open()
    print("opened:", path)
    try:
        for label, command in [
            ("set_connected", cmd_set_connected()),
            ("get_battery", cmd_get_battery()),
            ("query_status", cmd_query_status()),
            ("sync_time", cmd_sync_time()),
        ]:
            wire = command.hex()
            print(f"TX {label}: {wire}")
            n = printer.write(command)
            print(f"  wrote {n} bytes")
            time.sleep(args.settle)
            for plain in printer.drain(1.0):
                print("  ← plain:", plain.hex())
            print("  state:", printer.state)
    finally:
        printer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
