@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat. Hay bam dup "1 - Mo trang chinh.bat" mot lan truoc (lan dau no tu cai dat).
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\kb.py mcp-config
echo.
echo Muon tu them vao ung dung AI, go so roi Enter (bo trong = khong them gi):
echo   1 = Claude Desktop   2 = Codex   3 = Gemini CLI   4 = Cursor
set /p CHON=Chon: 
if "%CHON%"=="1" ".venv\Scripts\python.exe" scripts\kb.py mcp-config --install claude-desktop
if "%CHON%"=="2" ".venv\Scripts\python.exe" scripts\kb.py mcp-config --install codex
if "%CHON%"=="3" ".venv\Scripts\python.exe" scripts\kb.py mcp-config --install gemini-cli
if "%CHON%"=="4" ".venv\Scripts\python.exe" scripts\kb.py mcp-config --install cursor
pause
