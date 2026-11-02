@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
rem Lan dau: tu tao moi truong Python (.venv) va cai thu vien. Cac lan sau mo thang trang chinh.
if not exist ".venv\Scripts\python.exe" (
  echo Lan dau mo: dang cai dat moi truong cho phan mem, mat vai phut...
  py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul && py -3 -m venv .venv
  if not exist ".venv\Scripts\python.exe" python -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul && python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo KHONG TIM THAY PYTHON 3.11 TRO LEN tren may nay.
  echo Cai Python tu https://www.python.org/downloads/ ^(danh dau "Add python.exe to PATH"^),
  echo roi bam dup lai file nay.
  pause
  exit /b 1
)
if not exist ".venv\installed.txt" (
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo CAI THU VIEN CHUA XONG. Kiem tra ket noi mang roi bam dup lai file nay.
    pause
    exit /b 1
  )
  copy /y requirements.txt ".venv\installed.txt" >nul
)
fc /b requirements.txt ".venv\installed.txt" >nul 2>nul || (
  echo Danh sach thu vien da thay doi, dang cap nhat...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt && copy /y requirements.txt ".venv\installed.txt" >nul
)
title Trang tim kiem - dong cua so nay de tat
".venv\Scripts\python.exe" scripts\web.py
pause
