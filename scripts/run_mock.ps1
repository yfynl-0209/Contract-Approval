# 启动 mock 审批系统（外部对接方），端口 8001
#
# 用法：  .\scripts\run_mock.ps1

# 无论从哪里调用，都先切到项目根目录 —— 否则 uvicorn 找不到 mock_approval 包
Set-Location (Join-Path $PSScriptRoot "..")

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[错误] 未找到虚拟环境：$python" -ForegroundColor Red
    Write-Host "       请先执行：python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

Write-Host "启动 mock 审批系统 -> http://127.0.0.1:8001/docs" -ForegroundColor Green
& $python -m uvicorn mock_approval.main:app --host 127.0.0.1 --port 8001 --reload
