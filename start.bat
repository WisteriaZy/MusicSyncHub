@echo off
rem ============================================================
rem  Music Sync Hub 启动脚本（Windows）
rem
rem    start.bat                 默认 127.0.0.1:8787
rem    start.bat 0.0.0.0 9000    让局域网其它机器也能访问
rem
rem  依赖：uv（https://docs.astral.sh/uv/）
rem  重要：本文件必须保存为 ANSI/GBK 编码。cmd.exe 用系统代码页
rem        读取批处理文件，存成 UTF-8（无 BOM）会让中文注释变成
rem        乱码并导致整段脚本语法错误、直接启动失败。
rem ============================================================
setlocal
cd /d "%~dp0"

set "HOST=%~1"
if "%HOST%"=="" set "HOST=127.0.0.1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8787"

rem ---- 已经在运行？直接打开界面，不再起第二个实例 ----
rem   双击第二次时，uvicorn 会因为端口被占用直接退出，窗口一闪就关，
rem   用户看到的现象就是"打不开"。这里先探测一次。
set "URLHOST=%HOST%"
if "%URLHOST%"=="0.0.0.0" set "URLHOST=127.0.0.1"
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://%URLHOST%:%PORT%/api/system'; if($r.StatusCode -eq 200){exit 0} }catch{}; exit 1" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo   Music Sync Hub 已经在运行，正在打开界面 ...
  echo   地址： http://%URLHOST%:%PORT%/
  echo   如果要重启，请先关掉正在运行的那个窗口 ^(Ctrl+C^)。
  echo.
  start "" "http://%URLHOST%:%PORT%/"
  ping -n 2 127.0.0.1 >nul
  exit /b 0
)

set "PY=%CD%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  where uv >nul 2>nul
  if errorlevel 1 (
    echo [错误] 没找到 Python 环境，也没找到 uv。
    echo         请先安装 uv:  powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo         然后在项目目录执行:  uv sync
    pause
    exit /b 1
  )
  echo 首次运行，正在准备依赖，请稍候...
  uv sync
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

if not exist "%PY%" (
  echo [错误] 依赖准备完成，但仍找不到 %PY%
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo   Music Sync Hub 已启动
echo   界面地址： http://%HOST%:%PORT%
echo   数据目录： %USERPROFILE%\.music-sync-hub
echo   停止服务： 按 Ctrl+C
echo.

"%PY%" -m app.main --host %HOST% --port %PORT%
if errorlevel 1 (
  echo.
  echo [错误] 服务没能启动。常见原因：
  echo         1. 端口 %PORT% 被别的程序占用 —— 换一个端口，例如： start.bat 127.0.0.1 8899
  echo         2. 依赖没装好 —— 在项目目录执行： uv sync
  echo.
  pause
  exit /b 1
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [错误] 服务异常退出，退出码 %RC%。上面是详细报错。
  pause
)
exit /b %RC%
