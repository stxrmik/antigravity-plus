@echo off
rem Antigravity Plus installer for Windows: install.cmd [--uninstall [--purge]]
where py >nul 2>nul && (py -3 "%~dp0install.py" %* & exit /b %errorlevel%)
where python >nul 2>nul && (python "%~dp0install.py" %* & exit /b %errorlevel%)
echo Python 3.8+ is required: install it from https://www.python.org/downloads/ (keep "Add python.exe to PATH").
exit /b 1
