@echo off
chcp 65001 >nul
title QunZhi V15 系统启动面板

echo ============================================
echo   群智 V15 · 科研决策预演智能体 · 一键启动
echo ============================================
echo.

echo [1/4] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH
    pause
    exit /b 1
)

echo [2/4] 检查依赖...
python -c "import fastapi, uvicorn, requests" >nul 2>&1
if errorlevel 1 (
    echo [提示] 缺少依赖，正在安装（仅需 fastapi / uvicorn / pydantic / requests）...
    pip install -r requirements.txt
    python -c "import fastapi, uvicorn, requests" >nul 2>&1
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试：pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo [3/4] 检查 API 密钥配置...
if not exist ".env" (
    echo [提示] 未发现 .env 文件 —— 系统仍可启动，但发起预演会失败。
    echo        请复制 .env.example 为 .env，并填入 DEEPSEEK_API_KEY 后重新运行本脚本。
    echo.
)

echo [4/4] 启动群智网关（:5020）...
echo.
echo 服务启动中，约 3-5 秒后浏览器会自动打开；若显示"无法访问"请刷新一次。
echo 控制台地址: http://127.0.0.1:5020
echo.
start "" /min cmd /c "timeout /t 4 >nul && start http://127.0.0.1:5020"
python app/main_gateway.py

pause
