@echo off
rem wisland one-click setup: creates .venv, makes sure pip exists, installs requirements.
rem This file is ASCII-only on purpose (cmd.exe parses .bat files with the system code page).
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo  ===== wisland setup =====
echo.

rem ---- 1. find Python 3.10+ ----
rem py launcher first, then every python.exe on PATH (the Microsoft Store stub fails the
rem version test and is skipped), then the usual install folders.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)"') do set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY call :try "%%p"
if not defined PY call :try "%LOCALAPPDATA%\Python\bin\python.exe"
if not defined PY for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PY call :try "%%d\python.exe"
if not defined PY for /d %%d in ("%ProgramFiles%\Python3*") do if not defined PY call :try "%%d\python.exe"
if not defined PY (
    echo [x] Python 3.10 or newer was not found.
    echo     Install it from https://www.python.org/downloads/windows/
    echo     and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    echo.
    pause
    exit /b 1
)
echo [1/4] Using %PY%
"%PY%" --version

rem ---- 2. virtual environment ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment .venv ...
    "%PY%" -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo [2/4] .venv already exists
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3. pip (venvs made by uv or with --without-pip have none) ----
"%VPY%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [3/4] pip missing, running ensurepip ...
    "%VPY%" -m ensurepip --upgrade
    if errorlevel 1 goto :fail
) else (
    echo [3/4] pip OK
)

rem ---- 4. dependencies (falls back to the Tsinghua mirror if PyPI is unreachable) ----
echo [4/4] Installing dependencies, this can take a few minutes ...
"%VPY%" -m pip install --upgrade pip >nul 2>nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] PyPI failed, retrying with the Tsinghua mirror ...
    "%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 goto :fail
)

echo.
echo Checking dependencies actually installed in .venv ...
"%VPY%" -c "import importlib.util,sys; mods=['PySide6','psutil','httpx','pycaw','comtypes','yaml','numpy','winrt.windows.media.control','soundcard','sherpa_onnx','huggingface_hub','fugashi','unidic_lite','genanki']; missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('Missing in .venv: '+', '.join(missing) if missing else 'Dependencies OK'); sys.exit(1 if missing else 0)"
if errorlevel 1 (
    echo.
    echo [x] pip finished, but the packages are still missing from .venv.
    echo     Remove the .venv folder and run setup.bat again, or check the pip output above.
    echo.
    pause
    exit /b 1
)

echo.
echo  Done! Double-click "wisland.vbs" to start wisland.
echo  Live Japanese subtitles will download the model automatically on first use.
echo.
choice /c YN /m "Start wisland now"
if errorlevel 2 exit /b 0
start "" wscript.exe "%~dp0wisland.vbs"
exit /b 0

:try
if not exist "%~1" exit /b 1
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=%~1"
exit /b 0

:fail
echo.
echo [x] Setup failed. Scroll up for the error message.
echo     If it is a network problem, try again later or use a proxy / mirror.
echo.
pause
exit /b 1
