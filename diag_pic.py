# -*- coding: utf-8 -*-
"""
diag_pic.py —— pic-clear 长路径 / 映射盘 打开诊断 GUI

单文件 tkinter GUI，用来定位 dedupe_pic 在堡垒机 Z: 长路径下打不开图片的根因。

用法:
    双击 diag_pic.exe -> 点"浏览"选一张真实的问题图片 -> 点"开始诊断" ->
    结果自动填到大文本框 -> 点"复制全部日志"或 Ctrl+A/Ctrl+C 复制文本 -> 贴给作者.

不做实际去重, 只做只读探测.
"""
from __future__ import annotations

import io
import os
import platform
import subprocess
import sys
import traceback
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter: {e}", file=sys.stderr)
    sys.exit(1)

# 版本号: CI 会覆盖 _version.py 里的 VERSION 成 tag 名
try:
    from _version import VERSION as _V
except Exception:
    _V = "dev"
APP_VERSION = _V

APP_TITLE = f"pic-clear 图片打开诊断工具  {APP_VERSION}"


# ---------------- 长路径 helper (v0.4.61: 抽到 winpath_util) ----------------
# _resolve_mapped_drive_to_unc 在 diag_pic 里语义是 verbose 版, 返回 (rc, unc);
# 其他工具用的是缓存版 (只返回 unc). 这里对应 winpath_util 的 verbose API.
from winpath_util import (
    normalize_windows_path as _normalize_windows_path,
    long_path_prefix as _long_path_prefix,
    resolve_mapped_drive_to_unc_verbose as _resolve_mapped_drive_to_unc,
    to_long_path as _to_long_path,
    _safe_is_file_impl,
    _find_first_file_w,
)


def _to_unc_full(p: str, unc_root: str) -> str:
    r"""Z:\aaa\bbb -> \\?\UNC\server\share\aaa\bbb, 供第 4 种打开方式使用."""
    rest = p[2:].lstrip("\\")
    return "\\\\?\\UNC\\" + unc_root.lstrip("\\") + "\\" + rest


# ---------------- Marker 诊断 (v0.4.65+) ----------------

def run_marker_diagnostics(markers_root: str, sample_limit: int = 5) -> str:
    r"""扫 markers_root 下所有 _done.marker, 对每个跑一遍完整判定, 输出统计报告.

    检查内容:
      - rglob 短路径 vs \?\ 长路径 数出的 marker 数量差 (定位 GUI 预扫抖动)
      - 每条 marker 的 4 种查询结果: 短 isfile / 长 isfile / 短 stat / 长 stat
      - 路径长度分布 (最短 / 最长 / 平均 / >=260 数量)
      - 前 N 条异常样本 (safe_is_file=False 但客观应存在, 或 4 种查询结果不一致)
    """
    from pathlib import Path
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    root_raw = markers_root
    root = _normalize_windows_path(markers_root)

    w("=" * 60)
    w("pic-clear Marker 诊断报告")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"平台 : {platform.platform()}")
    w("=" * 60)
    w("")
    if root_raw != root:
        w(f"[原始 markers 根] {root_raw}")
        w(f"[规范 markers 根] {root}")
    else:
        w(f"[markers 根] {root}")
    w(f"[根路径长度] {len(root)}")
    w("")

    # 根是否存在
    root_p = Path(root)
    w(f"[根 is_dir 短] {root_p.is_dir()}")
    long_root = _to_long_path(root)
    if long_root != root:
        w(f"[长路径根] {long_root}")
        w(f"[长根 is_dir] {Path(long_root).is_dir()}")
    else:
        w(r"[长路径根] (根路径不足阈值, 不套 \?\)")
    w("")

    # rglob 数量对比: 短 vs 长
    w("--- rglob(_done.marker) 数量对比 ---")
    try:
        short_list = list(root_p.rglob("_done.marker"))
        w(f"短路径 rglob : {len(short_list)} 条")
    except Exception as e:
        w(f"短路径 rglob : ERR {type(e).__name__}: {e}")
        short_list = []
    if long_root != root:
        try:
            long_list = list(Path(long_root).rglob("_done.marker"))
            w(f"长路径 rglob : {len(long_list)} 条")
        except Exception as e:
            w(f"长路径 rglob : ERR {type(e).__name__}: {e}")
            long_list = []
        diff = len(long_list) - len(short_list)
        w(f"差值         : {diff:+d}  ({'长路径多' if diff>0 else '短路径多' if diff<0 else '一致'})")
    else:
        long_list = short_list
    w("")

    if not long_list:
        w("[没扫到 _done.marker, 报告结束]")
        return "\n".join(lines)

    # 用长路径列表做完整逐个诊断 (更全)
    all_markers = long_list

    # 路径长度分布
    lens = [len(str(m)) for m in all_markers]
    w("--- 路径长度分布 ---")
    w(f"总数     : {len(lens)}")
    w(f"最短     : {min(lens)}")
    w(f"最长     : {max(lens)}")
    w(f"平均     : {sum(lens) // len(lens)}")
    w(f">=260    : {sum(1 for L in lens if L >= 260)} (Windows MAX_PATH 边界)")
    w(f">=200    : {sum(1 for L in lens if L >= 200)}")
    w("")

    # 逐个跑 _safe_is_file_impl, 统计各分支命中
    stats = {
        "hit_short": 0,        # 短路径 isfile=True 直接命中
        "hit_long_isfile": 0,  # 长路径 isfile=True 命中
        "hit_long_stat": 0,    # 长路径 stat 兜底命中 (v0.4.65 新加的救命分支)
        "miss": 0,             # 全挂
    }
    miss_samples: list[dict] = []  # 全挂的样本
    quirk_samples: list[dict] = [] # 短挂长救回来的样本 (SMB quirk 证据)

    for m in all_markers:
        hit, diag = _safe_is_file_impl(str(m))
        if hit:
            if diag.get("short_isfile") == "True":
                stats["hit_short"] += 1
            elif diag.get("long_isfile") == "True":
                stats["hit_long_isfile"] += 1
                if len(quirk_samples) < sample_limit:
                    quirk_samples.append({"path": str(m), "diag": diag})
            elif diag.get("long_stat", "").startswith("OK"):
                stats["hit_long_stat"] += 1
                if len(quirk_samples) < sample_limit:
                    quirk_samples.append({"path": str(m), "diag": diag})
        else:
            stats["miss"] += 1
            if len(miss_samples) < sample_limit:
                miss_samples.append({"path": str(m), "diag": diag})

    w("--- safe_is_file 各分支命中统计 ---")
    w(f"[hit_short]        {stats['hit_short']:5d}  (短路径 isfile=True, 正常)")
    w(f"[hit_long_isfile]  {stats['hit_long_isfile']:5d}  (长路径 isfile 救回, 短路径撒谎 = SMB quirk)")
    w(f"[hit_long_stat]    {stats['hit_long_stat']:5d}  (长路径 stat 兜底救回, isfile 也撒谎 = v0.4.65 兜底)")
    w(f"[miss]             {stats['miss']:5d}  (全挂, 视频会被重跑)")
    total = sum(stats.values())
    if total:
        for k in stats:
            w(f"    {k:<18s} {stats[k]/total*100:5.1f}%")
    w("")

    # SMB quirk 样本
    if quirk_samples:
        w(f"--- SMB quirk 样本 (最多 {sample_limit} 条, 短路径撒谎但长路径救回) ---")
        for i, s in enumerate(quirk_samples, 1):
            w(f"[{i}] {s['path']}  len={len(s['path'])}")
            d = s["diag"]
            w(f"    short_isfile= {d.get('short_isfile')}")
            w(f"    long_isfile = {d.get('long_isfile')}")
            w(f"    long_stat   = {d.get('long_stat')}")
        w("")

    # miss 样本 (最可怕的, 重跑的元凶)
    if miss_samples:
        w(f"--- MISS 样本 (最多 {sample_limit} 条, 这些 marker 客观在但判定全 False, 视频会重跑) ---")
        for i, s in enumerate(miss_samples, 1):
            w(f"[{i}] {s['path']}  len={len(s['path'])}")
            d = s["diag"]
            w(f"    short_isfile= {d.get('short_isfile')}")
            w(f"    long_p      = {d.get('long_p')}")
            w(f"    long_isfile = {d.get('long_isfile')}")
            w(f"    long_stat   = {d.get('long_stat')}")
            # 补: 手动再试一把 os.path.exists / os.stat / open('rb') 各种方式
            path_try = d.get("long_p") or s["path"]
            try:
                w(f"    再试 os.path.exists({path_try[:40]}...) = {os.path.exists(path_try)}")
            except Exception as e:
                w(f"    再试 os.path.exists ERR {type(e).__name__}: {e}")
            try:
                with open(path_try, "rb") as f:
                    head = f.read(16)
                w(f"    再试 open(rb) OK head={head!r}")
            except Exception as e:
                w(f"    再试 open(rb) ERR {type(e).__name__}: {e}")
        w("")
    else:
        w("[没有 MISS 样本, safe_is_file 判定 100% 命中, marker skip 应该稳]")

    w("=" * 60)
    w("诊断结束. 请把上面全部内容复制并贴给作者.")
    w("=" * 60)
    return "\n".join(lines)


# ---------------- 单条 Marker 深度诊断 (v0.4.68+) ----------------

def run_single_marker_diagnostics(marker_path: str) -> str:
    r"""对一条 _done.marker 完整路径跑 8 种查询, 定位 IO 层是否撒谎.

    针对场景: extract_frames 打出 [MARKER_MISS] 但用户资源管理器亲眼看到 marker 存在.
    8 种查询:
      1) os.path.isfile 短路径
      2) os.path.isfile 长路径 (\\?\UNC\)
      3) os.stat 短路径
      4) os.stat 长路径
      5) open('rb') 短路径 head 16 字节
      6) open('rb') 长路径 head 16 字节
      7) 父目录 os.listdir 短路径, 打印是否含 _done.marker
      8) 父目录 os.listdir 长路径, 打印是否含 _done.marker
    """
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    raw = marker_path
    p = _normalize_windows_path(marker_path)

    w("=" * 60)
    w("pic-clear 单条 Marker 深度诊断")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"平台 : {platform.platform()}")
    w("=" * 60)
    w("")
    if raw != p:
        w(f"[原始路径] {raw}")
        w(f"[规范路径] {p}")
    else:
        w(f"[输入路径] {p}")
    w(f"[路径长度] {len(p)}")

    long_p = _to_long_path(p)
    if long_p == p:
        w("[长路径] (路径不足阈值 180, 未套 \\?\\)")
    else:
        w(f"[长路径] {long_p}")
        w(f"[长路径长度] {len(long_p)}")
    w("")

    # 拆父目录
    parent_short = os.path.dirname(p)
    parent_long  = os.path.dirname(long_p) if long_p != p else None
    fname        = os.path.basename(p)
    w(f"[父目录 短] {parent_short}   len={len(parent_short)}")
    if parent_long:
        w(f"[父目录 长] {parent_long}   len={len(parent_long)}")
    w(f"[文件名   ] {fname!r}   len={len(fname)}  bytes={fname.encode('utf-8')!r}")
    w("")

    def try_step(label: str, fn):
        w(f"--- {label} ---")
        try:
            result = fn()
            if result is not None:
                w(f"OK  {result}")
            else:
                w("OK")
        except Exception as e:
            w(f"ERR {type(e).__name__}: {e}")
        w("")

    # 1-2) isfile
    try_step("[1] os.path.isfile 短路径",
             lambda: os.path.isfile(p))
    if long_p != p:
        try_step("[2] os.path.isfile 长路径 (\\?\\)",
                 lambda: os.path.isfile(long_p))
    else:
        w("--- [2] os.path.isfile 长路径 ---")
        w("SKIP (无长路径)")
        w("")

    # 3-4) stat
    try_step("[3] os.stat 短路径",
             lambda: f"size={os.stat(p).st_size} mtime={int(os.stat(p).st_mtime)}")
    if long_p != p:
        try_step("[4] os.stat 长路径",
                 lambda: f"size={os.stat(long_p).st_size} mtime={int(os.stat(long_p).st_mtime)}")
    else:
        w("--- [4] os.stat 长路径 ---")
        w("SKIP (无长路径)")
        w("")

    # 5-6) open('rb')
    def _open_rb(path):
        with open(path, "rb") as f:
            head = f.read(16)
        return f"{len(head)} bytes: " + " ".join(f"{b:02x}" for b in head)
    try_step("[5] open('rb') 短路径",
             lambda: _open_rb(p))
    if long_p != p:
        try_step("[6] open('rb') 长路径",
                 lambda: _open_rb(long_p))
    else:
        w("--- [6] open('rb') 长路径 ---")
        w("SKIP (无长路径)")
        w("")

    # 7-8) 父目录 listdir + 是否含目标文件名
    def _list_and_check(dirpath, target):
        try:
            names = os.listdir(dirpath)
        except Exception as e:
            return f"listdir ERR {type(e).__name__}: {e}"
        hit = target in names
        line = f"listdir OK  共 {len(names)} 项   含 {target!r}: {hit}"
        if not hit and names:
            # 名字有差异? 打 hex 帮排查隐形字符
            similar = [n for n in names if n.startswith(target[:10]) or target in n]
            if similar:
                line += f"\n    近似名: {similar}"
                for n in similar[:3]:
                    line += f"\n      {n!r} bytes={n.encode('utf-8')!r}"
        elif not hit:
            line += f"\n    (目录空)"
        return line

    try_step("[7] 父目录 listdir 短路径 (关键: 能否列出 _done.marker)",
             lambda: _list_and_check(parent_short, fname))
    if parent_long:
        try_step("[8] 父目录 listdir 长路径",
                 lambda: _list_and_check(parent_long, fname))
    else:
        w("--- [8] 父目录 listdir 长路径 ---")
        w("SKIP (无长路径)")
        w("")

    # 9) 汇总解读
    w("=" * 60)
    w("解读:")
    w("  - [7]/[8] listdir 里含 _done.marker=True, 但 [1]-[4] isfile/stat 全 False:")
    w("      -> Python/SMB IO 层对**单个 stat** 撒谎, 需要走 listdir 兜底判定")
    w("  - [7]/[8] listdir 也不含 _done.marker:")
    w("      -> marker 真的不在这个目录, 是历史欠账 / 名字写错 / 父目录写错")
    w("  - [7]/[8] listdir 近似名给出 bytes: 名字有隐形字符/BOM/大小写差")
    w("  - [1]-[6] 有任何一条 OK: safe_is_file 里对应分支应能命中, 判定路径应改用这条")
    w("=" * 60)
    return "\n".join(lines)


