@echo off
REM Generate FIR IP cores from coefficient files
REM Run this from the pyrpl\fpga\ip directory

echo ============================================================
echo Generating FIR IP cores from coefficient files...
echo ============================================================

REM Run Vivado in batch mode with the TCL script
vivado -mode batch -source generate_fir_ips.tcl

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: FIR IP generation failed!
    exit /b 1
)

echo.
echo FIR IP generation completed successfully!
echo.
pause
