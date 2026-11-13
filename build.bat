@echo off
rem Builds dist\Minideck.exe (the program) and dist\MinideckSetup.exe (installer for client PCs).
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed --name Minideck ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import PIL._tkinter_finder --collect-all customtkinter ^
  --icon assets\icon.ico --add-data "assets\icon.ico;assets" --add-data "assets\icon.png;assets" ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  run_minideck.py || goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed --name MinideckSetup ^
  --hidden-import PIL._tkinter_finder --collect-all customtkinter ^
  --icon assets\icon.ico --add-data "assets\icon.ico;assets" --add-data "assets\icon.png;assets" ^
  --add-data "dist\Minideck.exe;payload" ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  --exclude-module numpy --exclude-module mss --exclude-module pynput ^
  setup_minideck.py || goto :error

echo.
echo Done: dist\Minideck.exe and dist\MinideckSetup.exe
exit /b 0

:error
echo Build failed.
exit /b 1
