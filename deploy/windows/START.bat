@echo off
rem Double-click to deploy QAU CS Advisor. Asks for administrator rights.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
