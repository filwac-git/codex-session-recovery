@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" set "PYTHON_EXE=%BUNDLED_PYTHON%"

if not defined PYTHON_EXE (
  where py.exe >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py.exe"
    set "PYTHON_ARGS=-3"
  )
)

if not defined PYTHON_EXE (
  for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    echo %%P | findstr /i /c:"\WindowsApps\python.exe" >nul
    if errorlevel 1 if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
  )
)

if not defined PYTHON_EXE (
  echo 未找到 Python 3。请安装 Python 3.11 或使用 Codex 自带的 Python。
  pause
  exit /b 2
)

if not "%~1"=="" goto run_arguments

:menu
echo.
echo Codex 本地会话恢复工具
echo =======================
echo 1. 扫描本地会话（只读）
echo 2. 恢复到 ChatGPT 官方服务（请先完全退出 Codex）
echo 3. 验证恢复结果（只读）
echo 4. 退出
echo.
set /p "CHOICE=请选择 [1-4]: "

if "%CHOICE%"=="1" "%PYTHON_EXE%" %PYTHON_ARGS% "%SCRIPT_DIR%recover_codex_sessions.py" scan
if "%CHOICE%"=="2" "%PYTHON_EXE%" %PYTHON_ARGS% "%SCRIPT_DIR%recover_codex_sessions.py" repair
if "%CHOICE%"=="3" "%PYTHON_EXE%" %PYTHON_ARGS% "%SCRIPT_DIR%recover_codex_sessions.py" verify
if "%CHOICE%"=="4" exit /b 0
echo.
pause
goto menu

:run_arguments
"%PYTHON_EXE%" %PYTHON_ARGS% "%SCRIPT_DIR%recover_codex_sessions.py" %*
exit /b %errorlevel%
