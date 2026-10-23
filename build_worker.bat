@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
    goto found
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=python"
    goto found
)
echo Python 3.10+ was not found. Install Python and add it to PATH.
exit /b 1
:found
%PYTHON% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed. Run: pip install pyinstaller
    exit /b 1
)
pushd "%~dp0"
%PYTHON% -m PyInstaller --onefile --name sharecompute-worker worker.py
if errorlevel 1 (
    popd
    exit /b 1
)
echo Built: %~dp0dist\sharecompute-worker.exe
popd
exit /b 0