# ---------------- 诊断主体 ----------------

def run_diagnostics(img_path: str) -> str:
    """跑一遍所有诊断项, 返回一个纯文本报告 (完整可复制)."""
    img_path_raw = img_path
    img_path = _normalize_windows_path(img_path)
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    def try_step(label: str, fn):
        w(f"--- {label} ---")
        try:
            result = fn()
            if result is not None:
                w(f"OK  {result}")
            else:
                w("OK")
        except Exception as e:
            w(f"ERR {type(e).__name__}: {e}")
        w("")

    w("=" * 60)
    w(f"pic-clear 图片打开诊断报告")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"Python 版本 : {sys.version.replace(chr(10), ' ')}")
    w(f"平台 : {platform.platform()}")
    try:
        from PIL import Image
        import PIL
        w(f"Pillow 版本 : {PIL.__version__}")
    except Exception as e:
        w(f"Pillow 版本 : (加载失败) {e}")
        return "\n".join(lines) + "\n[FATAL] 无 Pillow, 诊断中止"
    w("=" * 60)
    w("")

    # ---------- 基础信息 ----------
    if img_path_raw != img_path:
        w(f"[原始路径] {img_path_raw}")
        w(f"[规范路径] {img_path}   (已把 / -> \\ + 去重复 \\?\\)")
    else:
        w(f"[输入路径] {img_path}")
    w(f"[路径长度] {len(img_path)}")
    w("")

    try_step("os.path.exists", lambda: os.path.exists(img_path))
    try_step("os.path.isfile", lambda: os.path.isfile(img_path))
    try_step("os.path.getsize", lambda: os.path.getsize(img_path))
    try_step("os.stat", lambda: str(os.stat(img_path)))

    # ---------- 映射盘展开 ----------
    if os.name == "nt" and len(img_path) >= 2 and img_path[1] == ":":
        drive = img_path[:2]
        rc, unc = _resolve_mapped_drive_to_unc(drive)
        w(f"[WNetGetConnectionW({drive})] rc={rc}  unc={unc!r}")
        if os.name == "nt":
            try:
                out = subprocess.check_output(
                    ["net", "use", drive],
                    stderr=subprocess.STDOUT,
                    encoding="mbcs",
                    errors="replace",
                    timeout=10,
                )
                w(f"[net use {drive}]")
                for ln in out.splitlines():
                    w("  " + ln)
            except Exception as e:
                w(f"[net use {drive}] ERR {type(e).__name__}: {e}")
        w("")
    else:
        unc = None

    # ---------- 6 种打开方式 ----------
    long_p = _long_path_prefix(img_path)
    w(f"[加前缀后] {long_p}")
    w(f"[加前缀长度] {len(long_p)}")
    w("")

    from PIL import Image  # noqa: E402

    # 1) 原路径 Image.open
    try_step("[1] Image.open(原路径)",
             lambda: str(Image.open(img_path).size))

    # 2) \\?\ 前缀 Image.open
    try_step("[2] Image.open(\\\\?\\ 前缀)",
             lambda: str(Image.open(long_p).size))

    # 3) UNC 展开 Image.open
    if unc:
        try_step(f"[3] Image.open(\\\\?\\UNC\\{unc.lstrip(chr(92))})",
                 lambda: str(Image.open(_to_unc_full(img_path, unc)).size))
    else:
        w("--- [3] Image.open(\\\\?\\UNC\\...) ---")
        w("SKIP 无 UNC 展开信息")
        w("")

    # 4) open('rb', 原路径) + BytesIO
    def open_bio_original():
        with open(img_path, "rb") as f:
            data = f.read()
        return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
    try_step("[4] open(原路径,'rb') -> BytesIO -> Image.open",
             open_bio_original)

    # 5) open('rb', \\?\ 前缀) + BytesIO  ← 最有可能是 dedupe_pic 修复方向
    def open_bio_long():
        with open(long_p, "rb") as f:
            data = f.read()
        return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
    try_step("[5] open(\\\\?\\前缀,'rb') -> BytesIO -> Image.open",
             open_bio_long)

    # 6) open('rb', UNC 展开)
    if unc:
        unc_full = _to_unc_full(img_path, unc)
        def open_bio_unc():
            with open(unc_full, "rb") as f:
                data = f.read()
            return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
        try_step("[6] open(\\\\?\\UNC\\...,'rb') -> BytesIO -> Image.open",
                 open_bio_unc)
    else:
        w("--- [6] open(\\\\?\\UNC\\...,'rb') -> BytesIO -> Image.open ---")
        w("SKIP 无 UNC 展开信息")
        w("")

    # ---------- 文件头字节 hex (最后手段: 看看内容是不是被 SMB 拦成 0 字节 / HTML) ----------
    for label, path_try in [("原路径", img_path), ("\\\\?\\ 前缀", long_p)]:
        w(f"--- 文件头 hex ({label}) ---")
        try:
            with open(path_try, "rb") as f:
                head = f.read(32)
            w(f"OK  {len(head)} bytes: " + " ".join(f"{b:02x}" for b in head))
        except Exception as e:
            w(f"ERR {type(e).__name__}: {e}")
        w("")

    w("=" * 60)
    w("诊断结束. 请把上面全部内容复制并贴给作者.")
    w("=" * 60)
    return "\n".join(lines)


# ---------------- GUI ----------------


def run_log_batch_diagnostics(log_path: str) -> str:
    r"""扫抽帧日志, 抓每一条 [MARKER_MISS] 里的 marker 路径, 逐条深度诊断.

    v0.4.70 新增. 输入是切帧工具落地的日志文件 (extract_gui / pipeline 都行);
    输出一份汇总报告, 用户只填一个文件路径, 剩下的自动化.

    抓 marker 路径的正则匹配以下两种:
      1) "marker      = \\..._done.marker"   (extract_frames [MARKER_MISS] 段)
      2) "marker      = \\..._done.marker\n"  (行尾)
    """
    import re

    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w("=" * 60)
    w("pic-clear 日志批量 Marker 诊断  (v0.4.70+)")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"平台 : {platform.platform()}")
    w(f"日志文件 : {log_path}")
    w("=" * 60)
    w("")

    if not os.path.isfile(log_path):
        w(f"[FATAL] 日志文件不存在: {log_path}")
        return "\n".join(lines)

    # 读日志 (可能有 BOM / GBK / UTF-8, 都试一遍)
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            with open(log_path, "r", encoding=enc, errors="strict") as f:
                text = f.read()
            w(f"[编码] 读取成功: {enc}")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        w("[编码] 全部严格模式失败, fallback utf-8 + replace")
    w(f"[日志大小] {len(text)} 字符")
    w("")

    # 抓 marker 路径. 支持 "marker      = XXX" 或 "marker=XXX"
    pat = re.compile(r"marker\s*=\s*(.+?_done\.marker)\s*$", re.MULTILINE)
    hits = pat.findall(text)
    # 去重, 保序
    seen = set()
    markers: list[str] = []
    for m in hits:
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            markers.append(m)

    w(f"[抓到 marker 路径] 共 {len(markers)} 条 (去重后)")
    w("")

    if not markers:
        w("[WARN] 日志里没抓到 marker 路径 (正则匹配 'marker = ..._done.marker')")
        w("       请确认这是 extract_frames [MARKER_MISS] 段的日志.")
        return "\n".join(lines)

    # 逐条跑 6 层查询, 统计各层结果
    counters = {
        "short_isfile_true": 0,
        "long_isfile_true":  0,
        "stat_ok":           0,
        "listdir_hit":       0,
        "find_first_hit":    0,
        "all_miss":          0,
    }
    miss_samples: list[dict] = []
    hit_by_find_only: list[dict] = []  # 只有 FindFirstFileW 救回来的
    hit_by_listdir_only: list[dict] = []  # listdir 救回来的
    parent_lengths: list[int] = []

    for idx, m in enumerate(markers, 1):
        p_norm = _normalize_windows_path(m)
        long_p = _to_long_path(p_norm)

        # 1) short isfile
        try:
            short_isfile = os.path.isfile(p_norm)
        except OSError:
            short_isfile = False
        # 2) long isfile
        long_isfile = False
        if long_p != p_norm:
            try:
                long_isfile = os.path.isfile(long_p)
            except OSError:
                long_isfile = False
        # 3) long stat
        stat_ok = False
        stat_err = ""
        if long_p != p_norm:
            try:
                os.stat(long_p)
                stat_ok = True
            except OSError as e:
                stat_err = f"{type(e).__name__}:{e}"
        # 4) parent listdir
        listdir_hit = False
        parent_count = -1
        listdir_err = ""
        target_p = long_p if long_p != p_norm else p_norm
        parent = os.path.dirname(target_p)
        fname  = os.path.basename(target_p)
        try:
            names = os.listdir(parent)
            parent_count = len(names)
            listdir_hit = fname in names
        except OSError as e:
            listdir_err = f"{type(e).__name__}:{e}"
        # 5) FindFirstFileW
        ff_status, ff_detail = _find_first_file_w(target_p)
        find_hit = (ff_status == "HIT")

        parent_lengths.append(len(parent))

        # 统计
        if short_isfile:
            counters["short_isfile_true"] += 1
        if long_isfile:
            counters["long_isfile_true"] += 1
        if stat_ok:
            counters["stat_ok"] += 1
        if listdir_hit:
            counters["listdir_hit"] += 1
        if find_hit:
            counters["find_first_hit"] += 1

        any_hit = short_isfile or long_isfile or stat_ok or listdir_hit or find_hit
        if not any_hit:
            counters["all_miss"] += 1
            if len(miss_samples) < 5:
                miss_samples.append({
                    "marker": m,
                    "len": len(target_p),
                    "parent_count": parent_count,
                    "listdir_err": listdir_err,
                    "stat_err": stat_err,
                    "find_detail": ff_detail,
                })
        # find 单独救回
        if find_hit and not (short_isfile or long_isfile or stat_ok or listdir_hit):
            if len(hit_by_find_only) < 5:
                hit_by_find_only.append({
                    "marker": m,
                    "len": len(target_p),
                    "find_detail": ff_detail,
                })
        # listdir 单独救回
        if listdir_hit and not (short_isfile or long_isfile or stat_ok):
            if len(hit_by_listdir_only) < 5:
                hit_by_listdir_only.append({
                    "marker": m,
                    "len": len(target_p),
                })

    total = len(markers)
    w("--- 各层 API 判定结果 ---")
    def pct(n):
        return f"{n} ({n * 100.0 / total:.1f}%)" if total else "0"
    w(f"[1] os.path.isfile  短路径  HIT: {pct(counters['short_isfile_true'])}")
    w(f"[2] os.path.isfile  长路径  HIT: {pct(counters['long_isfile_true'])}")
    w(f"[3] os.stat         长路径  OK : {pct(counters['stat_ok'])}")
    w(f"[4] parent listdir          HIT: {pct(counters['listdir_hit'])}")
    w(f"[5] FindFirstFileW  长路径  HIT: {pct(counters['find_first_hit'])}")
    w(f"[X] 全 5 层都 MISS (真找不到): {pct(counters['all_miss'])}")
    w("")

    if parent_lengths:
        w(f"[父目录长度分布] min={min(parent_lengths)} max={max(parent_lengths)} "
          f"avg={sum(parent_lengths)/len(parent_lengths):.0f}")
        w("")

    # 单独救回的样本 = 揭示哪一层是"救命层"
    if hit_by_find_only:
        w(f"--- 只有 FindFirstFileW 救回来的 marker ({len(hit_by_find_only)} 条样本, 共 {counters['find_first_hit']} 条命中) ---")
        w("(说明: Python IO 全撒谎, 只有 Win32 底层 API 认帐 -> 根因是 Python CRT 归一化)")
        for s in hit_by_find_only:
            w(f"  * len={s['len']}  {s['marker']}")
            w(f"    find: {s['find_detail']}")
        w("")

    if hit_by_listdir_only:
        w(f"--- 只有 listdir 救回来的 marker ({len(hit_by_listdir_only)} 条样本) ---")
        w("(说明: 单文件 stat 撒谎, 父目录 listdir 讲真话)")
        for s in hit_by_listdir_only:
            w(f"  * len={s['len']}  {s['marker']}")
        w("")

    if miss_samples:
        w(f"--- 全 5 层都 MISS 的 marker ({len(miss_samples)} 条样本, 共 {counters['all_miss']} 条) ---")
        w("(说明: marker 真的不在这个位置; 可能是 pipeline 写路径 bug / 迁移遗漏)")
        for s in miss_samples:
            w(f"  * len={s['len']}  {s['marker']}")
            w(f"    parent listdir 项数: {s['parent_count']}  listdir_err: {s['listdir_err'] or '-'}")
            w(f"    stat_err : {s['stat_err'] or '-'}")
            w(f"    find     : {s['find_detail']}")
        w("")

    w("=" * 60)
    w("解读:")
    w("  - [1]/[2] short_isfile 或 long_isfile HIT 高 (>80%)")
    w("    -> Python IO 层能查到, 但为什么切帧当时 [MARKER_MISS]?")
    w("       可能是 SMB 缓存瞬时不一致; 短暂 miss 后自动 recover, 不严重")
    w("  - [4] listdir 显著高于 [1]/[2]/[3]")
    w("    -> 单文件 stat 撒谎, listdir 讲真话 (SMB quirk)")
    w("       -> v0.4.69 的 parent_listdir 兜底就是干这个")
    w("  - [5] FindFirstFileW 显著高于 [4]")
    w("    -> 连 listdir 都撒谎, Win32 底层才认账")
    w("       -> v0.4.70 加的 FindFirstFileW 兜底就是干这个")
    w("  - [X] all_miss 很高 (>10%)")
    w("    -> marker 真的不在预期位置, 是历史欠账/写路径 bug, 需要单独排查")
    w("=" * 60)
    w("诊断结束. 请把上面全部内容复制并贴给作者.")
    w("=" * 60)
    return "\n".join(lines)



