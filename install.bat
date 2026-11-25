@echo off
setlocal
cd /d "%~dp0"
echo === Rob Run Soundboard setup ===
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Install Python 3.12 from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo then run install.bat again.
  pause
  exit /b 1
)

echo Installing Python packages...
py -m pip install --user -r requirements.txt
if errorlevel 1 (
  echo Package install failed.
  pause
  exit /b 1
)

echo Creating a desktop shortcut...
for /f "delims=" %%P in ('py -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%P
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Rob Run Soundboard.lnk');$s.TargetPath='%PYW%';$s.Arguments='\"%~dp0soundboard.py\"';$s.WorkingDirectory='%~dp0';$s.Save()"

echo.
echo Done! Open "Rob Run Soundboard" from your desktop.
echo Don't forget VB-CABLE: https://vb-audio.com/Cable/  (see README)
pause
