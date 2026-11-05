@echo off
cd /d "%~dp0"
title TheSmarties Arcade

set PY=python
where py >nul 2>nul && set PY=py
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython

if not exist .env echo DISCORD_TOKEN=PASTE-YOUR-TOKEN-HERE> .env
findstr /c:"PASTE-YOUR-TOKEN-HERE" .env >nul 2>nul
if errorlevel 1 goto run
echo The bot token is missing.
echo Notepad is opening the .env file. Replace PASTE-YOUR-TOKEN-HERE with your bot token,
echo save with Ctrl+S, then close Notepad.
start /wait notepad .env

:run
echo Installing what the bot needs. The first run takes about a minute...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 echo The install step had a problem. Trying to start anyway...
echo.
echo Starting the bot. Keep this window open - closing it turns the bot off.
echo.
%PY% bot.py
echo.
echo The bot has stopped. Read the message above.
goto end

:nopython
echo Python is not installed on this PC.
echo 1. Go to https://www.python.org/downloads/ and download Python.
echo 2. In the installer, tick "Add python.exe to PATH", then click Install Now.
echo 3. Double-click start.bat again.

:end
echo.
pause