def collect_env_report() -> str:
    r"""磁盘 + SMB + 长路径注册表 环境体检. v0.4.71 新增.

    上游频繁改挂载方式 (Z: 映射 / \\filestor 直连 UNC / 网络位置 / NFS),
    下游先摸清运行时环境, 再判断 pic-clear 该走哪条路径.
    """
    import subprocess
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w("=" * 60)
    w("pic-clear 环境体检报告  (v0.4.71+)")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"平台     : {platform.platform()}")
    w(f"Python   : {sys.version.split()[0]}")
    w("=" * 60)
    w("")

    if os.name != "nt":
        w("[SKIP] 非 Windows 平台, 环境体检仅在 Windows 有意义.")
        return "\n".join(lines)

    def _run(cmd, timeout=6, shell=True):
        r"""执行命令拿 stdout+stderr, 超时/异常都吞掉, 返回字符串."""
        try:
            r = subprocess.run(
                cmd, shell=shell, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            out = r.stdout.decode("gbk", errors="replace") if r.stdout else ""
            return out.rstrip(), r.returncode
        except subprocess.TimeoutExpired:
            return f"[TIMEOUT after {timeout}s]", -1
        except Exception as e:
            return f"[EXCEPTION {type(e).__name__}: {e}]", -1

    # --------- Section 1: 盘符 & 卷 ----------
    w("--- 1. 逻辑盘符 (wmic logicaldisk) ---")
    out, rc = _run('wmic logicaldisk get DeviceID,DriveType,ProviderName,FileSystem,VolumeName,Size,FreeSpace /format:list')
    if rc != 0 or "[TIMEOUT" in out or "[EXCEPTION" in out:
        # 老 Windows 无 wmic 或超时, fallback PowerShell
        out, rc = _run('powershell -NoProfile -Command "Get-WmiObject Win32_LogicalDisk | Select-Object DeviceID,DriveType,ProviderName,FileSystem,VolumeName,Size,FreeSpace | Format-List"', timeout=10)
    if out:
        # DriveType 释义:
        #   0=Unknown 1=NoRoot 2=Removable 3=LocalDisk 4=NetworkDrive 5=CDROM 6=RAMDisk
        w(out)
        w("")
        w("    (DriveType: 3=本地 4=网络映射盘 5=光驱)")
    else:
        w("(无输出, rc=%d)" % rc)
    w("")

    # --------- Section 2: net use (映射盘/UNC 挂载) ----------
    w("--- 2. net use 输出 (哪些 UNC 被映射) ---")
    out, rc = _run("net use")
    w(out if out else f"(无输出, rc={rc})")
    w("")

    # --------- Section 3: SMB 连接协商结果 ----------
    w("--- 3. SMB 连接细节 (Get-SmbConnection) ---")
    ps_cmd = (
        'powershell -NoProfile -Command '
        '"Get-SmbConnection | Select-Object ServerName,ShareName,'
        'UserName,Credential,Dialect,NumOpens,Redirected | Format-List"'
    )
    out, rc = _run(ps_cmd, timeout=10)
    if out.strip():
        w(out)
        w("")
        w("    (Dialect: 2.1=Win7/Server2008R2  3.0=Win8/2012  3.02=Win8.1/2012R2  3.1.1=Win10+)")
        w("    (Samba 4 通常协商到 3.1.1; SMB1 说明服务端非常老)")
    else:
        w(f"(无输出, rc={rc}, 可能未启用 SMB Client 或无活跃连接)")
    w("")

    # --------- Section 4: 已挂载 UNC (net view / mklink 特殊) ----------
    w("--- 4. WNetGetConnection 逐盘符探测 ---")
    try:
        import string
        from winpath_util import resolve_mapped_drive_to_unc_verbose
        rows = []
        for ch in string.ascii_uppercase:
            drv = ch + ":"
            if not os.path.exists(drv + "\\"):
                continue
            rc2, val = resolve_mapped_drive_to_unc_verbose(drv)
            if rc2 == 0 and val:
                rows.append(f"  {drv}  ->  {val}")
            elif rc2 not in (0, 2250):
                # 2250 = ERROR_NOT_CONNECTED, 本地盘正常; 其他值才打
                rows.append(f"  {drv}  (WNet rc={rc2})")
        if rows:
            w("\n".join(rows))
        else:
            w("(没有映射到 UNC 的盘符, 说明当前不走盘符挂载, 用 UNC 直连)")
    except Exception as e:
        w(f"(探测挂载失败: {type(e).__name__}: {e})")
    w("")

    # --------- Section 5: SMB Client 缓存注册表 ----------
    w("--- 5. SMB Client 目录/文件缓存 (注册表, 只读) ---")
    reg_key = r'HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters'
    interesting = [
        ("FileNotFoundCacheLifetime",
         "文件不存在缓存(秒) 默认 5. 越大越可能读到旧的'不存在', 是 marker miss 罪魁"),
        ("DirectoryCacheLifetime",
         "目录列表缓存(秒) 默认 10. Samba 服务端无 Change Notify 时只能靠这个"),
        ("FileInfoCacheLifetime",
         "文件属性缓存(秒) 默认 10"),
        ("DormantFileLimit",
         "空闲文件上限, 影响长连接稳定性"),
        ("DisableBandwidthThrottling",
         "0=启用节流, 1=禁用; 高延迟共享盘要设 1"),
        ("DisableLargeMtu",
         "0=启用 large MTU (1MB), 1=禁用 (只用 64KB); 挂 SMB2+ 要 0"),
    ]
    for name, desc in interesting:
        cmd = f'reg query "{reg_key}" /v {name}'
        out, rc = _run(cmd, timeout=4)
        if rc == 0 and "REG_" in out:
            # 提取 REG_DWORD 0x?? 那一行
            for line in out.splitlines():
                s = line.strip()
                if name in s and "REG_" in s:
                    w(f"  {name:35s}  {s.split(name,1)[1].strip()}")
                    w(f"    {desc}")
                    break
            else:
                w(f"  {name:35s}  (未设置, 用默认值)")
                w(f"    {desc}")
        else:
            w(f"  {name:35s}  (未设置, 用默认值)")
            w(f"    {desc}")
    w("")

    # --------- Section 6: 长路径支持开关 ----------
    w("--- 6. Windows 长路径支持 (LongPathsEnabled) ---")
    cmd = r'reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled'
    out, rc = _run(cmd, timeout=4)
    if rc == 0 and "LongPathsEnabled" in out:
        for line in out.splitlines():
            if "LongPathsEnabled" in line:
                w(f"  {line.strip()}")
                if "0x1" in line:
                    w("    -> 已启用, 部分 Win32 API 可直接吃 >260 路径 (无需 \\\\?\\)")
                else:
                    w("    -> 未启用, >260 路径必须靠 \\\\?\\ 前缀绕开")
                break
    else:
        w("  (键不存在, 大多数 Windows Server 默认这样, 必须靠 \\\\?\\)")
    w("")

    # --------- Section 7: 关键路径可达性快速探测 ----------
    w("--- 7. 常用 pic-clear 根路径快速探测 ---")
    # 用户可能在意的几个根路径 (从环境变量 / 猜测)
    guesses = [
        r"\\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100",
        r"\\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100\节点",
        r"\\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100\切帧结果new",
        r"Z:\\",
        r"D:\\qzgj",
    ]
    for p in guesses:
        exists_short = "?"
        try:
            exists_short = "True" if os.path.exists(p) else "False"
        except Exception as e:
            exists_short = f"ERR:{type(e).__name__}"
        w(f"  {p}")
        w(f"    exists(短)={exists_short}")
        if exists_short == "True":
            # 长路径版
            try:
                from winpath_util import to_long_path
                lp = to_long_path(p)
                if lp != p:
                    try:
                        lex = "True" if os.path.exists(lp) else "False"
                    except Exception as e:
                        lex = f"ERR:{type(e).__name__}"
                    w(f"    exists(长)={lex}  ({lp})")
            except Exception:
                pass
            # isdir + listdir 前 3
            try:
                names = os.listdir(p)
                head = names[:3]
                w(f"    listdir OK  共 {len(names)} 项, 前 3: {head}")
            except Exception as e:
                w(f"    listdir ERR {type(e).__name__}: {e}")
    w("")

    # --------- Section 8: Explorer 挂载 (网络位置 Web Folder) ----------
    w("--- 8. 网络位置 / Web Folder 挂载 ---")
    # 用户截图显示的是"网络位置"(不是映射盘, 走 explorer.exe 层的 shortcut)
    # 这些通常存在 %APPDATA%\Microsoft\Windows\Network Shortcuts\
    ns = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Network Shortcuts")
    if os.path.isdir(ns):
        try:
            items = os.listdir(ns)
            w(f"  Network Shortcuts 目录: {ns}")
            w(f"  共 {len(items)} 项:")
            for it in items[:10]:
                w(f"    - {it}")
        except Exception as e:
            w(f"  (读取失败: {type(e).__name__}: {e})")
    else:
        w(f"  (无 Network Shortcuts 目录: {ns})")
    w("")
    w("  说明: 'Network Shortcuts' 里的 '网络位置' 只是 Explorer UI 层的快捷方式,")
    w("        不是真正的挂载. 打开时 Windows 直接用 UNC 路径访问, 走 SMB Redirector.")
    w("        对 Python IO 来说, 跟直接用 \\\\filestor... 完全一样.")
    w("")

    # --------- Section 9: 环境结论 ----------
    w("=" * 60)
    w("[环境画像小结]")
    w("=" * 60)
    w("如果第 2 节 net use 里没有你项目路径, 说明**没走映射盘**, Python 拿到的")
    w("永远是 \\\\filestor01...\\ UNC 原始路径 -> _to_long_path 会展开成 \\\\?\\UNC\\")
    w("")
    w("如果第 3 节 Get-SmbConnection 服务端识别成 Samba (或未列出), 说明:")
    w("  a) SMB 客户端拿不到 Change Notify")
    w("  b) 只能靠 DirectoryCacheLifetime (默认 10 秒) 时间过期后重新查")
    w("  c) 抽帧脚本刚写完 marker, 隔壁 clip 的判定 listdir 大概率读到 10 秒前的**空目录缓存**")
    w("     -> [MARKER_MISS] parent 有 0 项 = SMB 缓存, 不是 marker 真丢")
    w("")
    w("如果第 5 节 DirectoryCacheLifetime 未设置或很大 (>60), 是缓存罪魁之一, 但堡垒机不给改.")
    w("")
    w("如果第 6 节 LongPathsEnabled=0 (常见), 说明必须靠 \\\\?\\ 前缀绕开 MAX_PATH.")
    w("")
    w("如果第 7 节 UNC 短路径 exists=False 但长路径 exists=True, 说明路径 >=260 字符,")
    w("Python CRT 不吃, 必须走 \\\\?\\ 长路径 API 系列 (extract_frames 已经这样干).")
    w("=" * 60)
    return "\n".join(lines)


# 该文件即将被拼进 diag_pic.py, 位置: collect_env_report() 之后, ffprobe 诊断之前
# 独立函数 + 探针实现. 依赖 winpath_util / dedupe_gui._find_dedupe_targets (若可导入).

# ---------------- 深路径去重诊断 (v0.4.110 新增) ----------------

def _classify_path(p: str) -> str:
    r"""一眼看清用户选的路径是什么类型."""
    if os.name != "nt":
        return "non-Windows"
    s = str(p)
    if s.startswith("//?/") or s.startswith("\\\\?\\"):
        if "UNC" in s[:8].upper():
            return "长路径 UNC (\\\\?\\UNC\\...)"
        return "长路径 (\\\\?\\X:\\...)"
    if s.startswith("//") or s.startswith("\\\\"):
        return "UNC 直连 (\\\\server\\share\\...)"
    if len(s) >= 2 and s[1] == ":":
        # 简单猜: 大写 A-Z + 盘符
        return f"盘符路径 ({s[:2]})  —— 可能本地 / 也可能映射盘"
    if s.startswith("\\") or s.startswith("/"):
        return "单斜杠开头 —— 危险! 畸形 UNC 或'当前盘根'路径"
    return "其他 / 相对路径"


def _mangle_unc_single_slash(long_p: str) -> str:
    r"""复现 dedupe_gui._find_dedupe_targets 那处 bug 会产出的畸形 UNC.

    - 输入: \\?\UNC\filestor01...\share\...\camera07
    - 输出: \filestor01...\share\...\camera07   (只剥一根 \, 丢了一根)
    真实 bug 就是那一行 "\\" + normal_dirpath[len("\\\\?\\UNC\\"):]
    """
    prefix = "\\\\?\\UNC\\"
    if long_p.startswith(prefix):
        return "\\" + long_p[len(prefix):]
    return long_p


def _mangle_tkinter_style(p: str) -> str:
    r"""tkinter filedialog 在长路径下返回 //?/UNC/filestor.../... 混斜杠."""
    if os.name != "nt":
        return p
    s = p.replace("\\", "/")
    if s.startswith("//") and not s.startswith("//?/"):
        # UNC 直连转 tkinter 长路径混斜杠
        return "//?/UNC/" + s.lstrip("/")
    return s


def _probe_one_variant(label: str, p: str) -> list[str]:
    r"""对同一个字符串路径跑 exists / isfile / stat / open('rb')[:16] 四发, 原样返回结果."""
    out: list[str] = []
    out.append(f"  ── 变体: {label}")
    out.append(f"     path = {p!r}")
    out.append(f"     len  = {len(p)}")
    # os.path.exists
    try:
        r = os.path.exists(p)
        out.append(f"     os.path.exists      = {r}")
    except OSError as e:
        out.append(f"     os.path.exists      ERR {type(e).__name__}: {e}")
    # os.path.isfile
    try:
        r = os.path.isfile(p)
        out.append(f"     os.path.isfile      = {r}")
    except OSError as e:
        out.append(f"     os.path.isfile      ERR {type(e).__name__}: {e}")
    # os.stat
    try:
        st = os.stat(p)
        out.append(f"     os.stat             OK size={st.st_size}")
    except OSError as e:
        out.append(f"     os.stat             ERR "
                   f"[WinError {getattr(e, 'winerror', '?')}] "
                   f"{type(e).__name__}: {e}")
    # open('rb') 读 16 字节
    try:
        with open(p, "rb") as f:
            head = f.read(16)
        out.append(f"     open('rb').read(16) OK bytes={head[:8].hex()}...")
    except OSError as e:
        out.append(f"     open('rb')          ERR "
                   f"[WinError {getattr(e, 'winerror', '?')}] "
                   f"{type(e).__name__}: {e}")
    # Win32 FindFirstFileW 直调 (winpath_util 里的 _find_first_file_w)
    try:
        status, detail = _find_first_file_w(p)
        out.append(f"     FindFirstFileW      {status} {detail}")
    except Exception as e:
        out.append(f"     FindFirstFileW      EXC {type(e).__name__}: {e}")
    return out


def _find_deepest_and_longest(root_str: str,
                              max_walk_seconds: float = 30.0
                              ) -> tuple[str | None, str | None, dict]:
    r"""os.walk 递归找 (最深图片路径, 最长字符串图片路径).

    - 只认 .jpg/.jpeg/.png/.bmp/.webp
    - walk_root 走 to_long_path, 兼容深路径
    - 结果里的路径**剥回**原始形式 (双反斜杠 UNC 或 X:\\), 跟 dedupe 拿到的一致
    - max_walk_seconds 保护: 大目录别把 GUI 卡死
    """
    import time as _t
    stats = {
        "walked_dirs": 0,
        "total_images": 0,
        "max_depth": -1,
        "max_len": -1,
        "timeout": False,
        "walk_root": "",
    }
    exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    walk_root = _to_long_path(root_str)
    stats["walk_root"] = walk_root
    deepest_p: str | None = None
    longest_p: str | None = None
    t0 = _t.time()

    def _strip_prefix(dp: str) -> str:
        if dp.startswith("\\\\?\\UNC\\"):
            return "\\\\" + dp[len("\\\\?\\UNC\\"):]  # 正确剥法: 恢复两根 \
        if dp.startswith("\\\\?\\"):
            return dp[len("\\\\?\\"):]
        return dp

    try:
        for dirpath, _dirnames, filenames in os.walk(walk_root):
            stats["walked_dirs"] += 1
            if _t.time() - t0 > max_walk_seconds:
                stats["timeout"] = True
                break
            normal_dirpath = _strip_prefix(dirpath)
            # depth: 从 root_str 起算
            try:
                rel = os.path.relpath(normal_dirpath, root_str)
                depth = 0 if rel in (".", "") else rel.count(os.sep) + 1
            except Exception:
                depth = normal_dirpath.count(os.sep)
            for name in filenames:
                lower = name.lower()
                if not any(lower.endswith(e) for e in exts):
                    continue
                full = os.path.join(normal_dirpath, name)
                stats["total_images"] += 1
                if depth > stats["max_depth"]:
                    stats["max_depth"] = depth
                    deepest_p = full
                if len(full) > stats["max_len"]:
                    stats["max_len"] = len(full)
                    longest_p = full
    except Exception as e:
        stats["walk_exc"] = f"{type(e).__name__}: {e}"
    stats["elapsed_sec"] = round(_t.time() - t0, 2)
    return deepest_p, longest_p, stats


def _try_import_find_dedupe_targets():
    r"""尽量复用 dedupe_gui._find_dedupe_targets 原函数, 保证跟 GUI 行为完全一致.

    如果 diag_pic.exe 是独立打包的 (没跟 dedupe_gui 打一起), 就 fallback 到
    我们在这里内联的 verbatim 实现.
    """
    try:
        from dedupe_gui import _find_dedupe_targets as _fn
        return _fn, "dedupe_gui._find_dedupe_targets"
    except Exception:
        pass
    # verbatim 版本, 跟 dedupe_gui.py 那份**逐字**复制, 只把 _to_long_path 换成本模块
    def _fn(root, mode: str, logger=None):
        from pathlib import Path as _Path
        def _log(msg):
            if logger:
                try: logger(msg)
                except Exception: pass
        root = _Path(root)
        if not root.is_dir():
            _log(f"● 扫描: target 不是目录: {root}")
            return []
        if mode == "single":
            return [root]
        if mode == "subdirs":
            return sorted([p for p in root.iterdir() if p.is_dir()])
        exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
        out = set()
        walk_root = _to_long_path(str(root))
        for dirpath, _dirnames, filenames in os.walk(walk_root):
            normal_dirpath = dirpath
            if normal_dirpath.startswith("\\\\?\\UNC\\"):
                normal_dirpath = "\\" + normal_dirpath[len("\\\\?\\UNC\\"):]
            elif normal_dirpath.startswith("\\\\?\\"):
                normal_dirpath = normal_dirpath[len("\\\\?\\"):]
            has = False
            for name in filenames:
                l = name.lower()
                for ext in exts:
                    if l.endswith(ext):
                        has = True
                        break
                if has:
                    break
            if has:
                out.add(normal_dirpath)
        return sorted(_Path(s) for s in out)
    return _fn, "diag_pic 内联 verbatim 版 (dedupe_gui 不可导入)"


def _scan_processes() -> list[str]:
    r"""tasklist 过 sqDedupe / dedupe_pic / pipeline / sqDedupeGui."""
    if os.name != "nt":
        return ["  [SKIP] 非 Windows"]
    try:
        r = subprocess.run(
            ["tasklist", "/v", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
            encoding="mbcs", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        return [f"  [ERR] tasklist 调用失败: {type(e).__name__}: {e}"]
    if r.returncode != 0:
        return [f"  [ERR] tasklist rc={r.returncode} stderr={r.stderr!r}"]
    keys = ("sqdedupe", "dedupe_pic", "pipeline.exe", "sqdedupegui",
            "extract_frames", "sqextract")
    hits: list[str] = []
    for line in r.stdout.splitlines():
        lower = line.lower()
        if any(k in lower for k in keys):
            hits.append("  " + line)
    if not hits:
        return ["  (未发现残留 sqDedupe/dedupe_pic/pipeline/sqDedupeGui/extract 进程)"]
    hits.insert(0, "  格式: 名称, PID, 会话名, 会话#, 内存, 状态, 用户, CPU时间, 窗口标题")
    return hits


def _try_dhash(image_path: str) -> str:
    r"""跟 dedupe_pic.dhash 完全一致的逻辑, 但用 winpath_util._pil_open (跟生产完全一致)."""
    try:
        from PIL import Image
        from winpath_util import pil_open as _pil_open
    except Exception as e:
        return f"[SKIP] 导入 PIL/winpath_util 失败: {type(e).__name__}: {e}"
    hash_size = 8
    try:
        with _pil_open(image_path) as img:
            img = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.LANCZOS)
            pixels = list(img.tobytes())
    except Exception as e:
        return f"[FAIL] _pil_open/dhash 抛异常: {type(e).__name__}: {e}"
    bits = 0
    width = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            L = pixels[row * width + col]
            R = pixels[row * width + col + 1]
            bits = (bits << 1) | (1 if L > R else 0)
    is_zero = "  ⚠ 全 0 (读到黑图/空图, 会跟其他 0 哈希图强行聚成 1 组)" if bits == 0 else ""
    is_ffff = "  ⚠ 全 1 (罕见, 通常也是异常)" if bits == 0xFFFFFFFFFFFFFFFF else ""
    return f"dhash = 0x{bits:016x}{is_zero}{is_ffff}"


def run_deepdir_diagnostics(target_dir: str) -> str:
    r"""一键跑完深路径去重诊断. 用户只点一次目录, 剩下 diag_pic 自己搞."""
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w("=" * 68)
    w("深路径去重诊断报告  (v0.4.110+)")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"平台     : {platform.platform()}")
    w(f"Python   : {sys.version.split()[0]}")
    w("=" * 68)
    w("")

    # ---------- §1 路径类型识别 ----------
    w("--- §1 用户选的目录本身 ---")
    w(f"  原始字符串 : {target_dir!r}")
    w(f"  字符串长度 : {len(target_dir)}")
    w(f"  类型判定   : {_classify_path(target_dir)}")
    normed = _normalize_windows_path(target_dir)
    w(f"  归一化后   : {normed!r}")
    long_p = _to_long_path(target_dir)
    w(f"  长路径版   : {long_p!r}")
    w("")

    # ---------- §2 找最深 / 最长的一张图 ----------
    w("--- §2 递归扫图 (寻找最深叶子 / 最长字符串) ---")
    w(f"  os.walk 起点 (长路径): {_to_long_path(target_dir)!r}")
    deepest, longest, stats = _find_deepest_and_longest(target_dir)
    w(f"  扫描目录数 : {stats.get('walked_dirs')}")
    w(f"  扫描图片数 : {stats.get('total_images')}")
    w(f"  耗时       : {stats.get('elapsed_sec')} 秒"
      + ("   (⚠ 达到 30s 上限, 已提前中止)" if stats.get('timeout') else ""))
    if "walk_exc" in stats:
        w(f"  ⚠ os.walk 抛异常: {stats['walk_exc']}")
    if stats.get('total_images', 0) == 0:
        w("  ⚠ 扫不到任何图片! 可能路径本身就有问题, 或子目录 os.walk 穿不过去.")
        w("")
        w("  建议: 用文件资源管理器进选目录, 确认里面确实有 jpg. 若资源管理器")
        w("        能看到而 diag_pic 扫不到, 就是 Python os.walk 层的坑.")
        w("=" * 68)
        return "\n".join(lines)
    w(f"  最深叶子   : depth={stats.get('max_depth')}")
    w(f"    路径     : {deepest!r}")
    w(f"    长度     : {len(deepest) if deepest else 0}")
    w(f"  最长字符串 : len={stats.get('max_len')}")
    w(f"    路径     : {longest!r}")
    w("")

    # ---------- §3 对最深图跑 5 种变体探测 ----------
    for probe_name, probe_path in (("最深图", deepest), ("最长图", longest)):
        if probe_path is None:
            continue
        if probe_name == "最长图" and probe_path == deepest:
            w(f"--- §3.{probe_name} 与最深图相同, 跳过重复探测 ---")
            w("")
            continue
        w(f"--- §3 {probe_name}路径 5 种变体探测 ---")
        w(f"  原始: {probe_path!r}")
        w("")
        variants: list[tuple[str, str]] = []
        variants.append(("原始形式 (dedupe 从 os.walk 剥回的样子)", probe_path))
        v_norm = _normalize_windows_path(probe_path)
        variants.append(("归一化 (_normalize_windows_path)", v_norm))
        v_long = _to_long_path(probe_path)
        variants.append(("长路径 (_to_long_path)", v_long))
        v_mang = _mangle_unc_single_slash(v_long)
        if v_mang != v_long:
            variants.append(
                ("⚠ 畸形单斜杠 UNC (复现 dedupe_gui bug)", v_mang))
        else:
            variants.append(
                ("(非 UNC 长路径, 无法造出畸形单斜杠版, 跳过)", ""))
        v_tk = _mangle_tkinter_style(probe_path)
        if v_tk != probe_path:
            variants.append(("tkinter 混斜杠 (//?/UNC/... 复现)", v_tk))
        for label, p in variants:
            if p == "":
                w(f"  ── 变体: {label}  [跳过]")
                continue
            for line in _probe_one_variant(label, p):
                w(line)
            w("")
        w("")

    # ---------- §4 dhash 对最深图 ----------
    if deepest:
        w("--- §4 对最深图跑 dhash (跟 dedupe_pic 完全一致的逻辑) ---")
        w(f"  target = {deepest!r}")
        w(f"  {_try_dhash(deepest)}")
        w("")

    # ---------- §5 复现 dedupe_gui._find_dedupe_targets(recursive) ----------
    w("--- §5 复现 dedupe_gui._find_dedupe_targets(recursive) ---")
    w("  (这就是 sqDedupeGui.exe 递归模式下真正传给 sqDedupe.exe 的目录列表)")
    w("")
    try:
        fn, src = _try_import_find_dedupe_targets()
        w(f"  函数来源 : {src}")
        import time as _t
        t0 = _t.time()
        from pathlib import Path as _Path_dd
        result = fn(_Path_dd(target_dir), "recursive", logger=None)
        w(f"  返回目录数 : {len(result)}")
        w(f"  耗时       : {round(_t.time() - t0, 2)} 秒")
        show = list(result)[:5]
        for i, d in enumerate(show, 1):
            ds = str(d)
            w(f"  [{i}] {ds!r}")
            w(f"      长度={len(ds)}  开头={ds[:6]!r}")
            # 关键: 这条字符串到底是"两根反斜杠开头"的合法 UNC, 还是被剥剩一根的畸形?
            if os.name == "nt":
                if ds.startswith("\\\\") and not ds.startswith("\\\\?\\"):
                    w("      ✔ 合法 UNC (双反斜杠开头)")
                elif ds.startswith("\\") and not ds.startswith("\\\\"):
                    w("      ⚠⚠⚠ 畸形 UNC! 单反斜杠开头, 生产上会挂 ⚠⚠⚠")
                elif ds.startswith("\\\\?\\"):
                    w("      • 带 \\\\?\\ 前缀 (少见, 通常剥完不该带)")
                elif len(ds) >= 2 and ds[1] == ":":
                    w("      • 盘符路径 (本地/映射)")
        if len(result) > 5:
            w(f"  (仅显示前 5 条, 共 {len(result)} 条)")
    except Exception as e:
        w(f"  ⚠ 调用失败: {type(e).__name__}: {e}")
        w("  详情:")
        for l in traceback.format_exc().splitlines():
            w("    " + l)
    w("")

    # ---------- §6 残留进程扫描 ----------
    w("--- §6 tasklist 扫残留进程 ---")
    w("  (用来对上你 GUI 里报'删除 0 张'但目录里数量在减少的情况:")
    w("   如果这里能看到多个 sqDedupe/dedupe_pic 进程, 就是上一轮的残留仍在删)")
    for line in _scan_processes():
        w(line)
    w("")

    # ---------- 小结 ----------
    w("=" * 68)
    w("[小结判读参考]")
    w("=" * 68)
    w("- §3 最深图变体探测里, 如果'原始形式' os.stat/isfile 直接 ERR,")
    w("  但'长路径 (_to_long_path)' OK -> 说明 dedupe_pic 内部只要走 _safe_* 就能过")
    w("- 如果'畸形单斜杠 UNC' 变体的 os.stat 居然能过 -> 是当前盘根意外命中, 危险!")
    w("- §5 返回的目录列表里若有'⚠⚠⚠ 畸形 UNC', 就是本次问题的直接病灶")
    w("- §6 如果有残留 sqDedupe 进程 (启动很久还没退), 用户看到的'数量减少'就是它")
    w("- §4 dhash 全 0 -> 图读进来是黑图/空图, 200 张全 0 就会强行聚成 1 组")
    return "\n".join(lines)



# ---------------- 深路径去重决策复现 (v0.4.111 新增) ----------------

def run_dedup_replay(target_dir: str,
                     threshold: int = 3,
                     motion_threshold: float = 0.12,
                     protect_arg: str = "person,bicycle,car,motorcycle,bus,train,truck",
                     conf: float = 0.35,
                     enable_scene: bool = False,
                     max_files: int = 500) -> str:
    r"""在选定目录上跑一遍 dedupe_pic 完整决策链路 (build_index + motion + cluster + decide),
    但不真删, 只打印每一步的数字, 用来定位'深路径下判定为 0 张删'的根因.

    要求 diag_pic 同目录能找到 yolov8n.onnx (跟 sqDedupe.exe 一起放的模型).
    找不到就退化到"跳过 YOLO"模式, 只跑 dhash + cluster.
    """
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w("=" * 68)
    w("深路径去重决策复现  (v0.4.111+)")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"目标目录 : {target_dir!r}")
    w(f"参数     : threshold={threshold}  motion={motion_threshold}  "
      f"conf={conf}  scene={enable_scene}  max_files={max_files}")
    w("=" * 68)
    w("")

    # ---- 导入 dedupe_pic / detector 里的实际函数, 保证跟生产完全一致 ----
    try:
        import dedupe_pic
        from dedupe_pic import (
            build_index, mark_motion_changes, cluster,
            decide_actions, write_report, count_files,
        )
    except Exception as e:
        w(f"[FATAL] 导入 dedupe_pic 失败: {type(e).__name__}: {e}")
        for l in traceback.format_exc().splitlines():
            w("  " + l)
        return "\n".join(lines)

    try:
        import detector as _detector_mod
    except Exception as e:
        w(f"[FATAL] 导入 detector 失败: {type(e).__name__}: {e}")
        return "\n".join(lines)

    from pathlib import Path as _Path

    # ---- 找模型 ----
    w("--- §1 定位 YOLO 模型 ---")
    model_path = _detector_mod.resolve_model_path(None)
    if model_path is None:
        w("  ⚠ 未找到 yolov8n.onnx (查过: --model / exe同目录 / _MEIPASS / cwd)")
        w("  → 本次不跑 YOLO, 只跑 dhash + cluster (motion 保护会全部失效)")
        detector = None
    else:
        w(f"  ✓ 模型: {model_path}")
        try:
            detector = _detector_mod.YoloDetector(
                model_path=model_path, conf_thres=conf)
            w("  ✓ 模型加载成功")
        except Exception as e:
            w(f"  ⚠ 模型加载失败: {type(e).__name__}: {e}")
            detector = None
    w("")

    protect_classes = set(x.strip() for x in protect_arg.split(",") if x.strip())
    w(f"  保护类别 : {sorted(protect_classes)}")
    w("")

    # ---- 预扫总数, 跟 dedupe_pic 生产一致 ----
    w("--- §2 预扫 / build_index ---")
    exts = {"jpg", "jpeg", "png", "bmp", "webp"}
    root = _Path(target_dir)
    try:
        total = count_files(root, exts)
    except Exception as e:
        w(f"  ⚠ count_files 挂: {type(e).__name__}: {e}")
        total = 0
    w(f"  预扫总数 : {total}")

    if total > max_files:
        w(f"  ⚠ 超过 max_files={max_files}, 会截断到前 {max_files} 张")
        w(f"    (调大 max_files 参数可以跑完整目录, 大目录会慢)")
    w("")

    # ---- 手动跑 build_index 的核心, 但每张图都记录判定明细 ----
    w("--- §3 逐张扫描明细 (最多前 20 条 + 汇总) ---")
    import time as _t
    t0 = _t.time()
    from dedupe_pic import iter_files, dhash, Item
    from winpath_util import safe_stat as _s_stat

    items = []
    stat_fail = 0
    hash_fail = 0
    is_prot_count = 0
    has_vehicle_count = 0
    per_row_shown = 0
    scanned = 0

    for p in iter_files(root, exts):
        if scanned >= max_files:
            break
        scanned += 1
        row_bits = [f"  [{scanned:04d}]", f"len={len(str(p))}"]
        try:
            st = _s_stat(p)
        except OSError as e:
            stat_fail += 1
            if per_row_shown < 20:
                per_row_shown += 1
                row_bits.append(f"STAT_FAIL {type(e).__name__}: {e}")
                w("  ".join(row_bits))
            continue
        h = dhash(p)
        if h is None:
            hash_fail += 1
            if per_row_shown < 20:
                per_row_shown += 1
                row_bits.append("DHASH_FAIL (_pil_open 返回 None)")
                w("  ".join(row_bits))
            continue
        row_bits.append(f"dhash=0x{h:016x}")

        it = Item(p, st.st_size, st.st_mtime, h)
        if detector is not None:
            try:
                protected, hits, vehicles, size = detector.detect_full(p, protect_classes)
            except Exception as e:
                protected, hits, vehicles, size = False, [], [], None
                row_bits.append(f"DETECT_EXC {type(e).__name__}")
            has_person = any(d.class_name == "person" for d in hits)
            classes_hit = sorted({d.class_name for d in hits})
            if has_person:
                it.is_protected = True
                is_prot_count += 1
                row_bits.append(f"PROTECT(person) classes={classes_hit}")
            elif hits:
                row_bits.append(f"车类命中 classes={classes_hit}")
            else:
                row_bits.append("无 YOLO 命中")
            it.detected_classes = tuple(classes_hit)
            it.vehicle_boxes = tuple(d.box_xyxy for d in vehicles)
            it.image_size = size
            if vehicles:
                has_vehicle_count += 1
            row_bits.append(f"veh={len(vehicles)}")
            row_bits.append(f"size={size}")
        else:
            row_bits.append("(未跑 YOLO)")

        items.append(it)
        if per_row_shown < 20:
            per_row_shown += 1
            w("  ".join(row_bits))

    scan_elapsed = _t.time() - t0
    w("")
    w(f"  扫描完成 : 有效 {len(items)} 张, stat_fail={stat_fail}, hash_fail={hash_fail}")
    w(f"  YOLO 汇总 : is_protected(person)={is_prot_count}, "
      f"有车辆帧数={has_vehicle_count}")
    w(f"  耗时      : {scan_elapsed:.1f} 秒")
    w("")
    if len(items) < 2:
        w("[STOP] 有效图片 < 2, 无法进行相邻帧 / 聚类判定")
        return "\n".join(lines)

    # ---- motion 阶段 ----
    w("--- §4 相邻帧 motion 判定 ---")
    if detector is None:
        w("  ⚠ 未加载 YOLO, 跳过 motion 保护 (生产上此时也是 0 张 motion_protected)")
        motion_marked = 0
    else:
        # 先复制 items 的 motion_protected 状态用于对比
        motion_marked = mark_motion_changes(
            items, motion_threshold=motion_threshold)
    w(f"  motion_protected 标记数 : {motion_marked}")
    w("")

    # ---- cluster + decide ----
    w("--- §5 聚类 + 决策 ---")
    t0 = _t.time()
    groups = cluster(items, threshold)
    w(f"  聚类完成 : {len(groups)} 组近似重复, 耗时 {(_t.time()-t0):.2f}s")
    w("")

    total_delete = 0
    total_keep = 0
    total_protected_group = 0  # 组内全部被保护导致 0 张 DELETE
    for gi, grp in enumerate(groups, 1):
        actions = decide_actions(grp, "shortest-path")
        deletes = sum(1 for x in grp if actions.get(id(x), "KEEP") == "DELETE")
        keeps = len(grp) - deletes
        total_delete += deletes
        total_keep += keeps
        # 组内全 protected 的组是"0 张 DELETE"的直接来源, 单独统计
        n_prot = sum(1 for x in grp
                     if x.is_protected or x.motion_protected or x.scene_protected)
        n_unprot = len(grp) - n_prot
        if deletes == 0:
            total_protected_group += 1

        if gi <= 15:
            w(f"  组#{gi:02d}  大小={len(grp)}  KEEP={keeps}  DELETE={deletes}  "
              f"protected(硬+motion+scene)={n_prot}  unprotected={n_unprot}")
            for x in grp[:5]:
                marks = []
                if x.is_protected: marks.append("硬")
                if x.motion_protected: marks.append(f"motion({x.motion_reason})")
                if x.scene_protected: marks.append("scene")
                a = actions.get(id(x), "KEEP")
                w(f"    - [{a}] {x.path.name}  保护={','.join(marks) or '无'}")
            if len(grp) > 5:
                w(f"    ... 共 {len(grp)} 张, 仅显示前 5 张")

    if len(groups) > 15:
        w(f"  (仅显示前 15 组, 共 {len(groups)} 组)")
    w("")

    # ---- 最终结论 ----
    w("=" * 68)
    w("[最终结论]  ⚠ 请把整段贴给作者")
    w("=" * 68)
    w(f"  总扫描      : {scanned} 张  (预扫总数 {total})")
    w(f"  有效图      : {len(items)} 张  (读取失败 stat={stat_fail}, hash={hash_fail})")
    w(f"  is_protected(person 硬保护) : {is_prot_count} 张")
    w(f"  motion_protected 标记       : {motion_marked} 张")
    w(f"  相似组数    : {len(groups)}")
    w(f"  【会删除】  : {total_delete} 张  (dry-run, 未真删)")
    w(f"  【保留】    : {total_keep} 张")
    w(f"  组内全 protected 导致 DELETE=0 的组数 : {total_protected_group}")
    w("")

    # 特征提示
    w("[判读提示]")
    if len(items) == 0:
        w("  → 有效图 0, 说明连图都没读进来, 检查 stat_fail / hash_fail")
    elif is_prot_count == len(items):
        w("  → 所有图都被 YOLO 判为含 person -> 全硬保护 -> 0 张删除.")
        w("    问题可能是 YOLO 在深路径下'胡乱识别' (BytesIO 兜底喂进去后 letterbox 值不同?)")
    elif motion_marked >= len(items) * 0.9:
        w("  → motion_protected 覆盖 >90%, 相邻帧几乎全部被判'有变化' -> 保留.")
        w("    问题可能是 vehicle_boxes 在深路径下坐标偏移, 相邻帧 IoU 匹配不上")
    elif total_delete == 0 and len(groups) > 0:
        w("  → 有聚类组但一张都没删 -> 组内全部触发某种保护.")
        w("    上面各组明细里看每张的'保护='字段是啥, 就是根因.")
    elif total_delete > 0:
        w(f"  → 会删 {total_delete} 张. 如果生产日志显示 0, 那两次运行差异不在决策层,")
        w("    可能是 --apply / --force 参数没传, 或 marker 已存在整个目录被跳过.")
    return "\n".join(lines)


