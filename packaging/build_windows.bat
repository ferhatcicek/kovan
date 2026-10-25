@echo off
REM Kovan'i tek klasörlük Windows uygulaması olarak paketler (PyInstaller, onedir).
REM Kullanım: proje kökünde  packaging\build_windows.bat
python -m venv .build-venv || exit /b 1
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install . pyinstaller || exit /b 1
pyinstaller --noconfirm --clean --onedir --name Kovan ^
  --add-data "src\kovan\web\templates;kovan\web\templates" ^
  --add-data "src\kovan\web\static;kovan\web\static" ^
  --collect-submodules uvicorn ^
  --icon assets\kovan.ico ^
  packaging\kovan_entry.py || exit /b 1
echo.
echo Hazir: dist\Kovan\Kovan.exe
