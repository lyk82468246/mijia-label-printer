#!/usr/bin/env python3
"""Practical USB label printer for Xiaomi Mijia MJBQDYJ1-WC.

Tested on Windows with the USB printer-class device:
    VID 2717 PID 507D, bus-reported name "Mijia Label Printer".

It uses the reverse-engineered Mijia command framing (AES-CBC + custom CRC32)
over the Windows USB printer interface. Commands include text, image, QR code,
and Code128-style barcode labels.

Examples:
    python mijia_label.py text "Hello" "第二行"
    python mijia_label.py image label.png --preview-only
    python mijia_label.py qr "https://example.com"
    python mijia_label.py barcode "123456789"
    python mijia_label.py text "Dry run" --dry-run
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import ctypes.wintypes as wintypes
import io
import json
import struct
import sys
import time
from itertools import zip_longest
from pathlib import Path

from Crypto.Cipher import AES
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / ".vendor"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))

import segno  # noqa: E402
import barcode as barcode_library  # noqa: E402
from barcode.writer import ImageWriter  # noqa: E402


# ---------------------------------------------------------------------------
# Mijia protocol constants
# ---------------------------------------------------------------------------

KEY = bytes.fromhex("99B829436CDD5647AADB8816F73E8644")
IV = bytes.fromhex("0001020F3CF899ABABCD25318DF446B1")
CRC_SEED = 0x76953521
PIC_CHUNK_MAX = 1800
HEAD_WIDTH_DOTS = 96
BYTES_PER_ROW = HEAD_WIDTH_DOTS // 8

FALLBACK_DEVICE_PATH = (
    r"\\?\usb#vid_2717&pid_507d#000000000004"
    r"#{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"
)

GENERIC_WRITE = 0x40000000
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 0x102

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
kernel32.WriteFile.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
kernel32.GetOverlappedResult.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_bool,
]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


GUID_DEVINTERFACE_USBPRINT = GUID(
    0x28D78FAD,
    0x5A12,
    0x11D1,
    (ctypes.c_ubyte * 8)(0xAE, 0x5B, 0x00, 0x00, 0xF8, 0x03, 0xA8, 0xC2),
)


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("DevicePath", wintypes.WCHAR * 1),
    ]


setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.HANDLE,
]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]


def find_usb_printer_paths(vendor_id: str = "VID_2717", product_id: str = "PID_507D") -> list[str]:
    """Find currently present USB printer-interface device paths.

    SetupAPI reports only devices that are actually present now. The old USB
    Monitor registry entries can remain after a device is unplugged, so they
    must not be used as the sole source of connection state.
    """
    paths: list[str] = []
    handle = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(GUID_DEVINTERFACE_USBPRINT),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        return paths

    vendor_marker = vendor_id.lower()
    product_marker = product_id.lower()
    try:
        index = 0
        while True:
            interface_data = SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                handle, None, ctypes.byref(GUID_DEVINTERFACE_USBPRINT), index,
                ctypes.byref(interface_data),
            ):
                break
            index += 1

            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(interface_data), None, 0,
                ctypes.byref(required), None,
            )
            if required.value < ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W):
                continue

            buffer = ctypes.create_string_buffer(required.value)
            detail = ctypes.cast(
                buffer, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            ).contents
            detail.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(interface_data), buffer, required.value,
                None, None,
            ):
                continue

            path = ctypes.wstring_at(
                ctypes.addressof(detail) + SP_DEVICE_INTERFACE_DETAIL_DATA_W.DevicePath.offset
            )
            lowered = path.lower()
            if vendor_marker in lowered and product_marker in lowered:
                paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return list(dict.fromkeys(paths))


def resolve_device_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    paths = find_usb_printer_paths()
    if paths:
        return paths[0]
    return FALLBACK_DEVICE_PATH


# ---------------------------------------------------------------------------
# Low-level overlapped USB writing
# ---------------------------------------------------------------------------

class UsbPrinter:
    def __init__(self, path: str, write_timeout: float = 5.0):
        self.path = path
        self.write_timeout = write_timeout
        self.handle = kernel32.CreateFileW(
            path, GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None
        )
        if self.handle == INVALID_HANDLE_VALUE or self.handle is None:
            error = ctypes.get_last_error()
            raise OSError(
                f"无法打开 USB 打印设备: WinError {error}\n设备路径: {path}\n"
                "请确认打印机已连接、没有被其它打印任务占用。"
            )

    def close(self) -> None:
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def write(self, data: bytes, timeout: float | None = None) -> int:
        timeout = self.write_timeout if timeout is None else timeout
        event = kernel32.CreateEventW(None, True, False, None)
        overlapped = OVERLAPPED()
        overlapped.hEvent = event
        written = ctypes.c_uint32(0)

        try:
            ok = kernel32.WriteFile(
                self.handle, data, len(data), ctypes.byref(written), ctypes.byref(overlapped)
            )
            if not ok:
                error = ctypes.get_last_error()
                if error != ERROR_IO_PENDING:
                    raise OSError(f"USB WriteFile 失败: WinError {error}")
                wait = kernel32.WaitForSingleObject(event, int(timeout * 1000))
                if wait == WAIT_TIMEOUT:
                    kernel32.CancelIoEx(self.handle, ctypes.byref(overlapped))
                    raise TimeoutError(f"USB 写入超时（{timeout:.1f}s），设备可能暂停或未就绪")
                ok = kernel32.GetOverlappedResult(
                    self.handle, ctypes.byref(overlapped), ctypes.byref(written), False
                )
                if not ok:
                    raise OSError(f"USB 写入结果读取失败: WinError {ctypes.get_last_error()}")
            return written.value
        finally:
            kernel32.CloseHandle(event)


# ---------------------------------------------------------------------------
# Protocol framing
# ---------------------------------------------------------------------------

def _pad16(data: bytes) -> bytes:
    return data + b"\x00" * ((16 - len(data) % 16) % 16)


def crc32_custom(data: bytes, init: int = CRC_SEED) -> int:
    crc = (~init) & 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
            crc &= 0xFFFFFFFF
    return (~crc) & 0xFFFFFFFF


def frame_command(payload: bytes) -> bytes:
    encrypted = AES.new(KEY, AES.MODE_CBC, IV).encrypt(_pad16(payload))
    return (
        b"\xA3\x20"
        + struct.pack("<H", len(encrypted))
        + encrypted
        + struct.pack("<I", crc32_custom(encrypted))
    )


def cmd_set_connected() -> bytes:
    return frame_command(bytes.fromhex("11011E010001"))


def cmd_query_status() -> bytes:
    return frame_command(bytes.fromhex("1101130000"))


def cmd_sync_time(epoch_seconds: int | None = None) -> bytes:
    if epoch_seconds is None:
        epoch_seconds = int(time.time())
    return frame_command(bytes.fromhex("1101190400") + struct.pack("<I", epoch_seconds + 28800))


def cmd_print_start(label_length_dots: int, width_dots: int = HEAD_WIDTH_DOTS) -> bytes:
    return frame_command(
        bytes.fromhex("11050B0700")
        + struct.pack("<HH", width_dots, label_length_dots)
        + b"\x01\x00\x00"
    )


def cmd_print_data_chunks(raster: bytes) -> list[bytes]:
    total_chunks = max(1, (len(raster) + PIC_CHUNK_MAX - 1) // PIC_CHUNK_MAX)
    frames = []
    for index in range(total_chunks):
        chunk = raster[index * PIC_CHUNK_MAX:(index + 1) * PIC_CHUNK_MAX]
        payload_length = len(chunk) + 11
        header = (
            bytes.fromhex("11050D")
            + struct.pack("<HHH", payload_length, index + 1, total_chunks)
            + bytes.fromhex("100C0000000000")
        )
        frames.append(frame_command(header + chunk))
    return frames


def cmd_print_finalize(more_pages: bool = False) -> bytes:
    tail = b"\x01" if more_pages else b"\x00"
    return frame_command(bytes.fromhex("11050C09000102000000020100") + tail)


# ---------------------------------------------------------------------------
# Bitmap conversion
# ---------------------------------------------------------------------------

def image_to_raster(image: Image.Image, threshold: int = 128, invert: bool = False) -> tuple[bytes, int]:
    gray = image.convert("L")
    if invert:
        gray = ImageOps.invert(gray)
    height = max(1, round(gray.height * HEAD_WIDTH_DOTS / gray.width))
    resized = gray.resize((HEAD_WIDTH_DOTS, height), Image.Resampling.LANCZOS)
    black_white = resized.point(lambda value: 0 if value < threshold else 255, mode="L").convert("1")
    pixels = black_white.load()
    raster = bytearray(BYTES_PER_ROW * height)
    for y in range(height):
        for x in range(HEAD_WIDTH_DOTS):
            if pixels[x, y] == 0:
                raster[y * BYTES_PER_ROW + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(raster), height


def raster_to_image(raster: bytes, height: int) -> Image.Image:
    """Convert packed 1bpp printer raster back to a grayscale preview image."""
    if height < 1:
        raise ValueError("光栅高度必须大于 0")
    expected = BYTES_PER_ROW * height
    if len(raster) != expected:
        raise ValueError(f"光栅长度不正确：期望 {expected} 字节，实际 {len(raster)} 字节")
    image = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
    pixels = image.load()
    for y in range(height):
        row_offset = y * BYTES_PER_ROW
        for x in range(HEAD_WIDTH_DOTS):
            if raster[row_offset + (x >> 3)] & (0x80 >> (x & 7)):
                pixels[x, y] = 0
    return image


def rotate_print_image(image: Image.Image, rotation: int | str = 0) -> Image.Image:
    """Rotate label content and normalize the result to the 96-dot head width.

    Angles are counterclockwise. When a rotated label is wider than the fixed
    print head, it is scaled proportionally before thresholding so the preview
    matches the actual raster sent to USB.
    """
    try:
        angle = int(rotation) % 360
    except (TypeError, ValueError):
        raise ValueError("旋转角度必须是 0、90、180 或 270")
    if angle not in (0, 90, 180, 270):
        raise ValueError("旋转角度必须是 0、90、180 或 270")

    if angle == 0:
        rotated = image
    elif angle == 90:
        rotated = image.transpose(Image.Transpose.ROTATE_90)
    elif angle == 180:
        rotated = image.transpose(Image.Transpose.ROTATE_180)
    else:
        rotated = image.transpose(Image.Transpose.ROTATE_270)

    if rotated.width != HEAD_WIDTH_DOTS:
        raster, height = image_to_raster(rotated, threshold=128, invert=False)
        rotated = raster_to_image(raster, height)
    return rotated


# ---------------------------------------------------------------------------
# Label rendering
# ---------------------------------------------------------------------------

def load_font(font_path: str | None, size: int):
    if font_path:
        return ImageFont.truetype(font_path, size)
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\consola.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap_text(text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        if not raw_line:
            lines.append("")
            continue

        words = raw_line.split()
        use_word_wrap = len(words) > 1 and all(font.getlength(word) <= max_width for word in words)
        pieces = words if use_word_wrap else list(raw_line)
        separator = " " if use_word_wrap else ""

        current = ""
        for piece in pieces:
            candidate = piece if not current else current + separator + piece
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            if font.getlength(piece) <= max_width:
                current = piece
                continue
            current = ""
            for character in piece:
                candidate = current + character
                if font.getlength(candidate) <= max_width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = character
        if current:
            lines.append(current)
    return lines or [""]


def make_text_label(
    text: str,
    font_path: str | None = None,
    font_size: int = 18,
    margin: int = 4,
    align: str = "center",
    line_spacing: float = 1.15,
) -> Image.Image:
    font = load_font(font_path, font_size)
    content_width = HEAD_WIDTH_DOTS - margin * 2
    wrapped = wrap_text(text, font, content_width)
    try:
        ascent, descent = font.getmetrics()
        line_height = max(1, int((ascent + descent) * line_spacing))
    except Exception:
        line_height = max(1, int(font_size * 1.25 * line_spacing))
    height = max(12, margin * 2 + line_height * len(wrapped))
    image = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(wrapped):
        line_width = draw.textlength(line, font=font)
        if align == "left":
            x = margin
        elif align == "right":
            x = HEAD_WIDTH_DOTS - margin - line_width
        else:
            x = (HEAD_WIDTH_DOTS - line_width) // 2
        y = margin + index * line_height
        draw.text((x, y), line, font=font, fill=0)
    return image


def make_qr_label(
    data: str,
    margin: int = 4,
    error_level: str = "m",
    caption: str | None = None,
    font_path: str | None = None,
    font_size: int = 12,
) -> Image.Image:
    qr = segno.make(data, error=error_level.upper())
    try:
        matrix_size = qr.matrix_size()
    except Exception:
        matrix_size = qr.version * 4 + 17
    available = HEAD_WIDTH_DOTS - margin * 2
    scale = max(1, available // matrix_size)
    qr_size = matrix_size * scale
    if qr_size > available:
        raise ValueError(
            f"二维码模块尺寸 {matrix_size} 超过 96 点打印宽度；请缩短内容或降低纠错等级"
        )

    png = io.BytesIO()
    qr.save(png, kind="png", scale=scale, border=0, dark="black", light="white")
    png.seek(0)
    qr_image = Image.open(png).convert("L")

    if caption:
        caption_font = load_font(font_path, font_size)
        temp = Image.new("L", (10, 10), 255)
        temp_draw = ImageDraw.Draw(temp)
        caption_lines = wrap_text(caption, caption_font, available)
        caption_height = sum(
            temp_draw.textbbox((0, 0), line, font=caption_font)[3] + 2
            for line in caption_lines
        )
        height = margin * 2 + qr_size + caption_height + 2
        canvas = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
        canvas.paste(qr_image, (margin, margin))
        draw = ImageDraw.Draw(canvas)
        y = margin + qr_size + 2
        for line in caption_lines:
            width = draw.textlength(line, font=caption_font)
            x = (HEAD_WIDTH_DOTS - width) // 2
            draw.text((x, y), line, font=caption_font, fill=0)
            y += draw.textbbox((0, 0), line, font=caption_font)[3] + 2
    else:
        canvas = Image.new("L", (HEAD_WIDTH_DOTS, qr_size + margin * 2), 255)
        canvas.paste(qr_image, (margin, margin))
    return canvas


def _barcode_module_code(data: str, barcode_format: str) -> str:
    """Return a barcode as a string of 0/1 printer modules."""
    try:
        barcode_class = barcode_library.get_barcode_class(barcode_format)
        built = barcode_class(data).build()
    except Exception as exc:
        raise ValueError(f"无法生成 {barcode_format} 条码: {exc}") from exc

    if isinstance(built, (list, tuple)):
        code = "".join(str(item) for item in built)
    else:
        code = str(built)
    if not code or any(char not in "01" for char in code):
        raise ValueError(f"条码类型 {barcode_format} 没有返回可用的模块数据")
    return code


def _text_block_height(lines: list[str], font) -> int:
    if not lines:
        return 0
    temp = Image.new("L", (10, 10), 255)
    draw = ImageDraw.Draw(temp)
    return sum(draw.textbbox((0, 0), line, font=font)[3] + 2 for line in lines)


def _draw_text_lines(image: Image.Image, lines: list[str], font, y: int) -> int:
    if not lines:
        return y
    draw = ImageDraw.Draw(image)
    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(((image.width - width) // 2, y), line, font=font, fill=0)
        y += draw.textbbox((0, 0), line, font=font)[3] + 2
    return y


def _font_line_height(font) -> int:
    try:
        ascent, descent = font.getmetrics()
        return max(1, ascent + descent)
    except Exception:
        return max(1, int(font.size * 1.25))


def _barcode_text_rows(
    data: str,
    caption: str | None,
    show_text: bool,
    data_font,
    caption_font,
    max_width: int,
    same_line: bool = False,
) -> list[list[tuple[str, object]]]:
    """Build barcode text rows; each row may contain differently sized segments."""
    data_lines = wrap_text(str(data), data_font, max_width) if show_text and data else []
    caption_lines = wrap_text(str(caption), caption_font, max_width) if caption else []

    rows: list[list[tuple[str, object]]] = []
    if same_line:
        for data_line, caption_line in zip_longest(data_lines, caption_lines, fillvalue=""):
            segments: list[tuple[str, object]] = []
            if data_line:
                segments.append((data_line, data_font))
            if caption_line:
                if segments:
                    segments.append(("  " + caption_line, caption_font))
                else:
                    segments.append((caption_line, caption_font))
            if segments:
                rows.append(segments)
    else:
        rows.extend([[(line, data_font)] for line in data_lines])
        rows.extend([[(line, caption_font)] for line in caption_lines])
    return rows


def _text_rows_metrics(rows: list[list[tuple[str, object]]]) -> tuple[int, int, list[int], list[int]]:
    if not rows:
        return 0, 0, [], []
    temp = Image.new("L", (10, 10), 255)
    draw = ImageDraw.Draw(temp)
    row_widths: list[int] = []
    row_heights: list[int] = []
    for row in rows:
        width = sum(draw.textlength(text, font=font) for text, font in row)
        width += 6 * (len(row) - 1)
        row_widths.append(max(1, round(width)))
        row_heights.append(max(_font_line_height(font) for _, font in row) + 2)
    return max(row_widths), sum(row_heights), row_widths, row_heights


def _draw_text_rows(image: Image.Image, rows: list[list[tuple[str, object]]], y: int = 0) -> int:
    if not rows:
        return y
    _, _, row_widths, row_heights = _text_rows_metrics(rows)
    draw = ImageDraw.Draw(image)
    for row, row_width, row_height in zip(rows, row_widths, row_heights):
        max_ascent = max((font.getmetrics()[0] for _, font in row if hasattr(font, "getmetrics")), default=0)
        x = (image.width - row_width) // 2
        for text, font in row:
            ascent, _descent = font.getmetrics()
            draw.text((x, y + max_ascent - ascent), text, font=font, fill=0)
            x += draw.textlength(text, font=font) + 6
        y += row_height
    return y


def _render_vertical_barcode(
    code: str,
    data: str,
    show_text: bool,
    caption: str | None,
    font_path: str | None,
    font_size: int,
    same_line: bool = False,
    data_font_size: int | None = None,
    caption_font_size: int | None = None,
    module_scale: int = 3,
    quiet_modules: int = 8,
    bar_height: int = 44,
) -> Image.Image:
    """Render a barcode rotated so bar widths run along the label length.

    The 12 mm / 96-dot head is too narrow for many horizontal barcodes at a
    scannable module width. Rendering the symbol vertically gives each module
    3 dots while keeping the bar height inside the head width. Human-readable
    text is rotated with the symbol so it stays parallel to the bars.
    """
    data_font = load_font(font_path, data_font_size or font_size)
    caption_font = load_font(font_path, caption_font_size or font_size)
    quiet = quiet_modules * module_scale
    symbol_height = len(code) * module_scale + quiet * 2
    max_text_width = HEAD_WIDTH_DOTS - 8

    text_rows = _barcode_text_rows(
        data,
        caption,
        show_text,
        data_font,
        caption_font,
        max_text_width,
        same_line=same_line,
    )

    height = symbol_height
    rotated_text: Image.Image | None = None
    if text_rows:
        text_width, text_height, _, _ = _text_rows_metrics(text_rows)
        text_canvas = Image.new("L", (text_width, text_height), 255)
        _draw_text_rows(text_canvas, text_rows, 0)
        # The symbol is the horizontal barcode rotated 90 degrees clockwise;
        # rotate the text the same way so it remains parallel to the bars.
        rotated_text = text_canvas.transpose(Image.Transpose.ROTATE_270)

    image = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
    draw = ImageDraw.Draw(image)

    if rotated_text is None:
        bar_x = (HEAD_WIDTH_DOTS - bar_height) // 2
    else:
        # Put the rotated text strip on the left and shift the symbol right.
        # Shrink the symbol only when the selected font sizes require it.
        text_x = 2
        bar_x = rotated_text.width + 6
        bar_height = min(bar_height, HEAD_WIDTH_DOTS - bar_x - 2)
        bar_height = max(16, bar_height)

    for index, module in enumerate(code):
        if module == "1":
            y0 = quiet + index * module_scale
            draw.rectangle(
                (bar_x, y0, bar_x + bar_height - 1, y0 + module_scale - 1),
                fill=0,
            )

    if rotated_text is not None:
        text_y = quiet + (len(code) * module_scale - rotated_text.height) // 2
        text_y = max(0, min(text_y, height - rotated_text.height))
        image.paste(rotated_text, (text_x, text_y))
    return image


def make_barcode_label(
    data: str,
    barcode_format: str = "code128",
    margin: int = 4,
    show_text: bool = True,
    caption: str | None = None,
    font_path: str | None = None,
    font_size: int = 12,
    auto_vertical: bool = True,
    text_layout: str = "stacked",
    data_font_size: int | None = None,
    caption_font_size: int | None = None,
) -> Image.Image:
    code = _barcode_module_code(data, barcode_format)
    same_line = text_layout in ("same_line", "same-line")
    available = HEAD_WIDTH_DOTS - margin * 2
    quiet_modules = 8
    horizontal_scale = max(0, (available - quiet_modules * 2) // len(code))

    if auto_vertical and horizontal_scale < 2:
        return _render_vertical_barcode(
            code,
            data,
            show_text=show_text,
            caption=caption,
            font_path=font_path,
            font_size=font_size,
            same_line=same_line,
            data_font_size=data_font_size,
            caption_font_size=caption_font_size,
        )

    data_font = load_font(font_path, data_font_size or font_size)
    caption_font = load_font(font_path, caption_font_size or font_size)
    scale = max(1, horizontal_scale)
    quiet = quiet_modules * scale
    symbol_width = len(code) * scale + quiet * 2
    bar_height = min(32, max(12, available // 3))

    text_rows = _barcode_text_rows(
        data,
        caption,
        show_text,
        data_font,
        caption_font,
        available,
        same_line=same_line,
    )
    _, text_height, _, _ = _text_rows_metrics(text_rows)

    height = margin * 2 + bar_height + (2 + text_height if text_rows else 0)
    image = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
    draw = ImageDraw.Draw(image)
    x0 = (HEAD_WIDTH_DOTS - symbol_width) // 2
    y0 = margin
    for index, module in enumerate(code):
        if module == "1":
            x = x0 + quiet + index * scale
            draw.rectangle(
                (x, y0, x + scale - 1, y0 + bar_height - 1),
                fill=0,
            )

    if text_rows:
        _draw_text_rows(image, text_rows, y0 + bar_height + 2)
    return image


# ---------------------------------------------------------------------------
# Print job
# ---------------------------------------------------------------------------

def build_print_frames(image: Image.Image, include_handshake: bool = True) -> list[tuple[str, bytes]]:
    raster, height = image_to_raster(image)
    frames: list[tuple[str, bytes]] = []
    if include_handshake:
        frames.append(("set_connected", cmd_set_connected()))
        frames.append(("query_status", cmd_query_status()))
        frames.append(("sync_time", cmd_sync_time()))
    frames.append(("print_start", cmd_print_start(height)))
    frames.extend(
        (f"data_{index + 1}", frame)
        for index, frame in enumerate(cmd_print_data_chunks(raster))
    )
    frames.append(("finalize", cmd_print_finalize(False)))
    return frames


def send_frames(frames: list[tuple[str, bytes]], device_path: str, frame_delay: float, data_delay: float) -> None:
    printer = UsbPrinter(device_path)
    print(f"设备: {device_path}")
    try:
        for name, frame in frames:
            written = printer.write(frame)
            print(f"发送 {name}: {written} 字节")
            time.sleep(data_delay if name.startswith("data_") else frame_delay)
        print("打印任务已发送完成")
    finally:
        printer.close()


def describe_frames(frames: list[tuple[str, bytes]]) -> None:
    for name, frame in frames:
        preview = frame[:24].hex()
        suffix = "..." if len(frame) > 24 else ""
        print(f"{name}: {len(frame)} 字节  {preview}{suffix}")


# ---------------------------------------------------------------------------
# Composite labels: multiple text lines + QR + barcode on one label
# ---------------------------------------------------------------------------

def make_composite_label(
    text: str | None = None,
    qr: str | None = None,
    barcode: str | None = None,
    caption: str | None = None,
    margin: int = 4,
    font_path: str | None = None,
    font_size: int = 18,
    align: str = "center",
    line_spacing: float = 1.15,
    qr_error: str = "m",
    barcode_format: str = "code128",
    show_barcode_text: bool = True,
    spacing: int = 4,
) -> Image.Image:
    """Render a vertical stack of text, QR, barcode, and caption."""
    parts: list[Image.Image] = []

    if text and text.strip():
        parts.append(make_text_label(
            text,
            font_path=font_path,
            font_size=font_size,
            margin=margin,
            align=align,
            line_spacing=line_spacing,
        ))

    if qr and str(qr).strip():
        parts.append(make_qr_label(
            str(qr),
            margin=margin,
            error_level=qr_error,
            caption=None,
            font_path=font_path,
            font_size=max(8, font_size // 2),
        ))

    if barcode and str(barcode).strip():
        parts.append(make_barcode_label(
            str(barcode),
            barcode_format=barcode_format,
            margin=margin,
            show_text=show_barcode_text,
            caption=None,
            font_path=font_path,
            font_size=max(8, font_size // 2),
        ))

    if caption and caption.strip():
        parts.append(make_text_label(
            caption,
            font_path=font_path,
            font_size=max(8, font_size // 2),
            margin=margin,
            align=align,
            line_spacing=line_spacing,
        ))

    if not parts:
        raise ValueError("组合标签至少需要文字、二维码或条形码中的一项")

    total_height = sum(part.height for part in parts) + spacing * (len(parts) - 1)
    canvas = Image.new("L", (HEAD_WIDTH_DOTS, total_height), 255)
    y = 0
    for part in parts:
        canvas.paste(part, (0, y))
        y += part.height + spacing
    return canvas


# ---------------------------------------------------------------------------
# Batch label rendering from CSV / Excel rows
# ---------------------------------------------------------------------------

class _SafeRowFormat(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _render_template_value(value, row: dict):
    if isinstance(value, str):
        return value.format_map(_SafeRowFormat(row))
    if isinstance(value, list):
        return [_render_template_value(item, row) for item in value]
    if isinstance(value, dict):
        return {key: _render_template_value(item, row) for key, item in value.items()}
    return value


def _normalize_row(row) -> dict:
    return {str(key): "" if value is None else str(value) for key, value in row.items()}


def parse_csv_text(text: str) -> list[dict]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [_normalize_row(row) for row in reader]
    return [row for row in rows if any(value.strip() for value in row.values())]


def parse_table_file(path: str | Path) -> list[dict]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        text = path.read_text(encoding="utf-8-sig")
        return parse_csv_text(text)
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        workbook = load_workbook(path, data_only=True, read_only=True)
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return []
        headers = [
            str(cell).strip() if cell is not None else f"列{index + 1}"
            for index, cell in enumerate(header_row)
        ]
        parsed = []
        for values in rows:
            row = _normalize_row(dict(zip(headers, values)))
            if any(value.strip() for value in row.values()):
                parsed.append(row)
        return parsed
    raise ValueError("批量导入仅支持 CSV/TSV/TXT 或 XLSX/XLSM 文件")


def parse_table_data_url(data_url: str, filename: str = "") -> list[dict]:
    if not data_url.startswith("data:"):
        raise ValueError("上传文件必须是 data URL")
    header, encoded = data_url.split(",", 1)
    if "base64" not in header:
        raise ValueError("上传文件必须是 base64 data URL")
    raw = base64.b64decode(encoded)
    suffix = Path(filename).suffix.lower()
    if not suffix:
        mime = header[5:header.find(";")] if ";" in header else header[5:]
        suffix = {
            "text/csv": ".csv",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        }.get(mime, ".csv")
    temp_path = ROOT / ".batch_upload" / f"upload{suffix}"
    temp_path.parent.mkdir(exist_ok=True)
    temp_path.write_bytes(raw)
    return parse_table_file(temp_path)


def default_batch_template(columns: list[str]) -> dict:
    lines = [f"{column}: {{{column}}}" for column in columns]
    return {"text": "\n".join(lines)}


def render_batch_spec(template, row: dict) -> dict:
    if isinstance(template, str):
        template = json.loads(template)
    if not isinstance(template, dict):
        raise ValueError("批量模板必须是 JSON 对象")
    spec = _render_template_value(template, row)
    if not isinstance(spec, dict):
        raise ValueError("批量模板渲染结果必须是 JSON 对象")
    spec.setdefault("kind", "composite")
    return spec


def _composite_from_spec(spec: dict, args) -> Image.Image:
    image = make_composite_label(
        text=spec.get("text"),
        qr=spec.get("qr"),
        barcode=spec.get("barcode"),
        caption=spec.get("caption"),
        margin=int(spec.get("margin", args.margin)),
        font_path=args.font,
        font_size=int(spec.get("font_size", args.font_size)),
        align=spec.get("align", args.align),
        line_spacing=float(spec.get("line_spacing", args.line_spacing)),
        qr_error=spec.get("qr_error", args.qr_error),
        barcode_format=spec.get("barcode_format", args.barcode_format),
        show_barcode_text=not spec.get("no_barcode_text", args.no_barcode_text),
        spacing=int(spec.get("spacing", args.spacing)),
    )
    return rotate_print_image(image, spec.get("rotation", args.rotation))


def run_batch(args) -> int:
    rows = parse_table_file(args.file)
    if not rows:
        raise ValueError("表格中没有可用数据行")
    start = max(1, int(args.start))
    selected = rows[start - 1:]
    if args.limit is not None:
        selected = selected[:max(0, int(args.limit))]
    if not selected:
        raise ValueError("按起始行/数量筛选后没有可打印的数据行")

    if args.template:
        template = json.loads(Path(args.template).read_text(encoding="utf-8"))
    elif args.text or args.qr or args.barcode:
        template = {
            "text": args.text or "",
            "qr": args.qr or "",
            "barcode": args.barcode or "",
        }
    else:
        template = default_batch_template(list(rows[0].keys()))
    if args.caption:
        template["caption"] = args.caption

    images = []
    for index, row in enumerate(selected, start=start):
        spec = render_batch_spec(template, row)
        spec["kind"] = "composite"
        images.append(_composite_from_spec(spec, args))

    total_frames = 0
    total_bytes = 0
    frame_counts = []
    for image in images:
        frames = build_print_frames(image, include_handshake=not args.no_handshake)
        frame_counts.append(frames)
        total_frames += len(frames)
        total_bytes += sum(len(frame) for _, frame in frames)

    print(f"表格总行数: {len(rows)}；本次标签数: {len(images)}")
    print(f"打印帧总数: {total_frames}；总字节数: {total_bytes}")

    if args.preview_only:
        preview_path = Path(args.preview) if args.preview else ROOT / f"mijia_label_batch_row_{start}_preview.png"
        images[0].save(preview_path)
        print(f"已保存第 {start} 行预览: {preview_path}")
        return 0
    if args.dry_run:
        for index, frames in zip(range(start, start + len(images)), frame_counts):
            print(f"第 {index} 行: {len(frames)} 帧")
        return 0

    device_path = resolve_device_path(args.device_path)
    usb = UsbPrinter(device_path)
    print(f"设备: {device_path}")
    try:
        for index, (row, frames) in enumerate(zip(selected, frame_counts), start=start):
            print(f"打印第 {index} 行: {dict(row)}")
            for name, frame in frames:
                written = usb.write(frame)
                print(f"  发送 {name}: {written} 字节")
                time.sleep(args.data_delay if name.startswith("data_") else args.frame_delay)
            if index != start + len(selected) - 1:
                time.sleep(args.label_delay)
        print("批量打印任务已全部发送完成")
    finally:
        usb.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_common_options(parser: argparse.ArgumentParser, suppressed: bool = False) -> None:
    default = argparse.SUPPRESS if suppressed else None

    def optional_default(value):
        return argparse.SUPPRESS if suppressed else value

    parser.add_argument("--device-path", default=default, help="直接指定 USB 设备路径")
    parser.add_argument("--margin", type=int, default=optional_default(4), help="标签左右/上下留白，单位：点")
    parser.add_argument("--invert", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="反转黑白，适合白字黑底图片")
    parser.add_argument("--threshold", type=int, default=optional_default(128), help="黑白阈值 0-255")
    parser.add_argument("--preview-only", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="只生成预览图，不打印")
    parser.add_argument("--preview", default=default, help="预览图输出路径")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="生成打印帧但不写入 USB")
    parser.add_argument("--no-handshake", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="跳过 set_connected/status/time 命令")
    parser.add_argument("--frame-delay", type=float, default=optional_default(0.08), help="普通命令帧之间的间隔秒数")
    parser.add_argument("--data-delay", type=float, default=optional_default(0.35), help="图像数据帧之间的间隔秒数")
    parser.add_argument("--font", default=default, help="TrueType/OpenType 字体文件路径")
    parser.add_argument("--font-size", type=int, default=optional_default(18), help="文字字号，单位：点")
    parser.add_argument("--align", choices=["left", "center", "right"], default=optional_default("center"), help="文字对齐方式")
    parser.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=optional_default(0), help="打印内容逆时针旋转角度")
    parser.add_argument("--line-spacing", type=float, default=optional_default(1.15), help="文字行距倍数")
    parser.add_argument("--qr-error", choices=["l", "m", "q", "s"], default=optional_default("m"), help="二维码纠错等级")
    parser.add_argument("--barcode-format", default=optional_default("code128"), help="条形码格式")
    parser.add_argument("--no-barcode-text", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="条形码下方不显示数字文本")
    parser.add_argument("--caption", default=default, help="二维码/条码下方的文字说明")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mijia MJBQDYJ1-WC USB label printer tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_options(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    text_parser = subparsers.add_parser("text", help="打印文字标签")
    add_common_options(text_parser, suppressed=True)
    text_parser.add_argument("lines", nargs="+", help="文字，可传多个参数表示多行")

    image_parser = subparsers.add_parser("image", help="打印图片标签")
    add_common_options(image_parser, suppressed=True)
    image_parser.add_argument("path", help="PNG/JPG/BMP 等图片路径")

    qr_parser = subparsers.add_parser("qr", help="打印二维码")
    add_common_options(qr_parser, suppressed=True)
    qr_parser.add_argument("data", help="二维码内容，例如网址或文本")

    barcode_parser = subparsers.add_parser("barcode", help="打印条形码")
    add_common_options(barcode_parser, suppressed=True)
    barcode_parser.add_argument("data", help="条形码内容")
    barcode_parser.add_argument(
        "--text-layout",
        choices=["stacked", "same-line"],
        default="stacked",
        help="条码文字排列方式：stacked=分行，same-line=与条码内容同行",
    )
    barcode_parser.add_argument("--data-font-size", type=int, help="条码内容文字字号")
    barcode_parser.add_argument("--caption-font-size", type=int, help="条码附近附加文字字号")

    composite_parser = subparsers.add_parser("composite", aliases=["combo"], help="打印文字+二维码+条形码组合标签")
    add_common_options(composite_parser, suppressed=True)
    composite_parser.add_argument("--text", help="文字内容，可用 \\n 表示换行")
    composite_parser.add_argument("--qr", help="二维码内容")
    composite_parser.add_argument("--barcode", help="条形码内容")
    composite_parser.add_argument("--spacing", type=int, default=4, help="组合元素之间的空白，单位：点")

    batch_parser = subparsers.add_parser("batch", help="从 CSV/Excel 批量打印标签")
    add_common_options(batch_parser, suppressed=True)
    batch_parser.add_argument("file", help="CSV/TSV/TXT/XLSX/XLSM 文件路径")
    batch_parser.add_argument("--template", help="JSON 模板文件，值中可用 {列名} 占位")
    batch_parser.add_argument("--text", help="每行文字模板，例如：{名称}\\n{负责人}")
    batch_parser.add_argument("--qr", help="每行二维码模板，例如：{链接}")
    batch_parser.add_argument("--barcode", help="每行条形码模板，例如：{编号}")
    batch_parser.add_argument("--start", type=int, default=1, help="从第几行数据开始，默认 1")
    batch_parser.add_argument("--limit", type=int, help="最多打印多少张")
    batch_parser.add_argument("--spacing", type=int, default=4, help="组合元素之间的空白，单位：点")
    batch_parser.add_argument("--label-delay", type=float, default=1.0, help="相邻两张标签之间的间隔秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.margin <= 40:
        raise ValueError("margin 必须在 0-40 之间")
    if not 0 <= args.threshold <= 255:
        raise ValueError("threshold 必须在 0-255 之间")

    if args.command == "text":
        image = make_text_label(
            "\n".join(args.lines),
            font_path=args.font,
            font_size=args.font_size,
            margin=args.margin,
            align=args.align,
            line_spacing=args.line_spacing,
        )
    elif args.command == "image":
        image = Image.open(args.path).convert("L")
        if args.invert:
            image = ImageOps.invert(image)
        # Recreate through raster conversion path so threshold is applied.
        raster, height = image_to_raster(image, threshold=args.threshold, invert=False)
        canvas = Image.new("L", (HEAD_WIDTH_DOTS, height), 255)
        pixels = canvas.load()
        for y in range(height):
            for x in range(HEAD_WIDTH_DOTS):
                byte = raster[y * BYTES_PER_ROW + (x >> 3)]
                if byte & (0x80 >> (x & 7)):
                    pixels[x, y] = 0
        image = canvas
    elif args.command == "qr":
        image = make_qr_label(
            args.data,
            margin=args.margin,
            error_level=args.qr_error,
            caption=args.caption,
            font_path=args.font,
            font_size=max(8, args.font_size // 2),
        )
    elif args.command == "barcode":
        image = make_barcode_label(
            args.data,
            barcode_format=args.barcode_format,
            margin=args.margin,
            show_text=not args.no_barcode_text,
            caption=args.caption,
            font_path=args.font,
            font_size=max(8, args.font_size // 2),
            text_layout=args.text_layout,
            data_font_size=args.data_font_size,
            caption_font_size=args.caption_font_size,
        )
    elif args.command in ("composite", "combo"):
        text = args.text.replace("\\n", "\n") if args.text else None
        if not any([text, args.qr, args.barcode, args.caption]):
            raise ValueError("组合标签至少需要 --text、--qr、--barcode 或 --caption 中的一项")
        image = make_composite_label(
            text=text,
            qr=args.qr,
            barcode=args.barcode,
            caption=args.caption,
            margin=args.margin,
            font_path=args.font,
            font_size=args.font_size,
            align=args.align,
            line_spacing=args.line_spacing,
            qr_error=args.qr_error,
            barcode_format=args.barcode_format,
            show_barcode_text=not args.no_barcode_text,
            spacing=args.spacing,
        )
    elif args.command == "batch":
        return run_batch(args)
    else:
        raise ValueError(f"未知命令: {args.command}")

    image = rotate_print_image(image, args.rotation)
    preview_path = Path(args.preview) if args.preview else ROOT / f"mijia_label_{args.command}_preview.png"
    image.save(preview_path)
    print(f"预览: {preview_path}  尺寸: {image.width}x{image.height}")

    frames = build_print_frames(image, include_handshake=not args.no_handshake)
    total_bytes = sum(len(frame) for _, frame in frames)
    print(f"打印帧: {len(frames)} 个，总计 {total_bytes} 字节")

    if args.preview_only:
        return 0
    if args.dry_run:
        describe_frames(frames)
        return 0

    device_path = resolve_device_path(args.device_path)
    send_frames(frames, device_path, args.frame_delay, args.data_delay)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
