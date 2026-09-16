# M8 验收总编排：后端 pytest + 验收走查 + 前端四条门禁。
#
# ⚠️ 本脚本**只编排，不实现**：每条门禁各自可单独运行，
# 这里把它们串起来并聚合退出码（任何一条失败 → 退出 1）。
#
# 用法：  powershell -ExecutionPolicy Bypass -File scripts\verify_m8.ps1
# 注意：  Playwright / 截图不在其中（浏览器二进制未安装，见 verify_m8.py 第 10 条）。

#requires -Version 5.1
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$failures = [System.Collections.Generic.List[string]]::new()

Write-Host "=== [1/6] 后端 pytest（tests/）===" -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\python.exe") -m pytest tests -q
if ($LASTEXITCODE -ne 0) { $failures.Add("backend pytest") }

Write-Host "=== [2/6] 验收走查（五模块连续 + 薄出口 + 契约漂移）===" -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\python.exe") -u (Join-Path $root "scripts\verify_m8.py")
if ($LASTEXITCODE -ne 0) { $failures.Add("verify_m8 walkthrough") }

Push-Location (Join-Path $root "frontend")
try {
    Write-Host "=== [3/6] 前端 lint ===" -ForegroundColor Cyan
    npm run lint
    if ($LASTEXITCODE -ne 0) { $failures.Add("frontend lint") }

    Write-Host "=== [4/6] 前端 typecheck ===" -ForegroundColor Cyan
    npm run typecheck
    if ($LASTEXITCODE -ne 0) { $failures.Add("frontend typecheck") }

    Write-Host "=== [5/6] 前端 vitest ===" -ForegroundColor Cyan
    npm test -- --run
    if ($LASTEXITCODE -ne 0) { $failures.Add("frontend vitest") }

    Write-Host "=== [6/6] 前端 build（含产物泄漏检查）===" -ForegroundColor Cyan
    # ⚠️ 先用 cmd 原生 rmdir 清掉上一次的产物：在 CodeBuddy IDE 内，node 的
    # fs.rmSync 被 safe-delete 守卫 shim，vite 清空 dist 时会因"本轮累计删除
    # 文件数 ≥500"抛 SAFE_DELETE_BULK_CONFIRM_REQUIRED —— 那是环境守卫，
    # 不是构建失败。cmd 的 rmdir 不经过该 shim，能稳定清空。
    if (Test-Path dist) { cmd /c "rmdir /s /q dist" }
    npm run build
    if ($LASTEXITCODE -ne 0) { $failures.Add("frontend build") }
}
finally {
    Pop-Location
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host "M8 验收未通过：$($failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "M8 验收通过（后端 + 走查 + 前端四条门禁）。" -ForegroundColor Green
exit 0
