#!/usr/bin/env python3
"""Local web server and USB print backend for the Mijia label printer."""

from __future__ import annotations

import argparse
import base64
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

import mijia_label as printer
import segno

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "mijia_label_web"
PRINT_LOCK = threading.Lock()
SERVER: ThreadingHTTPServer | None = None


def get_lan_ip() -> str:
    hostname = socket.gethostname()
    candidates: list[str] = []
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except OSError:
        pass

    try:
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_socket.connect(("8.8.8.8", 80))
        candidates.append(temp_socket.getsockname()[0])
        temp_socket.close()
    except OSError:
        pass

    for ip in candidates:
        if ip.startswith("192.168.") or ip.startswith("10."):
            return ip
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return ip
            except (ValueError, IndexError):
                pass
    return candidates[0] if candidates else "127.0.0.1"


def decode_data_url(data_url: str) -> Image.Image:
    if not data_url.startswith("data:"):
        raise ValueError("图片必须是 data URL")
    header, encoded = data_url.split(",", 1)
    if "base64" not in header:
        raise ValueError("图片 data URL 必须是 base64")
    raw = base64.b64decode(encoded)
    return Image.open(io.BytesIO(raw)).convert("L")


def raster_to_image(raster: bytes, height: int) -> Image.Image:
    image = Image.new("L", (printer.HEAD_WIDTH_DOTS, height), 255)
    pixels = image.load()
    for y in range(height):
        for x in range(printer.HEAD_WIDTH_DOTS):
            byte = raster[y * printer.BYTES_PER_ROW + (x >> 3)]
            if byte & (0x80 >> (x & 7)):
                pixels[x, y] = 0
    return image


def prepare_uploaded_image(payload: dict) -> Image.Image:
    image = decode_data_url(payload["image"])
    if payload.get("invert"):
        image = ImageOps.invert(image)
    raster, height = printer.image_to_raster(
        image,
        threshold=int(payload.get("threshold", 128)),
        invert=False,
    )
    return raster_to_image(raster, height)