# ---------------- 视频时长 (ffprobe) 诊断 (v0.4.96 新增) ----------------

def _run_ffprobe_once(ffprobe: str, arg: str, timeout: float = 15.0) -> dict:
    """跑一次 ffprobe, 返回 {rc, stdout, stderr, exc}. 不抛异常."""
    cmd = [ffprobe, "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1",
           arg]
    out = {"cmd": cmd, "rc": None, "stdout": "", "stderr": "", "exc": ""}
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        out["rc"] = r.returncode
        out["stdout"] = (r.stdout or "").strip()
        out["stderr"] = (r.stderr or "").strip()
    except Exception as e:
        out["exc"] = f"{type(e).__name__}: {e}"
    return out


def _resolve_ffprobe_for_diag(user_hint: str) -> tuple[str, str]:
    r"""定位 ffprobe.exe. 返回 (path_or_empty, 说明).

    优先级:
      1. 用户在 UI 手填的
      2. 环境变量 FFPROBE / PATH 里的 ffprobe / ffprobe.exe
      3. exe 同目录 / PyInstaller _MEIPASS 目录
    """
    import shutil
    from pathlib import Path
    hint = (user_hint or "").strip().strip('"').strip("'")
    if hint:
        p = Path(hint)
        if p.is_file():
            return str(p), f"用 UI 手填: {p}"
        return "", f"UI 手填路径不存在: {p}"

    env_p = os.environ.get("FFPROBE")
    if env_p:
        p = Path(env_p)
        if p.is_file():
            return str(p), f"从环境变量 FFPROBE 找到: {p}"

    which = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if which:
        return which, f"从 PATH 找到: {which}"

    # exe 同目录
    exe_dir = Path(sys.executable).resolve().parent
    for name in ("ffprobe.exe", "ffprobe"):
        cand = exe_dir / name
        if cand.is_file():
            return str(cand), f"在 exe 同目录找到: {cand}"

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for name in ("ffprobe.exe", "ffprobe"):
            cand = Path(meipass) / name
            if cand.is_file():
                return str(cand), f"在 PyInstaller _MEIPASS 找到: {cand}"

    return "", "PATH / 环境变量 / exe 同目录 / _MEIPASS 都没找到"


