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
