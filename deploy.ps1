<#
.SYNOPSIS
    把本目录的 private_companion 插件源码同步到各个 NA 实例，并校验三台一致。

.DESCRIPTION
    角色内容（世界观 / 场景池 / 事件池 / 触发别名）现在都在插件数据目录里，
    不在源码里；所以源码可以三台共用，本脚本只同步代码、不碰数据文件。

.EXAMPLE
    .\deploy.ps1                  # 同步到三台并重启
    .\deploy.ps1 -NoRestart       # 只复制，不重启
    .\deploy.ps1 -Targets NA3     # 只同步 NA3
#>
param(
    [string]$SourceDir = $PSScriptRoot,
    [string[]]$Targets = @("NA1", "NA2", "NA3"),
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"

# 实例名 -> 容器内 NA 数据目录
$INSTANCES = @{
    "NA1" = "/var/lib/docker/nekro_agent_data"
    "NA2" = "/var/lib/docker/nekro_agent_data2"
    "NA3" = "/var/lib/docker/nekro_agent_data3"
}

# 需要同步的文件（webui.html 也在内，方便改了前端一起发）
$FILES = @(
    "__init__.py", "plugin.py", "core.py", "router.py", "state.py", "handlers.py",
    "day_card.py", "proactive.py", "busy_gate.py", "chronotype.py",
    "plan_diversity.py", "proactive_queue.py", "webui.html", "README.md", "LICENSE"
)

$pluginSub = "/plugins/workdir/private_companion"

foreach ($name in $Targets) {
    if (-not $INSTANCES.ContainsKey($name)) { throw "未知实例：$name" }
}
if (-not (Test-Path (Join-Path $SourceDir "core.py"))) {
    throw "SourceDir 里找不到 core.py：$SourceDir"
}

# ---------- 1) 复制 ----------
foreach ($name in $Targets) {
    $dest = "$($INSTANCES[$name])$pluginSub"
    Write-Host "==> $name"
    foreach ($f in $FILES) {
        $src = Join-Path $SourceDir $f
        if (-not (Test-Path $src)) { Write-Warning "  跳过（源里没有）：$f"; continue }
        docker cp $src "${name}:$dest/$f" | Out-Null
    }
    # 清掉字节码，避免旧 .pyc 干扰
    docker exec $name sh -c "rm -rf $dest/__pycache__" | Out-Null
    $chk = docker exec $name sh -c "cd $dest && python3 -m py_compile *.py && echo OK"
    if ($chk -notmatch "OK") { throw "$name 语法检查失败" }
    Write-Host "    已复制，py_compile OK"
}

# ---------- 2) 重启 ----------
if (-not $NoRestart) {
    Write-Host "==> 重启 $($Targets -join ' ')"
    docker restart @Targets | Out-Null
    Start-Sleep -Seconds 45
}

# ---------- 3) 校验三台源码一致 ----------
Write-Host ""
Write-Host "==> 一致性校验"
$bad = 0
foreach ($f in $FILES) {
    $row = @()
    foreach ($name in $Targets) {
        $h = (docker exec $name md5sum "$($INSTANCES[$name])$pluginSub/$f" 2>$null) -split '\s+' | Select-Object -First 1
        $row += if ($h) { $h.Substring(0, 8) } else { "缺失" }
    }
    $same = ($row | Select-Object -Unique).Count -eq 1
    if (-not $same) { $bad++ }
    Write-Host ("    {0,-22} {1}   {2}" -f $f, ($row -join "  "), $(if ($same) { "一致" } else { "** 不一致 **" }))
}
if ($bad -gt 0) { Write-Warning "$bad 个文件在实例间不一致" } else { Write-Host "    全部一致 ✓" }

# ---------- 4) 数据文件概览（只读，不修改）----------
Write-Host ""
Write-Host "==> 各实例角色内容（数据目录，不随源码同步）"
foreach ($name in $Targets) {
    $dd = "$($INSTANCES[$name])/plugin_data/xiaojiu.private_companion"
    $ws = docker exec $name sh -c "wc -c < $dd/world_stage.txt 2>/dev/null || echo 0"
    $card = docker exec $name sh -c "wc -c < $dd/day_card.json 2>/dev/null || echo 0"
    $al = docker exec $name sh -c "wc -c < $dd/trigger_aliases.json 2>/dev/null || echo 0"
    Write-Host ("    {0}  world_stage {1,6} B   day_card {2,6} B   aliases {3,6} B" -f `
        $name, $ws.Trim(), $card.Trim(), $al.Trim())
}
