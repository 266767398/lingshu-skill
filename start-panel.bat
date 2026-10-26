@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

set "PY="
where python >nul 2>nul && set "PY=python"
if "%PY%"=="" (
  where py >nul 2>nul && set "PY=py -3"
)

if "%PY%"=="" (
  echo.
  echo [错误] 没有找到 Python。
  echo 请先安装 Python 3.9 及以上版本，安装时勾选 "Add Python to PATH"。
  echo 下载：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

if not exist "scripts\panel_server.py" (
  echo.
  echo [错误] 找不到 scripts\panel_server.py
  echo 请确认 start-panel.bat 和 scripts 文件夹在同一个技能目录里。
  echo.
  pause
  exit /b 1
)

echo.
echo  灵枢 · 记忆面板启动中……
echo  地址：http://127.0.0.1:%PORT%
echo  关闭这个窗口即可停止服务。
echo.

%PY% "scripts\panel_server.py" --port %PORT%
if errorlevel 1 (
  echo.
  echo [提示] 启动失败。常见原因：
  echo   1. 端口 %PORT% 被占用 —— 换一个端口：start-panel.bat 8766
  echo   2. 防火墙拦截 —— 面板只监听本机 127.0.0.1，一般不需要放行
  echo   3. Python 版本过低 —— 需要 3.9 及以上
  echo.
  pause
)

endlocal
