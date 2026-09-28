@echo off
setlocal

REM 每日签到。挂 Windows 任务计划每天跑一次：
REM   schtasks /create /tn "router-checkin-daily" /tr "E:\router-checkin\tools\daily_checkin.cmd" /sc daily /st 08:10
REM
REM AnyRouter 不用 --login：把浏览器里的 session Cookie 填进配置的 cookies.session。
REM AgentRouter 用配置里的 username/password 每天自动登录；只有 profile 丢失或
REM 站点要求人工滑块时，才需要手工登录一次（普通浏览器窗口）：
REM   python tools\daily_checkin.py --login agentrouter-1
REM
REM 退出码：全部完成 0；有账号需要处理 1；配置错误或旧参数 --notify 2
REM （非 0 时任务计划会显示失败，便于发现）。通知用 --notify-url 或环境变量 CHECKIN_NOTIFY_URL。
REM
REM 这两个站都必须走代理，代理地址写在 tools\checkin_sites.json 每条配置里。
REM 脚本是独立项目，不依赖 wxbot 或 E:\newcheckin。

cd /d "%~dp0.."
set "PYTHON=C:\Python314\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%PYTHON%" tools\daily_checkin.py %*
set CODE=%ERRORLEVEL%

if %CODE% NEQ 0 echo [daily_checkin] 有账号需要处理，详见 logs\daily_checkin.log

endlocal & exit /b %CODE%
