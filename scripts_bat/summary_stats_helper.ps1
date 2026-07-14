param(
    [string]$StatsRoot,
    [string]$DateMode,
    [string]$DateValue,
    [string]$PathMode,
    [string]$PathFilter,
    [string]$ExportCsv
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---- 找日期目录 ----
$dateDirs = @()
if ($DateMode -eq "one") {
    $d = Join-Path $StatsRoot $DateValue
    if (Test-Path $d) {
        $dateDirs = @($d)
    }
} elseif ($DateMode -eq "all") {
    $dateDirs = Get-ChildItem -Path $StatsRoot -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^\d{8}$' } |
                ForEach-Object { $_.FullName }
}

if ($dateDirs.Count -eq 0) {
    Write-Host ""
    Write-Host "[结果] 未找到任何符合条件的日期目录（$StatsRoot 下）"
    Write-Host "       提示：只统计增量新格式 YYYYMMDD 目录，存量'周一0714'目录不参与。"
    exit 0
}

# ---- 读所有 csv ----
$allRows = New-Object System.Collections.ArrayList
foreach ($dir in $dateDirs) {
    $csvs = Get-ChildItem -Path $dir -Filter "machine_id_*.csv" -File -ErrorAction SilentlyContinue
    foreach ($csv in $csvs) {
        $machine = $csv.BaseName -replace '^machine_id_',''
        $dateName = Split-Path $dir -Leaf
        try {
            $rows = Import-Csv -Path $csv.FullName
        } catch {
            Write-Host "[WARN] 读 csv 失败: $($csv.FullName) - $($_.Exception.Message)"
            continue
        }
        foreach ($r in $rows) {
            $r | Add-Member -NotePropertyName _machine -NotePropertyValue $machine -Force
            $r | Add-Member -NotePropertyName _date -NotePropertyValue $dateName -Force
            [void]$allRows.Add($r)
        }
    }
}

if ($allRows.Count -eq 0) {
    Write-Host ""
    Write-Host "[结果] 找到日期目录但没有 csv 记录"
    exit 0
}

# ---- 路径过滤 ----
$filtered = $allRows
if ($PathMode -eq "exact") {
    $target = $PathFilter.TrimEnd('\')
    $filtered = $allRows | Where-Object { $_.abs_path.TrimEnd('\') -eq $target }
} elseif ($PathMode -eq "prefix") {
    $prefix = $PathFilter.TrimEnd('\') + '\'
    $selfPath = $PathFilter.TrimEnd('\')
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

# ---- 汇总核心 ----
$groups = $filtered | Group-Object abs_path

$folderCount = $groups.Count
$totalOriginal = 0
$totalDeleted = 0
$totalRemain = 0

$byMachine = @{}

foreach ($g in $groups) {
    $sorted = $g.Group | Sort-Object timestamp
    $firstTotal = [int]$sorted[0].total
    $lastRemain = [int]$sorted[-1].remain
    $sumDeleted = ($g.Group | Measure-Object -Property deleted -Sum).Sum
    if ($null -eq $sumDeleted) { $sumDeleted = 0 }

    $totalOriginal += $firstTotal
    $totalRemain += $lastRemain
    $totalDeleted += $sumDeleted

    $m = $sorted[-1]._machine
    if (-not $byMachine.ContainsKey($m)) {
        $byMachine[$m] = @{ folders = 0; deleted = 0; remain = 0; original = 0 }
    }
    $byMachine[$m].folders += 1
    $byMachine[$m].original += $firstTotal
    $byMachine[$m].deleted += $sumDeleted
    $byMachine[$m].remain += $lastRemain
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
if ($DateMode -eq "one") {
    Write-Host ("  日期范围     : {0}" -f $DateValue)
} else {
    Write-Host ("  日期范围     : 全部历史（共 {0} 天）" -f $dateDirs.Count)
}
Write-Host ("  数据条目     : {0} 行原始记录" -f $filtered.Count)
Write-Host ("  涉及文件夹   : {0} 个" -f $folderCount)
Write-Host ("  参与的机器   : {0} 台" -f $byMachine.Count)
Write-Host ""
Write-Host "  [核心指标]"
Write-Host ("  原始图片总数 : {0:N0} 张   (每目录首次 total 求和)" -f $totalOriginal)
Write-Host ("  累计删除数   : {0:N0} 张   (所有 deleted 求和)" -f $totalDeleted)
Write-Host ("  当前剩余     : {0:N0} 张   (每目录最后一次 remain 求和)" -f $totalRemain)
Write-Host ("  删除比例     : {0:F2}%" -f $deleteRatio)
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

        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "涉及文件夹"; value = $folderCount })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "原始图片总数"; value = $totalOriginal })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "累计删除数"; value = $totalDeleted })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "当前剩余"; value = $totalRemain })
        [void]$exportData.Add([PSCustomObject]@{ section = "汇总"; key = "删除比例%"; value = [math]::Round($deleteRatio, 2) })

        foreach ($m in ($byMachine.Keys | Sort-Object)) {
            $s = $byMachine[$m]
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "文件夹数"; value = $s.folders })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "原图";     value = $s.original })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "删除";     value = $s.deleted })
            [void]$exportData.Add([PSCustomObject]@{ section = "按机器_$m"; key = "剩余";     value = $s.remain })
        }

        # 明细：每个文件夹一行
        foreach ($g in ($groups | Sort-Object Name)) {
            $sorted = $g.Group | Sort-Object timestamp
            $firstTotal = [int]$sorted[0].total
            $lastRemain = [int]$sorted[-1].remain
            $sumDeleted = ($g.Group | Measure-Object -Property deleted -Sum).Sum
            if ($null -eq $sumDeleted) { $sumDeleted = 0 }
            $folderName = $sorted[-1].folder_name
            [void]$exportData.Add([PSCustomObject]@{
                section = "明细"
                key = "$folderName | $($g.Name)"
                value = "原图=$firstTotal 删除=$sumDeleted 剩余=$lastRemain"
            })
        }

        $exportData | Export-Csv -Path $exportPath -Encoding UTF8 -NoTypeInformation
        Write-Host ("[导出] {0}" -f $exportPath)
    } catch {
        Write-Host ("[ERROR] 导出失败: {0}" -f $_.Exception.Message)
    }
}
