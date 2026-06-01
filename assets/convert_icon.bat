@echo off
title Convert PNG to ICO
echo Make sure you have placed your new image as "raw_icon.png" in the assets folder!
echo.
cd /d "%~dp0\.."
python -c "from PIL import Image; img = Image.open(r'assets\raw_icon.png').convert('RGBA'); img.save(r'assets\icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]); print('Success! Generated assets\icon.ico')"
echo.
echo Now you can rebuild the EXE to apply the new icon using pyinstaller.
pause
