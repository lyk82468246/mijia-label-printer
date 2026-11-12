@echo off
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
python mijia_label_server.py --host 0.0.0.0 --port 8071
