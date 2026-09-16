@echo off
rem Who wants an offer: one double click starts the database, the server,
rem the dashboard and the local agent watcher. See wwao/up.py.
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Не найден uv - менеджер Python-зависимостей проекта.
  echo Установите его: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run python -m wwao up
echo.
echo Окно можно закрыть.
pause
