@echo off
set SRC=%cd%
set DEST=C:\Users\aj92uwef\Documents\fpga_compilation\fpga

:: Remove old destination if exists
if exist "%DEST%" rmdir /s /q "%DEST%"

:: Copy folder
xcopy "%SRC%" "%DEST%" /E /I /Y

:: Go to new folder and run make.bat
cd /d "%DEST%"
call make.bat
