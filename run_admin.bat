@echo off
rem Relaunches the Network Monitor with Administrator rights so Windows 11
rem exposes WiFi details (SSID, signal %, channel). Approve the UAC prompt.
rem pythonw.exe = windowless interpreter, so no console lingers behind the GUI
powershell -NoProfile -Command "Start-Process pythonw -ArgumentList '\"%~dp0network_monitor.py\"' -Verb RunAs -WorkingDirectory '%~dp0'"
