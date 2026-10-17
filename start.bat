@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import pyaudiowpatch" 2>nul || (echo 首次執行,安裝 PyAudioWPatch... && python -m pip install -r requirements.txt)
python server.py %*
pause