def render_label_image(payload: dict) -> Image.Image:
    kind = payload.get("kind")
    margin = int(payload.get("margin", 4))
    font_path = payload.get("font") or None
    font_size = int(payload.get("font_size", 18))
    align = payload.get("align", "center")
    line_spacing = float(payload.get("line_spacing", 1.15))
    caption = payload.get("caption") or None

    if kind == "text":
        text = payload.get("text", "")
        if isinstance(text, list):
            text = "\n".join(str(line) for line in text)
        return printer.make_text_label(
            str(text),
            font_path=font_path,
            font_size=font_size,
            margin=margin,
            align=align,
            line_spacing=line_spacing,
        )
    if kind == "qr":
        return printer.make_qr_label(
            str(payload.get("data", "")),
            margin=margin,
            error_level=payload.get("qr_error", "m"),
            caption=caption,
            font_path=font_path,
            font_size=max(8, font_size // 2),
        )
    if kind == "barcode":
        return printer.make_barcode_label(
            str(payload.get("data", "")),
            barcode_format=payload.get("barcode_format", "code128"),
            margin=margin,
            show_text=not payload.get("no_barcode_text", False),
            caption=caption,
            font_path=font_path,
            font_size=max(8, font_size // 2),
        )
    if kind == "composite":
        return printer.make_composite_label(
            text=payload.get("text") or None,
            qr=payload.get("qr") or None,
            barcode=payload.get("barcode") or None,
            caption=caption,
            margin=margin,
            font_path=font_path,
            font_size=font_size,
            align=align,
            line_spacing=line_spacing,
            qr_error=payload.get("qr_error", "m"),
            barcode_format=payload.get("barcode_format", "code128"),
            show_barcode_text=not payload.get("no_barcode_text", False),
            spacing=int(payload.get("spacing", 4)),
        )
    if kind == "image":
        return prepare_uploaded_image(payload)
    raise ValueError(f"不支持的标签类型: {kind}")


def image_from_payload(payload: dict) -> Image.Image:
    image = render_label_image(payload)
    return printer.rotate_print_image(image, payload.get("rotation", 0))


def table_rows_from_payload(payload: dict) -> list[dict]:
    if payload.get("file"):
        return printer.parse_table_data_url(payload["file"], payload.get("filename", "upload.csv"))
    if payload.get("csv_text"):
        return printer.parse_csv_text(payload["csv_text"])
    raise ValueError("请上传 CSV/Excel 文件或粘贴 CSV/TSV 表格")


def batch_template_from_payload(payload: dict, rows: list[dict]) -> dict:
    template = payload.get("template")
    if template in (None, "", {}):
        return printer.default_batch_template(list(rows[0].keys()))
    if isinstance(template, str):
        template = json.loads(template)
    if not isinstance(template, dict):
        raise ValueError("批量模板必须是 JSON 对象")
    return template


def batch_common_options(payload: dict) -> dict:
    return {
        "margin": payload.get("margin", 4),
        "font_size": payload.get("font_size", 18),
        "align": payload.get("align", "center"),
        "rotation": payload.get("rotation", 0),
        "line_spacing": payload.get("line_spacing", 1.15),
        "qr_error": payload.get("qr_error", "m"),
        "barcode_format": payload.get("barcode_format", "code128"),
        "no_barcode_text": payload.get("no_barcode_text", False),
        "spacing": payload.get("spacing", 4),
        "caption": payload.get("caption"),
    }


def batch_selected_rows(payload: dict, rows: list[dict]) -> list[dict]:
    start = max(1, int(payload.get("start", 1)))
    selected = rows[start - 1:]
    limit = payload.get("limit")
    if limit not in (None, "", 0):
        selected = selected[:max(0, int(limit))]
    if not selected:
        raise ValueError("按起始行/数量筛选后没有可打印的数据行")
    return selected


def batch_label_payload(row: dict, template: dict, common: dict) -> dict:
    spec = printer.render_batch_spec(template, row)
    return {**common, **spec}


def print_batch_payload(payload: dict) -> dict:
    rows = table_rows_from_payload(payload)
    template = batch_template_from_payload(payload, rows)
    common = batch_common_options(payload)
    selected = batch_selected_rows(payload, rows)
    device_path = printer.resolve_device_path(payload.get("device_path"))
    include_handshake = not payload.get("no_handshake", False)
    frame_delay = float(payload.get("frame_delay", 0.08))
    data_delay = float(payload.get("data_delay", 0.35))
    label_delay = float(payload.get("label_delay", 1.0))

    results = []
    usb = printer.UsbPrinter(device_path)
    try:
        for offset, row in enumerate(selected):
            index = int(payload.get("start", 1)) + offset
            image = image_from_payload(batch_label_payload(row, template, common))
            frames = printer.build_print_frames(image, include_handshake=include_handshake)
            bytes_sent = 0
            for name, frame in frames:
                written = usb.write(frame)
                bytes_sent += written
                print(f"batch row {index}: send {name}, {written} bytes")
                import time as _time
                _time.sleep(data_delay if name.startswith("data_") else frame_delay)
            results.append({
                "row": index,
                "frames": len(frames),
                "bytes": bytes_sent,
                "height": image.height,
            })
            if offset != len(selected) - 1:
                import time as _time
                _time.sleep(label_delay)
    finally:
        usb.close()
    return {
        "ok": True,
        "printed": True,
        "device_path": device_path,
        "total_rows": len(rows),
        "printed_rows": len(results),
        "results": results,
    }


def png_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def qr_png_bytes(url: str) -> bytes:
    qr = segno.make(url, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=6, border=2, dark="black", light="white")
    return buffer.getvalue()


class LabelRequestHandler(BaseHTTPRequestHandler):
    server_version = "MijiaLabelWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 32 * 1024 * 1024:
            raise ValueError("请求体为空或超过 32MB 限制")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_static("index.html")
            return
        if path.startswith("/api/status"):
            device_paths = printer.find_usb_printer_paths()
            ip = get_lan_ip()
            port = SERVER.server_port if SERVER else 8071
            self.send_json(200, {
                "ok": True,
                "device_found": bool(device_paths),
                "device_paths": device_paths,
                "lan_ip": ip,
                "port": port,
                "url": f"http://{ip}:{port}/",
            })
            return
        if path.startswith("/api/site-qr"):
            query = parse_qs(parsed.query)
            url = query.get("url", [None])[0]
            if not url:
                ip = get_lan_ip()
                port = SERVER.server_port if SERVER else 8071
                url = f"http://{ip}:{port}/"
            self.send_bytes(200, qr_png_bytes(url), "image/png")
            return
        if path.startswith("/static/"):
            self.send_static(path[len("/static/"):])
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/preview":
                image = image_from_payload(payload)
                self.send_json(200, {
                    "ok": True,
                    "width": image.width,
                    "height": image.height,
                    "image": png_data_url(image),
                })
                return
            if path in ("/api/print", "/api/dry-run"):
                image = image_from_payload(payload)
                frames = printer.build_print_frames(image, include_handshake=not payload.get("no_handshake", False))
                total_bytes = sum(len(frame) for _, frame in frames)
                device_path = printer.resolve_device_path(payload.get("device_path"))
                if path == "/api/dry-run":
                    self.send_json(200, {
                        "ok": True,
                        "dry_run": True,
                        "device_path": device_path,
                        "frames": len(frames),
                        "bytes": total_bytes,
                        "width": image.width,
                        "height": image.height,
                    })
                    return
                with PRINT_LOCK:
                    printer.send_frames(
                        frames,
                        device_path,
                        float(payload.get("frame_delay", 0.08)),
                        float(payload.get("data_delay", 0.35)),
                    )
                self.send_json(200, {
                    "ok": True,
                    "printed": True,
                    "device_path": device_path,
                    "frames": len(frames),
                    "bytes": total_bytes,
                    "width": image.width,
                    "height": image.height,
                })
                return
            if path == "/api/batch/preview":
                rows = table_rows_from_payload(payload)
                template = batch_template_from_payload(payload, rows)
                common = batch_common_options(payload)
                selected = batch_selected_rows(payload, rows)
                first_row = selected[0]
                image = image_from_payload(batch_label_payload(first_row, template, common))
                self.send_json(200, {
                    "ok": True,
                    "total_rows": len(rows),
                    "selected_rows": len(selected),
                    "preview_row": int(payload.get("start", 1)),
                    "row": first_row,
                    "width": image.width,
                    "height": image.height,
                    "image": png_data_url(image),
                })
                return
            if path == "/api/batch/dry-run":
                rows = table_rows_from_payload(payload)
                template = batch_template_from_payload(payload, rows)
                common = batch_common_options(payload)
                selected = batch_selected_rows(payload, rows)
                results = []
                total_frames = 0
                total_bytes = 0
                for offset, row in enumerate(selected):
                    index = int(payload.get("start", 1)) + offset
                    image = image_from_payload(batch_label_payload(row, template, common))
                    frames = printer.build_print_frames(image, include_handshake=not payload.get("no_handshake", False))
                    bytes_count = sum(len(frame) for _, frame in frames)
                    total_frames += len(frames)
                    total_bytes += bytes_count
                    results.append({"row": index, "frames": len(frames), "bytes": bytes_count, "height": image.height})
                self.send_json(200, {
                    "ok": True,
                    "dry_run": True,
                    "total_rows": len(rows),
                    "selected_rows": len(selected),
                    "total_frames": total_frames,
                    "total_bytes": total_bytes,
                    "results": results,
                })
                return
            if path == "/api/batch/print":
                with PRINT_LOCK:
                    result = print_batch_payload(payload)
                self.send_json(200, result)
                return
            self.send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})

    def send_static(self, relative_path: str) -> None:
        requested = Path(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            self.send_json(403, {"ok": False, "error": "forbidden"})
            return
        file_path = WEB_DIR / requested
        if not file_path.is_file():
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }.get(file_path.suffix.lower(), "application/octet-stream")
        self.send_bytes(200, file_path.read_bytes(), content_type)


def main() -> int:
    global SERVER
    parser = argparse.ArgumentParser(description="Mijia label printer local web designer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8071)
    args = parser.parse_args()

    if not WEB_DIR.is_dir():
        raise SystemExit(f"Web directory not found: {WEB_DIR}")

    device_paths = printer.find_usb_printer_paths()
    print("Mijia USB printer found:", device_paths or "not found")
    SERVER = ThreadingHTTPServer((args.host, args.port), LabelRequestHandler)
    ip = get_lan_ip()
    print(f"Local web page: http://127.0.0.1:{args.port}/")
    print(f"LAN web page:   http://{ip}:{args.port}/")
    print("Scan the QR code shown on the page from a phone on the same Wi-Fi network.")
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        print("server stopped")
    finally:
        SERVER.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
