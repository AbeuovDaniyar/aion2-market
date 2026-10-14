@echo off
rem Builds dist\AION2MarketAssistant.exe: one file, Python and every library inside,
rem plus a copy of your current data\market.db as the starting history for new installs.
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
%PY% -m pip install -q pyinstaller || goto :error
%PY% packaging\make_icon.py || goto :error
%PY% packaging\make_seed.py || goto :error
set SEED=
if exist packaging\seed\market.db set SEED=--add-data "packaging\seed;seed"
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name AION2MarketAssistant ^
  --icon packaging\app.ico ^
  --add-data "app\db\schema.sql;app\db" %SEED% ^
  --exclude-module pytest --exclude-module respx --exclude-module datamodel_code_generator ^
  packaging\launcher.py || goto :error
echo.
echo Built dist\AION2MarketAssistant.exe
exit /b 0
:error
echo Build failed.
exit /b 1
