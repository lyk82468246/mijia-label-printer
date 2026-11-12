#!/usr/bin/env python3
"""Overlapped USB printer probe; prevents a NAKing bulk endpoint from hanging Python."""

import ctypes
import struct
import sys
import time
from Crypto.Cipher import AES

KEY = bytes.fromhex("99B829436CDD5647AADB8816F73E8644")
IV = bytes.fromhex("0001020F3CF899ABABCD25318DF446B1")
CRC_SEED = 0x76953521
DEVICE_PATH = r"\\?\usb#vid_2717&pid_507d#000000000004#{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
kernel32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_bool]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
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


def frame(payload: bytes) -> bytes:
    enc = AES.new(KEY, AES.MODE_CBC, IV).encrypt(pad16(payload))
    return b"\xA3\x20" + struct.pack("<H", len(enc)) + enc + struct.pack("<I", crc32_custom(enc))


def unframe(wire: bytes) -> bytes | None:
    if len(wire) < 8 or wire[0] != 0xA3 or wire[1] not in (0, 0x20):
        return None
    n = struct.unpack_from("<H", wire, 2)[0]
    if len(wire) < n + 8:
        return None
    body = wire[4:4+n]
    if crc32_custom(body) != struct.unpack_from("<I", wire, 4+n)[0]:
        return None
    return AES.new(KEY, AES.MODE_CBC, IV).decrypt(body) if wire[1] == 0x20 else body


class AsyncUsb:
    def __init__(self, path: str):
        self.handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle == INVALID_HANDLE_VALUE or self.handle is None:
            raise OSError(f"open failed: WinError {ctypes.get_last_error()}")
        self.path = path

    def close(self):
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def _event(self):
        return kernel32.CreateEventW(None, True, False, None)

    def write(self, data: bytes, timeout: float = 3.0):
        event = self._event()
        ov = OVERLAPPED()
        ov.hEvent = event
        written = ctypes.c_uint32(0)
        ok = kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                kernel32.CloseHandle(event)
                raise OSError(f"WriteFile failed immediately: WinError {err}")
            wait = kernel32.WaitForSingleObject(event, int(timeout * 1000))
            if wait == WAIT_TIMEOUT:
                kernel32.CancelIoEx(self.handle, ctypes.byref(ov))
                kernel32.CloseHandle(event)
                return "timeout", 0
            ok = kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(written), False)
            if not ok:
                err = ctypes.get_last_error()
                kernel32.CloseHandle(event)
                raise OSError(f"GetOverlappedResult write failed: WinError {err}")
        kernel32.CloseHandle(event)
        return "ok", written.value

    def read(self, size: int = 4096, timeout: float = 2.0):
        event = self._event()
        ov = OVERLAPPED()
        ov.hEvent = event
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_uint32(0)
        ok = kernel32.ReadFile(self.handle, buf, size, ctypes.byref(read), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                kernel32.CloseHandle(event)
                raise OSError(f"ReadFile failed immediately: WinError {err}")
            wait = kernel32.WaitForSingleObject(event, int(timeout * 1000))
            if wait == WAIT_TIMEOUT:
                kernel32.CancelIoEx(self.handle, ctypes.byref(ov))
                kernel32.CloseHandle(event)
                return "timeout", b""
            ok = kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(read), False)
            if not ok:
                err = ctypes.get_last_error()
                kernel32.CloseHandle(event)
                raise OSError(f"GetOverlappedResult read failed: WinError {err}")
        kernel32.CloseHandle(event)
        return "ok", buf.raw[:read.value]


def main():
    dev = AsyncUsb(DEVICE_PATH)
    print("opened:", DEVICE_PATH)
    try:
        commands = [
            ("set_connected", frame(bytes.fromhex("11011E010001"))),
            ("get_battery", frame(bytes.fromhex("11011A0000"))),
            ("query_status", frame(bytes.fromhex("1101130000"))),
            ("sync_time", frame(bytes.fromhex("1101190400") + struct.pack("<I", int(time.time()) + 28800))),
        ]
        rxbuf = bytearray()
        for name, data in commands:
            status, n = dev.write(data, timeout=3.0)
            print(f"TX {name}: {n} bytes, status={status}")
            if status != "ok":
                continue
            time.sleep(0.2)
            status, chunk = dev.read(4096, timeout=2.0)
            print("RX", status, chunk.hex() if chunk else "")
            if chunk:
                rxbuf.extend(chunk)
                while len(rxbuf) >= 8 and rxbuf[0] == 0xA3 and rxbuf[1] in (0, 0x20):
                    n = struct.unpack_from("<H", rxbuf, 2)[0]
                    total = n + 8
                    if len(rxbuf) < total:
                        break
                    plain = unframe(bytes(rxbuf[:total]))
                    del rxbuf[:total]
                    if plain is not None:
                        print("PLAIN", plain.hex())
    finally:
        dev.close()


if __name__ == "__main__":
    main()
