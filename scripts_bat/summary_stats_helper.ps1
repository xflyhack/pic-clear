param(
    [string]$StatsRoot,
    [string]$PathMode,
    [string]$PathFilter,
    [string]$ExportCsv
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Test-Path $StatsRoot)) {
    Write-Host ""
    Write-Host "[错误] StatsRoot 不存在: $StatsRoot"
    exit 2
}

# ---- 递归收集所有 machine_id_*.csv ----
$csvs = @(
    Get-ChildItem -Path $StatsRoot -Recurse -File -Filter "machine_id_*.csv" -ErrorAction SilentlyContinue
)

if ($csvs.Count -eq 0) {
    Write-Host ""
    Write-Host "[结果] $StatsRoot 下递归没找到 machine_id_*.csv"
    Write-Host "       (确认 dedupe_watcher/append_stats 是否往这个 root 写过数据)"
    exit 0
}

# ---- 读所有行，附加 _machine / _sourceCsv ----
$allRows = New-Object System.Collections.ArrayList
foreach ($csv in $csvs) {
    $machine = $csv.BaseName -replace '^machine_id_',''
    try {
        $rows = Import-Csv -Path $csv.FullName
    } catch {
        Write-Host "[警告] 读 csv 失败: $($csv.FullName) - $($_.Exception.Message)"
        continue
    }
    foreach ($r in $rows) {
        $r | Add-Member -NotePropertyName _machine   -NotePropertyValue $machine        -Force
        $r | Add-Member -NotePropertyName _sourceCsv -NotePropertyValue $csv.FullName   -Force
        [void]$allRows.Add($r)
    }
}

if ($allRows.Count -eq 0) {
    Write-Host ""
    Write-Host "[结果] 找到 csv 文件但没有可解析的数据行"
    exit 0
}

# ---- 路径过滤 ----
$filtered = $allRows
if ($PathMode -eq "exact") {
    $target = $PathFilter.TrimEnd('\')
    $filtered = $allRows | Where-Object { $_.abs_path.TrimEnd('\') -eq $target }
} elseif ($PathMode -eq "prefix") {
    $selfPath = $PathFilter.TrimEnd('\')
    $prefix   = $selfPath + '\'
    $filtered = $allRows | Where-Object {
        $ap = $_.abs_path
        ($ap -eq $selfPath) -or ($ap.StartsWith($prefix))
    }
}

if (@($filtered).Count -eq 0) {
    Write-Host ""
    Write-Host "[结果] 路径过滤后无数据"
    exit 0
}

# ---- 汇总核心：按 abs_path 分组 ----
$groups = $filtered | Group-Object abs_path

$folderCount   = $groups.Count
$totalOriginal = 0
$totalDeleted  = 0
$totalRemain   = 0

$byMachine = @{}

foreach ($g in $groups) {
    $sorted     = $g.Group | Sort-Object timestamp
    $firstTotal = [int]$sorted[0].total
    $lastRemain = [int]$sorted[-1].remain
    $sumDeleted = ($g.Group | Measure-Object -Property deleted -Sum).Sum
    if ($null -eq $sumDeleted) { $sumDeleted = 0 }

    $totalOriginal += $firstTotal
    $totalRemain   += $lastRemain
    $totalDeleted  += $sumDeleted

    $m = $sorted[-1]._machine
    if (-not $byMachine.ContainsKey($m)) {
        $byMachine[$m] = @{ folders = 0; deleted = 0; remain = 0; original = 0 }
    }
    $byMachine[$m].folders  += 1
    $byMachine[$m].original += $firstTotal
    $byMachine[$m].deleted  += $sumDeleted
    $byMachine[$m].remain   += $lastRemain
}

$deleteRatio = 0
if ($totalOriginal -gt 0) {
    $deleteRatio = $totalDeleted / $totalOriginal * 100
}

# ---- 屏幕输出 ----
Write-Host ""
Write-Host "============================================================"
Write-Host "  汇总结果"
Write-Host "============================================================"
Write-Host ""
Write-Host ("  统计根目录   : {0}" -f $StatsRoot)
Write-Host ("  扫到 csv     : {0} 个" -f $csvs.Count)
Write-Host ("  数据条目     : {0} 行" -f $filtered.Count)
Write-Host ("  涉及文件夹   : {0} 个" -f $folderCount)
Write-Host ("  参与的机器   : {0} 台" -f $byMachine.Count)
Write-Host ""
Write-Host "  [核心指标]"
Write-Host ("  原始图片总数 : {0:N0} 张   (每目录首次 total 求和)" -f $totalOriginal)
Write-Host ("  累计删除数   : {0:N0} 张   (所有 deleted 求和)"    -f $totalDeleted)
Write-Host ("  当前剩余     : {0:N0} 张   (每目录最后一次 remain 求和)" -f $totalRemain)
Write-Host ("  删除比例     : {0:F2}%%" -f $deleteRatio)
Write-Host ""

if ($byMachine.Count -gt 0) {
    Write-Host "  [按机器分]"
    foreach ($m in ($byMachine.Keys | Sort-Object)) {
        $s = $byMachine[$m]
        Write-Host ("    {0,-24}  文件夹 {1,4}  原图 {2,10:N0}  删除 {3,10:N0}  剩余 {4,10:N0}" -f $m, $s.folders, $s.original, $s.deleted, $s.remain)
    }
    Write-Host ""
}

# ---- 导出 CSV ----
if ($ExportCsv -eq "1") {
    $tsFile = Get-Date -Format "yyyyMMdd_HHmmss"
    $exportPath = Join-Path $StatsRoot ("summary_" + $tsFile + ".csv")
    try {
        $exportData = New-Object System.Collections.ArrayList

        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "统计根目录";     value = $StatsRoot   })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "涉及文件夹";     value = $folderCount })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "原始图片总数"; value = $totalOriginal })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "累计删除数";   value = $totalDeleted  })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "当前剩余";       value = $totalRemain   })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "删除比例%";     value = [math]::Round($deleteRatio, 2) })

        foreach ($m in ($byMachine.Keys | Sort-Object)) {
            $s = $byMachine[$m]
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "文件夹数"; value = $s.folders  })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "原图";     value = $s.original })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "删除";     value = $s.deleted  })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "剩余";     value = $s.remain   })
        }

        # 明细：每个文件夹一行
        foreach ($g in ($groups | Sort-Object Name)) {
            $sorted     = $g.Group | Sort-Object timestamp
            $firstTotal = [int]$sorted[0].total
            $lastRemain = [int]$sorted[-1].remain
            $sumDeleted = ($g.Group | Measure-Object -Property deleted -Sum).Sum
            if ($null -eq $sumDeleted) { $sumDeleted = 0 }
            $folderName = $sorted[-1].folder_name
            [void]$exportData.Add([PSCustomObject]@{
                section = "明细"
                key     = "$folderName | $($g.Name)"
                value   = "原图=$firstTotal 删除=$sumDeleted 剩余=$lastRemain"
            })
        }

        $exportData | Export-Csv -Path $exportPath -Encoding UTF8 -NoTypeInformation
        Write-Host ("[导出] {0}" -f $exportPath)
    } catch {
        Write-Host ("[错误] 导出失败: {0}" -f $_.Exception.Message)
    }
}
