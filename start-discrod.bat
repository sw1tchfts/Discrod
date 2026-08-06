@echo off
setlocal
rem Start Discrod. Double-click friendly: on first run this creates the
rem Python virtual environment and installs dependencies, then launches the
rem app. Subsequent runs go straight to launch (deps reinstall automatically
rem if requirements.txt changed).

cd /d "%~dp0app"

rem --- find a Python interpreter (python on PATH, else the py launcher) -----
set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10+ is required but was not found.
        echo Install it from https://www.python.org/downloads/
        echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then
        echo run this file again.
        pause
        exit /b 1
    )
    set "PYTHON=py -3"
)

rem --- create the venv on first run -----------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :fail
)

rem --- install deps on first run or when requirements.txt changed -----------
fc /b requirements.txt ".venv\requirements.stamp" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(one-time, a few minutes^)...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
    copy /y requirements.txt ".venv\requirements.stamp" >nul
)

rem --- launch ----------------------------------------------------------------
".venv\Scripts\python.exe" -m discrod
if errorlevel 1 (
    echo.
    echo Discrod exited with an error - see the messages above.
    pause
)
exit /b 0

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
