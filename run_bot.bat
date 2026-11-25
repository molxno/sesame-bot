@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo No se encontro el entorno .venv
    echo Crea uno con: python -m venv .venv
    echo Luego: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo Falta el archivo .env con DISCORD_TOKEN
    pause
    exit /b 1
)

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0bot.py"