def run_video_duration_diagnostics(video_path: str, ffprobe_hint: str = "") -> str:
    r"""跑视频时长 (ffprobe) 全套诊断. v0.4.96 新增.

    覆盖场景: stats_viewer 详情面板'视频时长(s)'一直空 / footer'视频总时长'一直 0 秒.
    分三级排查:
      1) ffprobe 能不能找到
      2) 视频文件本身可读吗 (safe_stat 两级)
      3) ffprobe 喂原始短路径 / \\?\ 长路径 / UNC 三种参数, 打完整 rc/stdout/stderr
    """
    from pathlib import Path
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    w("=" * 60)
    w("pic-clear 视频时长 (ffprobe) 诊断报告")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"Python   : {sys.version.split()[0]}")
    w(f"平台     : {platform.system()} {platform.release()}")
    w("=" * 60)

    # ----- 1) 定位 ffprobe -----
    ffprobe, hint_msg = _resolve_ffprobe_for_diag(ffprobe_hint)
    w("")
    w("[1] 定位 ffprobe")
    w(f"    {hint_msg}")
    if not ffprobe:
        w("")
        w("!! 结论: 找不到 ffprobe.exe. 装 ffmpeg 或把 ffprobe.exe 放到 exe 同目录.")
        w("   (extract_frames.exe 也吃 --ffmpeg 参数, ffprobe 会在同目录找)")
        return "\n".join(lines)

    # ffprobe 版本自检
    try:
        r = subprocess.run(
            [ffprobe, "-version"], capture_output=True, text=True,
            timeout=5, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        first_line = (r.stdout or "").splitlines()[0] if r.stdout else ""
        w(f"    版本自检 : rc={r.returncode}  {first_line}")
    except Exception as e:
        w(f"    版本自检 : 异常 {type(e).__name__}: {e}")

    # ----- 2) 视频文件属性 -----
    w("")
    w("[2] 视频文件属性")
    raw = video_path
    norm = _normalize_windows_path(raw)
    long_p = _to_long_path(norm)
    w(f"    原始路径 : {raw}")
    w(f"    归一化   : {norm}  (len={len(norm)})")
    w(f"    长路径   : {long_p}  (len={len(long_p)})")

    # 两级 stat
    for label, tgt in (("短路径", norm), ("长路径", long_p)):
        try:
            st = os.stat(tgt)
            w(f"    {label} os.stat : OK  size={st.st_size}  "
              f"mtime={datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}")
        except Exception as e:
            w(f"    {label} os.stat : ERR  {type(e).__name__}: {e}")

    # ----- 3) 三种参数喂 ffprobe -----
    w("")
    w("[3] 三种路径参数喂 ffprobe -show_entries=format=duration")

    attempts = [("原始短路径", norm), ("长路径 \\?\\ 前缀", long_p)]
    # 如果 Z: 之类映射盘且能 resolve 出 UNC, 也试一下
    try:
        if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
            rc, unc = _resolve_mapped_drive_to_unc(norm[0])
            if unc:
                unc_full = _to_unc_full(norm, unc)
                attempts.append(("UNC \\?\\UNC", unc_full))
                w(f"    (检测到映射盘 {norm[0]}: -> {unc}, 追加 UNC 尝试)")
    except Exception as e:
        w(f"    (映射盘解析异常, 跳过 UNC 尝试: {type(e).__name__}: {e})")

    got_duration: float | None = None
    for label, arg in attempts:
        w("")
        w(f"    --- 尝试: {label} ---")
        w(f"    参数     : {arg}")
        r = _run_ffprobe_once(ffprobe, arg)
        if r["exc"]:
            w(f"    subprocess 异常: {r['exc']}")
            continue
        w(f"    rc       : {r['rc']}")
        stdout = r["stdout"]
        stderr = r["stderr"]
        w(f"    stdout   : {stdout!r}")
        if stderr:
            w(f"    stderr   : {stderr[:500]!r}"
              f"{' ...(截断)' if len(stderr) > 500 else ''}")
        if r["rc"] == 0 and stdout:
            try:
                d = float(stdout)
                w(f"    -> 解析成功 duration={d:.3f} 秒 = {int(d)//60}分{int(d)%60}秒")
                if got_duration is None:
                    got_duration = d
            except ValueError as e:
                w(f"    -> stdout 解析失败: {e}")

    # ----- 4) 若全挂, 加跑一次 show_streams 看 codec 元信息 -----
    if got_duration is None:
        w("")
        w("[4] 3 种都拿不到 duration, 加跑 -show_streams 看能否读到视频头")
        cmd = [ffprobe, "-v", "error",
               "-show_streams", "-of", "default=noprint_wrappers=1", norm]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=15, encoding="utf-8", errors="replace",
                               creationflags=_CREATE_NO_WINDOW)
            w(f"    rc={r.returncode}")
            out = (r.stdout or "").strip()
            if out:
                for line in out.splitlines()[:20]:
                    w(f"      {line}")
            else:
                w("      (stdout 空)")
            err = (r.stderr or "").strip()
            if err:
                w(f"    stderr: {err[:400]!r}"
                  f"{' ...(截断)' if len(err) > 400 else ''}")
        except Exception as e:
            w(f"    异常: {type(e).__name__}: {e}")

    # ----- 结论 -----
    w("")
    w("=" * 60)
    if got_duration is not None:
        w(f"结论: 至少一级能拿到时长 = {got_duration:.3f} 秒.")
        w("      extract_frames.exe 会用相同两级 (原始短路径 / 长路径) 尝试,")
        w("      正常情况下 stats_viewer 里就会有值 (需要新版切帧 exe 重新抽一次).")
    else:
        w("结论: 3 种参数都拿不到时长. 把本报告全文贴给作者, 我看 stderr 定位.")
        w("      常见原因:")
        w("        - 视频编码/容器 ffprobe 不支持 (h265 裸流常见, 需要更完整的 ffprobe build)")
        w("        - 视频头损坏 / 文件被截断")
        w("        - 网络盘 IO 超时")
    w("=" * 60)
    return "\n".join(lines)


class DiagApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("980x720")

        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True)

        # Tab 1: 图片打开诊断 (原功能)
        img_tab = ttk.Frame(nb)
        nb.add(img_tab, text="图片打开诊断")
        self._build_image_tab(img_tab)

        # Tab 2: Marker 诊断 (v0.4.65 新增)
        mk_tab = ttk.Frame(nb)
        nb.add(mk_tab, text="Marker 诊断")
        self._build_marker_tab(mk_tab)

        # Tab 3: 日志批量诊断 (v0.4.70 新增)
        lb_tab = ttk.Frame(nb)
        nb.add(lb_tab, text="日志批量诊断")
        self._build_logbatch_tab(lb_tab)

        # Tab 4: 环境体检 (v0.4.71 新增) 放最前面用户能第一眼看到
        env_tab = ttk.Frame(nb)
        nb.add(env_tab, text="环境体检")
        self._build_env_tab(env_tab)

        # Tab 5: 视频时长 (ffprobe) 诊断 (v0.4.96 新增)
        vd_tab = ttk.Frame(nb)
        nb.add(vd_tab, text="视频时长诊断")
        self._build_videodur_tab(vd_tab)

        # Tab 6: 深路径去重诊断 (v0.4.110 新增)
        dd_tab = ttk.Frame(nb)
        nb.add(dd_tab, text="深路径去重诊断")
        self._build_deepdir_tab(dd_tab)

        # Tab 7: 深路径去重决策复现 (v0.4.111 新增)
        dr_tab = ttk.Frame(nb)
        nb.add(dr_tab, text="去重决策复现")
        self._build_replay_tab(dr_tab)

        # 挪到首位显示 (用户开 diag_pic 先看环境)
        nb.select(env_tab)
        # 启动自动跑一次
        self.root.after(200, self._auto_run_env)

    # ---------- Tab 1: 图片诊断 ----------

    def _build_image_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="图片路径:").pack(side=tk.LEFT)
        self.path_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.path_var)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览...", command=self.on_browse).pack(side=tk.LEFT, padx=2)

        btns = ttk.Frame(parent, padding=(8, 0))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始诊断", command=self.on_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志", command=self.on_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志", command=self.on_copy).pack(side=tk.LEFT)
        ttk.Label(btns, text=f"  {APP_TITLE}", foreground="#888").pack(side=tk.RIGHT)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL, command=self.txt.xview)
        self.txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.txt.bind("<Control-a>", self._select_all_img)
        self.txt.bind("<Control-A>", self._select_all_img)

        self._append(f"欢迎使用 {APP_TITLE}\n")
        self._append("使用步骤:\n")
        self._append("  1) 点 [浏览...] 选一张真实存在的图片 (jpg/png/bmp/webp)\n")
        self._append("  2) 点 [开始诊断]\n")
        self._append("  3) 结果自动填到本框, 用 [复制全部日志] 或 Ctrl+A / Ctrl+C 复制\n")
        self._append("  4) 把结果贴给作者定位\n\n")

    def _select_all_img(self, _event=None):
        self.txt.tag_add("sel", "1.0", "end")
        return "break"

    def _append(self, s: str) -> None:
        self.txt.insert(tk.END, s)
        self.txt.see(tk.END)

    def on_browse(self):
        p = filedialog.askopenfilename(
            title="选一张图片做诊断",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if p:
            self.path_var.set(p)

    def on_clear(self):
        self.txt.delete("1.0", tk.END)

    def on_copy(self):
        try:
            content = self.txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "全部日志已复制到剪贴板, 直接粘贴给作者即可")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_run(self):
        p = self.path_var.get().strip().strip(chr(34)).strip("'")
        if not p:
            messagebox.showwarning("请选图片", "请先点 [浏览...] 选一张真实存在的图片")
            return
        self._append(f"\n>>> 开始诊断: {p}\n\n")
        self.root.update()
        try:
            report = run_diagnostics(p)
        except Exception:
            report = "诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._append(report + "\n")

    # ---------- Tab 2: Marker 诊断 ----------

    def _build_marker_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Marker 根目录:").pack(side=tk.LEFT)
        self.mk_root_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.mk_root_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览...", command=self.on_mk_browse).pack(
            side=tk.LEFT, padx=2)

        opt = ttk.Frame(parent, padding=(8, 0))
        opt.pack(fill=tk.X)
        ttk.Label(opt, text="样本上限:").pack(side=tk.LEFT)
        self.mk_sample_var = tk.IntVar(value=5)
        ttk.Spinbox(opt, from_=1, to=50, textvariable=self.mk_sample_var,
                    width=6).pack(side=tk.LEFT, padx=6)
        ttk.Label(opt, text="(每类样本最多打印几条; 全量扫描不受此限)",
                  foreground="#888").pack(side=tk.LEFT)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始扫描 marker",
                   command=self.on_mk_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_mk_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_mk_copy).pack(side=tk.LEFT)

        # v0.4.68: 单条 marker 深度诊断 (贴一条 _done.marker 完整路径)
        row_one = ttk.Frame(parent, padding=(8, 4))
        row_one.pack(fill=tk.X)
        ttk.Label(row_one, text="单条 marker 完整路径:").pack(side=tk.LEFT)
        self.mk_one_var = tk.StringVar()
        ttk.Entry(row_one, textvariable=self.mk_one_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(row_one, text="深度诊断这一条",
                   command=self.on_mk_one_run).pack(side=tk.LEFT)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.mk_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.mk_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.mk_txt.xview)
        self.mk_txt.configure(yscrollcommand=yscroll.set,
                              xscrollcommand=xscroll.set)
        self.mk_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.mk_txt.bind("<Control-a>", self._select_all_mk)
        self.mk_txt.bind("<Control-A>", self._select_all_mk)

        self._mk_append("Marker 诊断工具 (v0.4.65+)\n\n")
        self._mk_append("用法:\n")
        self._mk_append("  1) 填 marker 根目录 (跟 extract_gui '--markers-root' 一致,\n")
        self._mk_append("     例如 \\\\filestor01...\\节点\\sjbz_20260717\\01)\n")
        self._mk_append("  2) 点 [开始扫描 marker]\n")
        self._mk_append("  3) 报告会列出:\n")
        self._mk_append("       - 短路径 rglob vs 长路径 rglob 数量差\n")
        self._mk_append("         (差 >0 说明短路径 scandir silently 漏了)\n")
        self._mk_append("       - safe_is_file 各分支命中比例\n")
        self._mk_append("         (hit_long_stat 高 = SMB isfile 撒谎, v0.4.65 兜底救回)\n")
        self._mk_append("       - MISS 样本 (marker 客观在但判定全 False, 会导致重跑)\n\n")

    def _select_all_mk(self, _event=None):
        self.mk_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _mk_append(self, s: str) -> None:
        self.mk_txt.insert(tk.END, s)
        self.mk_txt.see(tk.END)

    def on_mk_browse(self):
        p = filedialog.askdirectory(title="选 Marker 根目录")
        if p:
            self.mk_root_var.set(p)

    def on_mk_clear(self):
        self.mk_txt.delete("1.0", tk.END)

    def on_mk_copy(self):
        try:
            content = self.mk_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "Marker 诊断日志已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_mk_run(self):
        root = self.mk_root_var.get().strip().strip(chr(34)).strip("'")
        if not root:
            messagebox.showwarning("请选目录",
                                    "请先填或浏览一个 marker 根目录")
            return
        sample = max(1, int(self.mk_sample_var.get()))
        self._mk_append(f"\n>>> 开始扫描 marker: {root}\n\n")
        self.root.update()
        try:
            report = run_marker_diagnostics(root, sample_limit=sample)
        except Exception:
            report = "扫描脚本自身抛异常:\n" + traceback.format_exc()
        self._mk_append(report + "\n")

    def on_mk_one_run(self):
        p = self.mk_one_var.get().strip().strip(chr(34)).strip("'")
        if not p:
            messagebox.showwarning("请贴路径",
                "请把 [MARKER_MISS] 日志里的完整 marker 路径贴进这个输入框, "
                "以 _done.marker 结尾")
            return
        self._mk_append(f"\n>>> 单条深度诊断: {p}\n\n")
        self.root.update()
        try:
            report = run_single_marker_diagnostics(p)
        except Exception:
            report = "深度诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._mk_append(report + "\n")

    # ---------- Tab 3: 日志批量诊断 (v0.4.70) ----------

    def _build_logbatch_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="切帧日志文件:").pack(side=tk.LEFT)
        self.lb_path_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.lb_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览...", command=self.on_lb_browse).pack(
            side=tk.LEFT, padx=2)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始批量诊断",
                   command=self.on_lb_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_lb_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_lb_copy).pack(side=tk.LEFT)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.lb_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.lb_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.lb_txt.xview)
        self.lb_txt.configure(yscrollcommand=yscroll.set,
                              xscrollcommand=xscroll.set)
        self.lb_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.lb_txt.bind("<Control-a>", self._select_all_lb)
        self.lb_txt.bind("<Control-A>", self._select_all_lb)

        self._lb_append("日志批量 Marker 诊断  (v0.4.70+)\n\n")
        self._lb_append("用法:\n")
        self._lb_append("  1) 浏览选一份切帧日志 (extract_gui 或 pipeline 落地的 .log/.txt)\n")
        self._lb_append("  2) 点 [开始批量诊断]\n")
        self._lb_append("  3) 工具会自动:\n")
        self._lb_append("       - 抓日志里所有 [MARKER_MISS] 段的 marker 路径\n")
        self._lb_append("       - 每条 marker 用 5 层 API 复查一遍:\n")
        self._lb_append("           isfile(短) / isfile(长) / stat / listdir / FindFirstFileW\n")
        self._lb_append("       - 汇总哪一层能救回、哪一层撒谎、哪些真的丢失\n")
        self._lb_append("  4) 把结果复制贴给作者定位.\n\n")

    def _select_all_lb(self, _event=None):
        self.lb_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _lb_append(self, s: str) -> None:
        self.lb_txt.insert(tk.END, s)
        self.lb_txt.see(tk.END)

    def on_lb_browse(self):
        p = filedialog.askopenfilename(
            title="选切帧日志文件",
            filetypes=[("Log files", "*.log *.txt *.out"),
                       ("All files", "*.*")])
        if p:
            self.lb_path_var.set(p)

    def on_lb_clear(self):
        self.lb_txt.delete("1.0", tk.END)

    def on_lb_copy(self):
        try:
            content = self.lb_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "日志批量诊断已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_lb_run(self):
        p = self.lb_path_var.get().strip().strip(chr(34)).strip("'")
        if not p:
            messagebox.showwarning("请选文件", "请先浏览选一份切帧日志")
            return
        self._lb_append(f"\n>>> 开始批量诊断: {p}\n\n")
        self.root.update()
        try:
            report = run_log_batch_diagnostics(p)
        except Exception:
            report = "批量诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._lb_append(report + "\n")

    # ---------- Tab 4: 环境体检 (v0.4.71) ----------

    def _build_env_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="磁盘 / SMB / 长路径注册表 一键体检",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="重新体检",
                   command=self.on_env_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_env_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_env_copy).pack(side=tk.LEFT)
        ttk.Label(btns,
                  text="  (启动时会自动跑一次, 约 3-8 秒)",
                  foreground="#888").pack(side=tk.LEFT, padx=8)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.env_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.env_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.env_txt.xview)
        self.env_txt.configure(yscrollcommand=yscroll.set,
                               xscrollcommand=xscroll.set)
        self.env_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.env_txt.bind("<Control-a>", self._select_all_env)
        self.env_txt.bind("<Control-A>", self._select_all_env)

        self._env_append("环境体检 (v0.4.71+)\n\n")
        self._env_append("排查 pic-clear marker 判定异常前, 先看环境画像:\n")
        self._env_append("  - 磁盘类型 (本地 / 映射 / 网络位置 / UNC 直连)\n")
        self._env_append("  - SMB 协商 dialect (SMB2/SMB3.1.1) + 是否 Samba 服务端\n")
        self._env_append("  - SMB 缓存注册表 (FileNotFound/Directory/FileInfo Lifetime)\n")
        self._env_append("  - Windows LongPathsEnabled 开关\n")
        self._env_append("  - 常用根路径可达性\n\n")
        self._env_append("(启动时自动跑一次; 手动可点 [重新体检])\n")

    def _select_all_env(self, _event=None):
        self.env_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _env_append(self, s: str) -> None:
        self.env_txt.insert(tk.END, s)
        self.env_txt.see(tk.END)

    def on_env_clear(self):
        self.env_txt.delete("1.0", tk.END)

    def on_env_copy(self):
        try:
            content = self.env_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "环境体检报告已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_env_run(self):
        self._env_append("\n>>> 开始体检 ... (可能需要 3-8 秒)\n\n")
        self.root.update()
        try:
            report = collect_env_report()
        except Exception:
            report = "体检脚本自身抛异常:\n" + traceback.format_exc()
        self._env_append(report + "\n")

    def _auto_run_env(self):
        # 启动自动跑, 用户开 diag_pic 就能直接看到环境画像
        try:
            self.on_env_run()
        except Exception as e:
            self._env_append(f"\n[自动体检失败] {type(e).__name__}: {e}\n")

    # ---------- Tab 5: 视频时长诊断 (v0.4.96) ----------

    def _build_videodur_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="视频路径:").pack(side=tk.LEFT)
        self.vd_path_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.vd_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览...",
                   command=self.on_vd_browse_video).pack(side=tk.LEFT, padx=2)

        top2 = ttk.Frame(parent, padding=(8, 0))
        top2.pack(fill=tk.X)
        ttk.Label(top2, text="ffprobe (可空自动找):").pack(side=tk.LEFT)
        self.vd_ffprobe_var = tk.StringVar()
        ttk.Entry(top2, textvariable=self.vd_ffprobe_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top2, text="浏览...",
                   command=self.on_vd_browse_ffprobe).pack(side=tk.LEFT, padx=2)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始诊断",
                   command=self.on_vd_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_vd_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_vd_copy).pack(side=tk.LEFT)
        ttk.Label(btns, text="  用于排查 stats_viewer '视频时长'为空",
                  foreground="#888").pack(side=tk.RIGHT)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.vd_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.vd_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.vd_txt.xview)
        self.vd_txt.configure(yscrollcommand=yscroll.set,
                              xscrollcommand=xscroll.set)
        self.vd_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.vd_txt.bind("<Control-a>", self._select_all_vd)
        self.vd_txt.bind("<Control-A>", self._select_all_vd)

        self._vd_append(
            "用于排查 stats_viewer '视频时长(s)' 空 / footer '视频总时长' 0 秒.\n"
            "使用步骤:\n"
            "  1) [浏览...] 选一个 stats_viewer 里 duration 为空的视频文件\n"
            "  2) ffprobe 路径通常空着即可, 自动找 (env FFPROBE / PATH / exe 同目录)\n"
            "  3) [开始诊断], 结果会分 3-4 步打印\n"
            "  4) [复制全部日志] 贴给作者定位到底哪级失败\n\n"
        )

    def _select_all_vd(self, _event=None):
        self.vd_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _vd_append(self, s: str) -> None:
        self.vd_txt.insert(tk.END, s)
        self.vd_txt.see(tk.END)

    def on_vd_browse_video(self):
        p = filedialog.askopenfilename(
            title="选一个视频做诊断",
            filetypes=[
                ("视频文件", "*.mp4 *.mov *.mkv *.avi *.wmv *.flv "
                              "*.webm *.h264 *.h265 *.hevc *.ts *.m2ts"),
                ("所有文件", "*.*"),
            ],
        )
        if p:
            self.vd_path_var.set(p)

    def on_vd_browse_ffprobe(self):
        p = filedialog.askopenfilename(
            title="选 ffprobe.exe",
            filetypes=[("ffprobe", "ffprobe*.exe ffprobe"),
                       ("所有文件", "*.*")],
        )
        if p:
            self.vd_ffprobe_var.set(p)

    def on_vd_clear(self):
        self.vd_txt.delete("1.0", tk.END)

    def on_vd_copy(self):
        try:
            content = self.vd_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo(
                "已复制",
                "全部日志已复制到剪贴板, 直接粘贴给作者即可",
            )
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_vd_run(self):
        vp = self.vd_path_var.get().strip().strip(chr(34)).strip("'")
        if not vp:
            messagebox.showwarning(
                "请选视频",
                "请先点 [浏览...] 选一个 stats_viewer 里 duration 为空的视频",
            )
            return
        self._vd_append(f"\n>>> 开始诊断: {vp}\n\n")
        self.root.update()
        try:
            report = run_video_duration_diagnostics(
                vp, self.vd_ffprobe_var.get())
        except Exception:
            report = "诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._vd_append(report + "\n")


    # ---------- Tab 6: 深路径去重诊断 (v0.4.110) ----------

    def _build_deepdir_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="深路径去重诊断",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="选一个去重目标目录开始诊断",
                   command=self.on_dd_pick_and_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_dd_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_dd_copy).pack(side=tk.LEFT)
        ttk.Label(btns,
                  text="  (只点一次目录, 剩下的 diag_pic 全自动跑)",
                  foreground="#888").pack(side=tk.LEFT, padx=8)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.dd_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.dd_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.dd_txt.xview)
        self.dd_txt.configure(yscrollcommand=yscroll.set,
                              xscrollcommand=xscroll.set)
        self.dd_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.dd_txt.bind("<Control-a>", self._select_all_dd)
        self.dd_txt.bind("<Control-A>", self._select_all_dd)

        self._dd_append("深路径去重诊断  (v0.4.110+)\n\n")
        self._dd_append("用途: 一键定位'去重日志显示删除 0 张, 但目录里数量在减少'这类问题.\n\n")
        self._dd_append("怎么用:\n")
        self._dd_append("  1) 点 [选一个去重目标目录开始诊断]\n")
        self._dd_append("  2) 在弹出的目录选择框里, 选 GUI 上'去重目标'那一栏填的目录\n")
        self._dd_append("     (可以是网络位置 \\\\filestor01...\\..., 也可以是 Z: / D:)\n")
        self._dd_append("  3) diag_pic 会自动:\n")
        self._dd_append("     - 判断路径类型 (本地 / 映射盘 / UNC 直连 / 长路径)\n")
        self._dd_append("     - 递归找里面最深 / 最长的一张图\n")
        self._dd_append("     - 对这张图用 5 种前缀变体 (原始 / 归一化 / 长路径 / 畸形 / tkinter 混斜杠)\n")
        self._dd_append("       跑 exists / isfile / stat / open / FindFirstFileW\n")
        self._dd_append("     - 复现 dedupe_gui._find_dedupe_targets, 显示它真实返回的目录列表\n")
        self._dd_append("     - 对最深图跑 dhash, 看是否黑图/空图\n")
        self._dd_append("     - tasklist 扫残留 sqDedupe/dedupe_pic 进程\n")
        self._dd_append("  4) 用 [复制全部日志] 把结果贴给作者\n\n")

    def _select_all_dd(self, _event=None):
        self.dd_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _dd_append(self, s: str) -> None:
        self.dd_txt.insert(tk.END, s)
        self.dd_txt.see(tk.END)

    def on_dd_clear(self):
        self.dd_txt.delete("1.0", tk.END)

    def on_dd_copy(self):
        try:
            content = self.dd_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "深路径去重诊断报告已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_dd_pick_and_run(self):
        d = filedialog.askdirectory(
            title="选一个去重目标目录 (跟 sqDedupeGui 里'去重目标'那栏填的一致)",
            mustexist=True,
        )
        if not d:
            return
        self._dd_append(f"\n>>> 开始诊断: {d}\n")
        self._dd_append("    (递归扫图 + 5 变体探测, 大目录可能 5-30 秒, 请稍候...)\n\n")
        self.root.update()
        try:
            report = run_deepdir_diagnostics(d)
        except Exception:
            report = "诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._dd_append(report + "\n")


    # ---------- Tab 7: 深路径去重决策复现 (v0.4.111) ----------

    def _build_replay_tab(self, parent):
        top = ttk.Frame(parent, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="深路径去重决策复现",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)

        btns = ttk.Frame(parent, padding=(8, 4))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="选一个 camera 目录 (200 张图那种) 开始复现",
                   command=self.on_dr_pick_and_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志",
                   command=self.on_dr_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志",
                   command=self.on_dr_copy).pack(side=tk.LEFT)

        body = ttk.Frame(parent, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.dr_txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL,
                                command=self.dr_txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL,
                                command=self.dr_txt.xview)
        self.dr_txt.configure(yscrollcommand=yscroll.set,
                              xscrollcommand=xscroll.set)
        self.dr_txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.dr_txt.bind("<Control-a>", self._select_all_dr)
        self.dr_txt.bind("<Control-A>", self._select_all_dr)

        self._dr_append("深路径去重决策复现  (v0.4.111+)\n\n")
        self._dr_append("用途: 直接在深路径目录上跑一遍 dedupe_pic 完整判定逻辑,\n")
        self._dr_append("      不真删, 只报告'如果 apply 会删几张', 定位为啥深路径下总说 0 张.\n\n")
        self._dr_append("要求: diag_pic.exe 同目录能找到 yolov8n.onnx (跟 sqDedupe.exe 一起放的).\n")
        self._dr_append("      找不到就自动跳过 YOLO, 只跑 dhash+cluster (会失去 motion 保护).\n\n")
        self._dr_append("怎么用:\n")
        self._dr_append("  1) 点 [选一个 camera 目录] 按钮\n")
        self._dr_append("  2) 在弹出的目录选择框里选一个 200 张图那种叶子目录\n")
        self._dr_append("     (例如 \\\\filestor01...\\...\\camera02)\n")
        self._dr_append("  3) diag_pic 自己跑完扫描+YOLO+motion+聚类, 大概 30-90 秒\n")
        self._dr_append("  4) 用 [复制全部日志] 贴给作者\n\n")

    def _select_all_dr(self, _event=None):
        self.dr_txt.tag_add("sel", "1.0", "end")
        return "break"

    def _dr_append(self, s: str) -> None:
        self.dr_txt.insert(tk.END, s)
        self.dr_txt.see(tk.END)

    def on_dr_clear(self):
        self.dr_txt.delete("1.0", tk.END)

    def on_dr_copy(self):
        try:
            content = self.dr_txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "决策复现报告已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_dr_pick_and_run(self):
        d = filedialog.askdirectory(
            title="选一个 200 张图的 camera 目录",
            mustexist=True,
        )
        if not d:
            return
        self._dr_append(f"\n>>> 开始复现: {d}\n")
        self._dr_append("    (读图 + YOLO + motion + 聚类, 大概 30-90 秒, 请稍候...)\n\n")
        self.root.update()
        try:
            report = run_dedup_replay(d)
        except Exception:
            report = "复现脚本自身抛异常:\n" + traceback.format_exc()
        self._dr_append(report + "\n")


def main():
    root = tk.Tk()
    try:
        # DPI 兼容: 高分屏不糊
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
    except Exception:
        pass
    DiagApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
