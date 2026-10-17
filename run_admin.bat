@echo off
rem Relaunches the Network Monitor with Administrator rights so Windows 11
rem exposes WiFi details (SSID, signal %, channel). Approve the UAC prompt.
powershell -NoProfile -Command "Start-Process python -ArgumentList '\"%~dp0network_monitor.py\"' -Verb RunAs -WorkingDirectory '%~dp0'"
