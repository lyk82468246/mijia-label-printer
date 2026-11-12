# Mijia USB Label Printer

Private tooling for printing labels on a **Xiaomi Mijia MJBQDYJ1-WC label printer** over USB on Windows.

The printer enumerates as a standard USB printer-class device:

- Device name: `Mijia Label Printer`
- USB IDs: `VID 2717`, `PID 507D`
- USB class: `Class 07 / SubClass 01 / Protocol 02`
- Windows driver: `usbprint.inf`
- Print head width: `96 dots`

This project sends the printer's reverse-engineered command framing through the Windows USB printer interface. It does **not** require the Xiaomi mobile app or Bluetooth.

## Features

- Print text labels
- Print QR codes
- Print barcodes
- Print images
- Print composite labels: multiple text lines + QR code + barcode
- Batch printing from CSV / TSV / XLSX / XLSM
- Local web label designer with live preview
- Phone access through a LAN QR code shown by the web UI
- Preview-only and dry-run modes
- Chinese text support using Windows fonts such as Microsoft YaHei

## Repository layout

```text
mijia_label.py                     CLI tool and shared print protocol code
mijia_label_server.py              Local web server / USB print backend
mijia_label_web/                   Browser label designer
diagnostics/                       Low-level USB probe and early print tests
docs/protocol-findings.md          Protocol notes for the Mijia printer
examples/                          Batch printing examples
scripts/                           Windows helper scripts
```

## Install

Use Python 3.12 or newer on Windows.

```powershell
python -m pip install -r requirements.txt
```

External dependencies:

- Pillow
- pycryptodome
- segno
- python-barcode
- openpyxl

## Command-line usage

Run from the repository root.

### Text

```powershell
python mijia_label.py text "Hello Mijia"
python mijia_label.py text "Line 1" "Line 2" --preview-only
```

### QR code

```powershell
python mijia_label.py qr "https://example.com"
```

With text below the QR code:

```powershell
python mijia_label.py qr "https://example.com" --caption "Device A-01"
```

### Barcode

```powershell
python mijia_label.py barcode "1234567890"
```

Hide the number under the barcode:

```powershell
python mijia_label.py barcode "1234567890" --no-barcode-text
```

### Composite label

Text, QR code, and barcode on the same label:

```powershell
python mijia_label.py composite --text "Device A-01`nZhang San" --qr "https://example.com/a01" --barcode "A0001"
```

Generate a preview without printing:

```powershell
python mijia_label.py composite --text "Device A-01" --qr "https://example.com/a01" --barcode "A0001" --preview-only
```

### Batch printing

Use the example Excel file:

```powershell
python mijia_label.py batch examples/batch_sample.xlsx --template examples/batch_template.json
```

Print only the first row:

```powershell
python mijia_label.py batch examples/batch_sample.xlsx --template examples/batch_template.json --limit 1
```

Dry-run without writing to USB:

```powershell
python mijia_label.py batch examples/batch_sample.csv --template examples/batch_template.json --dry-run
```

The template maps spreadsheet columns to label fields:

```json
{
  "text": "{名称}\n负责人：{负责人}",
  "qr": "{链接}",
  "barcode": "{编号}"
}
```

Missing columns become empty strings. Empty QR, barcode, or text fields are skipped in composite labels.

## Local web designer

Start the server:

```powershell
python mijia_label_server.py --host 0.0.0.0 --port 8071
```

Open locally:

```text
http://127.0.0.1:8071/
```

The page shows a QR code containing the LAN URL, for example:

```text
http://<your-lan-ip>:8071/
```

A phone on the same Wi-Fi / LAN can scan that QR code and use the same designer. The browser sends print requests to the Python backend, which writes to the USB printer.

The web UI supports text, QR codes, barcodes, images, composite labels, batch preview, batch dry-run, and batch printing.

## Useful options

| Option | Description |
|---|---|
| `--preview-only` | Save/show a preview without printing |
| `--dry-run` | Build USB frames without opening the device |
| `--margin N` | Label margin in dots |
| `--threshold N` | Black/white threshold, default `128` |
| `--invert` | Invert black and white |
| `--font PATH` | TrueType/OpenType font file |
| `--font-size N` | Font size in dots |
| `--align left/center/right` | Text alignment |
| `--rotation 0/90/180/270` | Rotate label content counterclockwise |
| `--line-spacing X` | Text line spacing multiplier |
| `--frame-delay X` | Delay between command frames |
| `--data-delay X` | Delay between image data frames |
| `--device-path PATH` | Override automatic USB device discovery |
| `--qr-error l/m/q/s` | QR error correction level |
| `--barcode-format NAME` | Barcode format, default `code128` |
| `--no-barcode-text` | Do not print the barcode number |
| `--caption TEXT` | Additional caption text |
| `--spacing N` | Gap between composite label sections |
| `--start N` | Batch starting row |
| `--limit N` | Maximum batch labels |
| `--label-delay X` | Delay between printed labels |

Options may appear before or after the subcommand.

## Protocol background

The printer protocol was documented by the public reverse-engineering project:

- <https://github.com/jasiek/xiaomi-mjbqdyj1-wc-reveng>

Key details:

- Frame format: `A3 20 <length LE16> <AES-CBC payload> <CRC32 LE>`
- Cipher: AES-128-CBC with zero padding
- CRC32: IEEE CRC32 with custom initial value `0x76953521`
- Raster: 96 dots wide, 1 bpp, MSB is the leftmost dot
- Large raster data is split into 1800-byte chunks

This repository adapts those BLE-framed commands for the printer's USB printer-class interface.

## Troubleshooting

### Device cannot be opened

1. Confirm the printer appears in Device Manager as `Mijia Label Printer`.
2. Confirm no other print job is using the device.
3. Reconnect the USB cable if the device path changed.
4. Try the diagnostic scripts in `diagnostics/`.

### Blank label

- Try `--invert` for white-on-black source images.
- Adjust `--threshold`.
- Use a high-contrast black-and-white image.

### QR code too large

The print head is only 96 dots wide. Shorten the QR content or use lower error correction:

```powershell
python mijia_label.py qr "https://example.com" --qr-error l
```

### Chinese text is garbled

Specify a Chinese-capable font:

```powershell
python mijia_label.py text "中文测试" --font "C:\Windows\Fonts\msyh.ttc"
```

### Phone cannot open the web page

1. Confirm the phone and PC are on the same LAN.
2. Allow Python to listen on port `8071` in Windows Firewall.
3. Open the LAN URL shown on the page, not `localhost`.
