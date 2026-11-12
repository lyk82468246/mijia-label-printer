@echo off
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
python mijia_label.py composite --text "设备A-01`n张三 / 实验室" --qr "https://example.com/a01" --barcode "A0001"
