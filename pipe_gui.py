# -*- coding: utf-8 -*-
"""
pipe_gui.py —— pic-clear 的图形前端

- 主窗口：tkinter 表单，选盘/选源目录/选输出/选子目录/调阈值/一键运行
- 系统托盘：pystray + Pillow 生成图标
- 全局快捷键：keyboard，默认 Ctrl+Alt+P 呼出状态浮层
- 环境自检：查 extract_frames.exe / dedupe_pic.exe / license.lic
- 后台运行：内部调 pipeline.cmd_submit()，跟 pipeline.exe 一样 detach worker
- 状态刷新：读 jobs 目录的 status.json + tasklist 检查 3 个 exe

编译成独立的 pipe_gui.exe，不影响现有 pipeline.exe / dedupe_pic.exe / extract_frames.exe。
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --- 依赖检查（GUI 打包时都会带上，但开发时给个友好报错） ---
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter：{e}", file=sys.stderr)
    sys.exit(1)

# 复用 pipeline 内部函数（同目录 py 文件；PyInstaller 也会一起打包）
import pipeline  # noqa: E402


# =========================================================================
# 图标（icon.ico / icon.png）
# =========================================================================

def _resource_path(name: str) -> str:
    """在 dev 模式和 PyInstaller onefile 打包后都能定位到资源文件。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = os.path.join(base, name)
        if os.path.exists(candidate):
            return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, name)


def _apply_window_icon(win) -> None:
    """给 Tk 窗口设置 pic-clear 图标。Windows 用 .ico，其他平台用 PhotoImage(.png)。
    失败静默：图标缺失不影响功能。"""
    try:
        ico = _resource_path("icon.ico")
        if os.name == "nt" and os.path.exists(ico):
            win.iconbitmap(ico)
            return
        png = _resource_path("icon.png")
        if os.path.exists(png):
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            # 挂到窗口上防止被 GC
            win._icon_photo_ref = img  # type: ignore[attr-defined]
    except Exception:
        pass



# =========================================================================
# 环境自检
# =========================================================================

REQUIRED_EXES = ["extract_frames.exe", "dedupe_pic.exe"]
REQUIRED_FILES = ["license.lic"]

# Windows 常见部署位置：System32 + pipe_gui.exe 同目录 + PATH
def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        dirs.append(Path(sysroot) / "System32")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    else:
        dirs.append(Path(__file__).resolve().parent)
    return dirs


# =========================================================================
# 配置持久化：记住上次的选择，下次打开自动填回来
# =========================================================================

import json


def _config_path() -> Path:
    r"""配置文件路径：%USERPROFILE%\.pic-clear\pipe_gui.json（跨用户/机器隔离）"""
    home = Path(os.path.expanduser("~"))
    return home / ".pic-clear" / "pipe_gui.json"


def _load_config() -> dict:
    """加载上次的配置。文件不存在 / 损坏 → 返回空 dict，走默认。"""
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    """保存配置。失败不抛异常（不能因为存配置失败影响主流程）。"""
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[WARN] 保存配置失败: {e}", file=sys.stderr)


def _find_pipeline_exe() -> str | None:
    """在 System32 / pipe_gui.exe 同目录 / PATH 里找 pipeline.exe。找不到返回 None。"""
    import shutil
    for d in _candidate_dirs():
        cand = d / "pipeline.exe"
        if cand.is_file():
            return str(cand)
    hit = shutil.which("pipeline.exe")
    return hit


def check_environment() -> tuple[bool, list[tuple[str, str, str]]]:
    """
    返回 (all_ok, rows)
    rows: [(name, status, path_or_hint), ...]
        status = "OK" | "MISS"
    """
    rows: list[tuple[str, str, str]] = []
    all_ok = True
    for name in REQUIRED_EXES + REQUIRED_FILES:
        found = None
        for d in _candidate_dirs():
            p = d / name
            if p.is_file():
                found = p
                break
        # 再兜底 PATH
        if not found and name.endswith(".exe"):
            import shutil
            hit = shutil.which(name)
            if hit:
                found = Path(hit)
        if found:
            rows.append((name, "OK", str(found)))
        else:
            rows.append((name, "MISS", "  ".join(str(d) for d in _candidate_dirs())))
            all_ok = False
    # 追加一条 worker 一致性预检：extract_frames / dedupe_pic 必须同源
    try:
        ok, resolved, errors = pipeline.preflight_check_workers()
        if ok:
            paths = {Path(v).parent for v in resolved.values()}
            if len(paths) == 1:
                where = next(iter(paths))
                rows.append(("worker 一致性", "OK", f"两个 worker 都来自 {where}"))
            else:
                # 不太应该出现（一致性预检通过但目录不同）；给个提示
                rows.append(("worker 一致性", "OK",
                             "  ,  ".join(f"{k}={v}" for k, v in resolved.items())))
        else:
            rows.append(("worker 一致性", "MISS",
                         " | ".join(errors[:3]) if errors else "预检失败"))
            all_ok = False
    except Exception as e:
        rows.append(("worker 一致性", "MISS", f"预检异常：{e}"))
        all_ok = False
    return all_ok, rows


# =========================================================================
# 数据盘/目录辅助
# =========================================================================

def list_drives() -> list[str]:
    """Windows：返回 ['C:', 'D:', 'Z:'] 之类。非 Windows：返回 ['/']。"""
    if os.name != "nt":
        return ["/"]
    from string import ascii_uppercase
    out = []
    for letter in ascii_uppercase:
        d = f"{letter}:\\"
        if Path(d).exists():
            out.append(f"{letter}:")
    return out


def list_subdirs(root: Path) -> list[str]:
    try:
        return sorted([p.name for p in root.iterdir() if p.is_dir()])
    except Exception:
        return []


# =========================================================================
# 状态查询：进程 + 最新 job
# =========================================================================

# pipe_gui.exe 也算进来：因为 pipe_gui 提交任务时 detach 出的 worker 就是自身副本
PROC_NAMES = ["pipe_gui.exe", "pipeline.exe", "extract_frames.exe", "dedupe_pic.exe"]


def query_processes() -> dict[str, dict]:
    """返回 { name: {"running": bool, "count": int, "pids": [..], "mem_kb": int} }"""
    result: dict[str, dict] = {n: {"running": False, "count": 0, "pids": [], "mem_kb": 0} for n in PROC_NAMES}
    if os.name != "nt":
        # 非 Windows 场景（开发调试），用 ps
        try:
            out = subprocess.check_output(["ps", "-A", "-o", "pid=,comm="], text=True)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid, comm = parts
                for n in PROC_NAMES:
                    if comm.endswith(n) or comm.endswith(n.replace(".exe", "")):
                        r = result[n]
                        r["running"] = True
                        r["count"] += 1
                        r["pids"].append(int(pid))
        except Exception:
            pass
        return result

    try:
        # tasklist /FO CSV /NH，逗号分隔，双引号包字段
        raw = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            text=True, encoding="gbk", errors="ignore",
        )
    except Exception:
        return result

    import csv, io
    reader = csv.reader(io.StringIO(raw))
    for row in reader:
        if len(row) < 5:
            continue
        name = row[0].strip()
        if name not in PROC_NAMES:
            continue
        try:
            pid = int(row[1].strip())
        except Exception:
            continue
        mem_raw = row[4].strip().replace(" K", "").replace(",", "").replace(",", "")
        try:
            mem_kb = int(mem_raw)
        except Exception:
            mem_kb = 0
        r = result[name]
        r["running"] = True
        r["count"] += 1
        r["pids"].append(pid)
        r["mem_kb"] += mem_kb
    return result


def find_latest_job(out_root: Path):
    jobs_root = out_root / ".pipeline" / "jobs"
    if not jobs_root.is_dir():
        return None
    subs = [p for p in jobs_root.iterdir() if p.is_dir()]
    if not subs:
        return None
    subs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pipeline._load_status(subs[0])


def format_kb(kb: int) -> str:
    if kb >= 1024:
        return f"{kb/1024:.1f} MB"
    return f"{kb} K"


# =========================================================================
# 一键杀死所有相关进程
# =========================================================================

# 会被清理的进程名（自己 pipe_gui.exe 只杀"其他实例"，当前实例排除）
KILL_TARGETS = ["pipeline.exe", "extract_frames.exe", "dedupe_pic.exe", "pipe_gui.exe"]


def kill_all_related(exclude_pids: list[int]) -> tuple[list[tuple[str, int, bool]], int]:
    """按名字杀掉所有 KILL_TARGETS，跳过 exclude_pids 里的 PID。

    返回 (results, killed_count)：
      results: [(name, pid, ok), ...] 每个尝试过的进程
      killed_count: 成功杀掉的进程数
    """
    results: list[tuple[str, int, bool]] = []
    killed = 0
    if os.name != "nt":
        # 非 Windows 场景：kill -9
        procs = query_processes()
        for name, info in procs.items():
            for pid in info["pids"]:
                if pid in exclude_pids:
                    continue
                try:
                    os.kill(pid, 9)
                    results.append((name, pid, True))
                    killed += 1
                except Exception:
                    results.append((name, pid, False))
        return results, killed

    # Windows：先查 PID 列表，逐个 taskkill /F /PID，跳过自己
    procs = query_processes()
    for name in KILL_TARGETS:
        info = procs.get(name)
        if not info or not info["running"]:
            continue
        for pid in info["pids"]:
            if pid in exclude_pids:
                continue
            try:
                rc = subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                ok = (rc == 0)
                results.append((name, pid, ok))
                if ok:
                    killed += 1
            except Exception:
                results.append((name, pid, False))
    return results, killed


def mark_latest_job_stopped(out_root: Path) -> str | None:
    """把最新一个还在 running/pending 的 job 状态改成 stopped，返回 job_id。"""
    st = find_latest_job(out_root)
    if not st:
        return None
    if st.state not in ("pending", "running"):
        return None
    st.state = "stopped"
    st.ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.last_message = "被『杀死所有进程』强制停止"
    try:
        # jobs 目录路径
        job_dir = out_root / ".pipeline" / "jobs" / st.job_id
        pipeline._save_status(job_dir, st)
        return st.job_id
    except Exception as e:
        print(f"[WARN] 更新 status.json 失败: {e}", file=sys.stderr)
        return None


# =========================================================================
# 主 GUI
# =========================================================================


# --- Tooltip 浮层 -----------------------------------------------------------

class _Tooltip:
    """轻量 tooltip：鼠标悬停 delay 毫秒后弹一个黄底 Toplevel，移开立即隐藏。
    不依赖三方库，Windows tkinter 原生够用。"""

    def __init__(self, widget: "tk.Widget", text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None):
        self._cancel_pending()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _on_leave(self, _event=None):
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)  # 无边框
        try:
            tip.wm_attributes("-topmost", True)
        except Exception:
            pass
        tip.wm_geometry(f"+{x}+{y}")
        # 黄底黑字，跟系统 tooltip 观感一致
        lbl = tk.Label(
            tip, text=self.text, justify="left",
            background="#ffffe0", foreground="#111",
            relief="solid", borderwidth=1,
            font=("Microsoft YaHei", 9),
            wraplength=360, padx=6, pady=3,
        )
        lbl.pack()
        self._tip = tip

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def _add_tip(widget: "tk.Widget", text: str) -> None:
    """给任意 widget 挂一个 tooltip（重复调用会重复挂，谨慎）。"""
    _Tooltip(widget, text)


def _tip_icon(parent: "tk.Widget", text: str) -> "tk.Label":
    """返回一个可以 pack/grid 的 ⓘ 图标 Label，鼠标悬停时展示 text。
    调用方负责把返回值 pack / grid 到合适位置。"""
    lbl = tk.Label(parent, text="ⓘ", foreground="#0066cc",
                   font=("Segoe UI", 10, "bold"), cursor="question_arrow")
    _Tooltip(lbl, text)
    return lbl


# --- COCO 80 类中英文映射 + 分组 ---------------------------------------------
#
# 用途：pipe_gui 和 dedupe_gui 的『保护类别』Tab 用中文标签渲染复选框，
# 用户勾选后再按英文名拼成 dedupe_pic.exe 的 --protect 参数。
#
# 保护规则（写死在 dedupe_pic.py，GUI 不动这个规则）：
#   - person             → 硬保护，命中即保留（不管有没有动）
#   - bicycle / car / motorcycle / bus / train / truck  → 软保护，相邻帧
#                          位置发生变化才保留，静止则可参与去重
#   - 其它 COCO 类别     → 也走"软保护"，但静态物品永远不会动，勾选后
#                          等同于永久保留（用户要自己清楚这一点）

# 保持顺序与 detector.COCO_NAMES 完全一致（后者不能改，模型输出下标绑定）
COCO_ZH: dict[str, str] = {
    "person": "人",
    "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车",
    "truck": "卡车", "boat": "船", "traffic light": "红绿灯",
    "fire hydrant": "消防栓", "stop sign": "停车标志",
    "parking meter": "停车计时器", "bench": "长椅",
    "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马",
    "sheep": "羊", "cow": "牛", "elephant": "大象", "bear": "熊",
    "zebra": "斑马", "giraffe": "长颈鹿",
    "backpack": "背包", "umbrella": "雨伞", "handbag": "手提包",
    "tie": "领带", "suitcase": "行李箱",
    "frisbee": "飞盘", "skis": "滑雪板", "snowboard": "单板滑雪",
    "sports ball": "运动球", "kite": "风筝",
    "baseball bat": "棒球棒", "baseball glove": "棒球手套",
    "skateboard": "滑板", "surfboard": "冲浪板", "tennis racket": "网球拍",
    "bottle": "瓶子", "wine glass": "酒杯", "cup": "杯子",
    "fork": "叉子", "knife": "刀", "spoon": "勺子", "bowl": "碗",
    "banana": "香蕉", "apple": "苹果", "sandwich": "三明治",
    "orange": "橙子", "broccoli": "西兰花", "carrot": "胡萝卜",
    "hot dog": "热狗", "pizza": "披萨", "donut": "甜甜圈", "cake": "蛋糕",
    "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶",
    "tv": "电视", "laptop": "笔记本", "mouse": "鼠标", "remote": "遥控器",
    "keyboard": "键盘", "cell phone": "手机",
    "microwave": "微波炉", "oven": "烤箱", "toaster": "烤面包机",
    "sink": "水槽", "refrigerator": "冰箱",
    "book": "书本", "clock": "时钟", "vase": "花瓶", "scissors": "剪刀",
    "teddy bear": "泰迪熊", "hair drier": "吹风机", "toothbrush": "牙刷",
}

# 分组显示，让 80 个复选框不堆成一坨
COCO_GROUPS: list[tuple[str, list[str]]] = [
    ("人（硬保护）", ["person"]),
    ("交通工具（默认车类软保护，运动才保留）", [
        "bicycle", "car", "motorcycle", "airplane", "bus", "train",
        "truck", "boat",
    ]),
    ("交通设施", [
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    ]),
    ("动物", [
        "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe",
    ]),
    ("配件 / 随身物品", [
        "backpack", "umbrella", "handbag", "tie", "suitcase",
    ]),
    ("运动器材", [
        "frisbee", "skis", "snowboard", "sports ball", "kite",
        "baseball bat", "baseball glove", "skateboard", "surfboard",
        "tennis racket",
    ]),
    ("餐具 / 食物", [
        "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
        "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
        "hot dog", "pizza", "donut", "cake",
    ]),
    ("家具", [
        "chair", "couch", "potted plant", "bed", "dining table", "toilet",
    ]),
    ("电器", [
        "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
        "microwave", "oven", "toaster", "sink", "refrigerator",
    ]),
    ("其它物品", [
        "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush",
    ]),
]

# 默认勾选：跟 dedupe_pic.py --protect 的默认值完全一致
COCO_DEFAULT_PROTECT: list[str] = [
    "person", "bicycle", "car", "motorcycle", "bus", "train", "truck",
]

# 「车类」软保护集合：勾了这些的默认逻辑跟 dedupe_pic.py 一致
COCO_VEHICLE_SET: set[str] = {
    "bicycle", "car", "motorcycle", "bus", "train", "truck",
}


# --- DPI 自适应 -------------------------------------------------------------

# Tk 的默认 scaling 系数（对应 72dpi 到 96dpi 的换算：96/72 = 1.333...）。
# 用户屏幕真实 scale 除以这个基线，就是我们要为 geometry 补的额外倍率。
_TK_DEFAULT_SCALING = 96.0 / 72.0


def _query_windows_dpi() -> float | None:
    """直接问 Windows 系统真实 DPI。

    Tk 自己的 winfo_fpixels("1i") 在 PyInstaller 打包 + 部分虚拟机 / 未声明
    DPI aware 的场景下会永远返回 96，导致 scale 一直算成 1.0。所以我们
    优先用 Windows API：
      1) GetDpiForSystem   Win10 1607+：系统 DPI（跟"显示设置"里那个 % 一致）
      2) GetDeviceCaps     兜底：桌面 DC 的 LOGPIXELSY
      3) 返回 None         非 Windows 或调用全部失败
    """
    if os.name != "nt":
        return None
    try:
        from ctypes import windll
    except Exception:
        return None
    # 1) GetDpiForSystem —— 最准，跟 Windows 显示设置里的 % 完全一致
    try:
        dpi = int(windll.user32.GetDpiForSystem())
        if dpi > 0:
            return float(dpi)
    except Exception:
        pass
    # 2) GetDeviceCaps(hdc, LOGPIXELSY=90) —— 老 Windows 也能用
    try:
        LOGPIXELSY = 90
        hdc = windll.user32.GetDC(0)
        if hdc:
            try:
                dpi = int(windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSY))
                if dpi > 0:
                    return float(dpi)
            finally:
                windll.user32.ReleaseDC(0, hdc)
    except Exception:
        pass
    return None


def _apply_dpi_scaling(root: "tk.Tk", *, min_scale: float = 1.0,
                       max_scale: float = 3.0) -> float:
    """把 Tk 的字号和 pixel 尺度按屏幕 DPI 放大。

    - 优先用 Windows API（GetDpiForSystem / GetDeviceCaps）拿真实 DPI；
      拿不到再退回到 Tk 的 winfo_fpixels("1i")
    - 环境变量 PIC_CLEAR_UI_SCALE 可强制指定倍率（相对 100%），
      调试或虚拟机 DPI 透传不准时救急用
    - Tk scaling 单位是"点/像素"，正确公式是 dpi / 72
    - 结果做上下限 clamp，防止极端 DPI（比如虚拟机报 300+）把 UI 撑爆
    - 返回值是"用户实际 scale ÷ Tk 默认 scale"，调用方可以把硬编码 geometry
      按这个倍率放大，效果跟 font 放大保持一致

    典型返回值：
      100% 屏（96dpi）  → 1.00
      125% 屏（120dpi） → 1.25
      150% 屏（144dpi） → 1.50
      200% 屏（192dpi） → 2.00
    """
    # 0) 环境变量强制指定：PIC_CLEAR_UI_SCALE=1.5 → 相当于 144dpi
    env_scale = os.environ.get("PIC_CLEAR_UI_SCALE", "").strip()
    if env_scale:
        try:
            forced = float(env_scale)
            if forced > 0:
                dpi = 96.0 * forced
            else:
                dpi = None
        except Exception:
            dpi = None
    else:
        dpi = None
    # 1) Windows API
    if dpi is None:
        dpi = _query_windows_dpi()
    # 2) 兜底：Tk 自己
    if dpi is None:
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
    if dpi <= 0:
        dpi = 96.0
    scaling = dpi / 72.0
    # clamp Tk scaling 自身（防止 fpixels 报离谱数字）
    scaling = max(1.0, min(scaling, max_scale * _TK_DEFAULT_SCALING))
    try:
        root.tk.call("tk", "scaling", scaling)
    except Exception:
        pass
    # 返回给调用方的倍率是"相对 Tk 默认"，clamp 到 [min_scale, max_scale]
    factor = scaling / _TK_DEFAULT_SCALING
    factor = max(min_scale, min(factor, max_scale))
    return factor


def _scale_geometry(w: int, h: int, scale: float) -> str:
    """把默认 geometry 按 scale 放大，返回 tk 认识的 'WxH' 字符串。"""
    return f"{int(round(w * scale))}x{int(round(h * scale))}"


def _parse_geometry_str(geo: str) -> tuple[int, int, int | None, int | None] | None:
    """把 Tk geometry 字符串（'WxH' 或 'WxH+X+Y'）解析成 (w, h, x, y)。
    只解析像素数字；解析失败返回 None。x/y 可为负（多屏场景）。"""
    if not geo or not isinstance(geo, str):
        return None
    try:
        # 常见形式：820x720 或 820x720+100+50 或 820x720-100+50（多屏）
        import re
        m = re.match(r"^\s*(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?\s*$", geo)
        if not m:
            return None
        w = int(m.group(1))
        h = int(m.group(2))
        x = int(m.group(3)) if m.group(3) else None
        y = int(m.group(4)) if m.group(4) else None
        return (w, h, x, y)
    except Exception:
        return None


def _compute_default_geometry(root: "tk.Tk", scale: float,
                              base_w: int = 820, base_h: int = 720,
                              min_w: int = 760, min_h: int = 560,
                              max_w_ratio: float = 0.80,
                              max_h_ratio: float = 0.85) -> str:
    """按屏幕大小 + DPI scale，算出主窗口首次打开的合理 WxH。

    规则：
      - 从 base_w/base_h * scale 出发（保持原有默认视觉体积）
      - 上限：屏幕宽度 * max_w_ratio、屏幕高度 * max_h_ratio
        —— 防止在小笔记本上撑出屏幕，或者主机没桌面任务栏空间
      - 下限：min_w/min_h * scale
        —— 防止超小屏幕（比如 800x600 老机器）算出的默认值过小
    """
    try:
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
    except Exception:
        sw, sh = 1920, 1080

    want_w = int(base_w * scale)
    want_h = int(base_h * scale)

    max_w = int(sw * max_w_ratio)
    max_h = int(sh * max_h_ratio)
    want_w = min(want_w, max_w)
    want_h = min(want_h, max_h)

    min_w_scaled = int(min_w * scale)
    min_h_scaled = int(min_h * scale)
    want_w = max(want_w, min_w_scaled)
    want_h = max(want_h, min_h_scaled)

    return f"{want_w}x{want_h}"


def _sanitize_saved_geometry(root: "tk.Tk", geo: str) -> str | None:
    """校验 saved geometry：
      - 必须能解析
      - 宽/高必须在合理范围（>= 400px 且 <= 屏幕物理尺寸）
      - 位置越界（比如外接屏拔了）也判为不合理
    合理返回原字符串，不合理返回 None。"""
    parsed = _parse_geometry_str(geo)
    if not parsed:
        return None
    w, h, x, y = parsed
    if w < 400 or h < 300:
        return None
    try:
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
    except Exception:
        sw, sh = 99999, 99999
    if w > sw or h > sh:
        return None
    # 位置越界不致命，Tk 自己会拉回来；这里只做温和校验
    return geo


def _enable_hidpi_awareness() -> None:
    """Windows 高 DPI 感知：让系统知道我们自己管缩放，别帮我们做模糊拉伸。
    要在 tk.Tk() 之前调用效果最好。"""
    if os.name != "nt":
        return
    try:
        from ctypes import windll
        # Per-monitor DPI aware（值 2），比 Process DPI aware（值 1）更好
        # 老 Windows 不支持就退回 1
        try:
            windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


_TAB_STYLE_APPLIED = False


def apply_tab_style(root) -> None:
    """轻量样式：只给选项卡加内边距，避免挤在一起。

    保留系统原生主题（Windows 下 vista、macOS 下 aqua），不强改颜色、
    不切换 theme，避免风格与系统脱节。
    """
    global _TAB_STYLE_APPLIED
    if _TAB_STYLE_APPLIED:
        return
    try:
        from tkinter import ttk
        style = ttk.Style(root)
        # 只调 padding：横向留白让 tab 之间有明显间距
        style.configure("TNotebook.Tab", padding=[14, 6])
        _TAB_STYLE_APPLIED = True
    except Exception:
        pass


def _run_diag_dpi() -> int:
    """`pipe_gui.exe --diag-dpi` 入口：打印 DPI 相关状态方便排障。

    输出去向：
      - 同时写到 stdout（cmd 直接运行时能看到）
      - 同时写到 %TEMP%\\pic_clear_diag_dpi.txt
      - 最后弹一个 Tk messagebox 展示全文（因为 --windowed 打包吞掉了 stdout）

    信息内容：
      - Windows 版本（sys.getwindowsversion）
      - Process DPI awareness 当前值（GetProcessDpiAwareness）
      - GetDpiForSystem 返回值
      - GetDeviceCaps(LOGPIXELSY) 返回值
      - 一个瞬时的 Tk 根窗口的 winfo_fpixels("1i") 读数
      - _apply_dpi_scaling 最终选出来的 scale
      - PIC_CLEAR_UI_SCALE 环境变量当前值
    退出码 0。
    """
    lines: list[str] = []
    def _p(s: str) -> None:
        lines.append(s)
        try:
            print(s)
        except Exception:
            pass

    _p("=" * 60)
    _p("  pipe_gui.exe --diag-dpi")
    _p("=" * 60)
    _p(f"[python]   {sys.version.splitlines()[0]}")
    _p(f"[platform] os.name={os.name}  sys.platform={sys.platform}")
    _p(f"[env]      PIC_CLEAR_UI_SCALE={os.environ.get('PIC_CLEAR_UI_SCALE','(未设置)')}")

    if os.name == "nt":
        try:
            wv = sys.getwindowsversion()
            _p(f"[winver]   major={wv.major} minor={wv.minor} build={wv.build}")
        except Exception as e:
            _p(f"[winver]   查询失败: {e}")
        try:
            from ctypes import windll, c_int, byref
            # GetProcessDpiAwareness（Win8.1+）
            try:
                v = c_int(-1)
                # 参数 0 表示当前进程
                windll.shcore.GetProcessDpiAwareness(0, byref(v))
                names = {0: "UNAWARE", 1: "SYSTEM_AWARE", 2: "PER_MONITOR_AWARE"}
                _p(f"[awareness] GetProcessDpiAwareness = {v.value} ({names.get(v.value,'?')})")
            except Exception as e:
                _p(f"[awareness] GetProcessDpiAwareness 失败: {e}")
            # GetDpiForSystem（Win10 1607+）
            try:
                dpi_sys = int(windll.user32.GetDpiForSystem())
                _p(f"[dpi]      GetDpiForSystem = {dpi_sys}  (100%=96, 125%=120, 150%=144, 200%=192)")
            except Exception as e:
                _p(f"[dpi]      GetDpiForSystem 失败: {e}")
            # GetDeviceCaps
            try:
                LOGPIXELSY = 90
                hdc = windll.user32.GetDC(0)
                if hdc:
                    try:
                        dpi_dc = int(windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSY))
                    finally:
                        windll.user32.ReleaseDC(0, hdc)
                    _p(f"[dpi]      GetDeviceCaps(LOGPIXELSY) = {dpi_dc}")
                else:
                    _p("[dpi]      GetDC(0) 返回 0")
            except Exception as e:
                _p(f"[dpi]      GetDeviceCaps 失败: {e}")
        except Exception as e:
            _p(f"[dpi]      ctypes.windll 加载失败: {e}")

    q = _query_windows_dpi()
    _p(f"[final]    _query_windows_dpi() = {q}")

    # 起一个隐藏 Tk 根，只为拿 winfo_fpixels，然后立刻销毁
    tk_root = None
    try:
        tk_root = tk.Tk()
        tk_root.withdraw()
        try:
            fpx = float(tk_root.winfo_fpixels("1i"))
        except Exception as e:
            fpx = f"失败: {e}"
        try:
            scale = _apply_dpi_scaling(tk_root)
        except Exception as e:
            scale = f"失败: {e}"
        _p(f"[tk]       winfo_fpixels('1i') = {fpx}")
        _p(f"[tk]       _apply_dpi_scaling  = {scale}")
    except Exception as e:
        _p(f"[tk]       Tk 初始化失败: {e}")

    _p("=" * 60)

    # 写文件（诊断结果落盘，无论 stdout 是否被吞）
    try:
        import tempfile
        report_path = Path(tempfile.gettempdir()) / "pic_clear_diag_dpi.txt"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        _p(f"[report]   已写入 {report_path}")
    except Exception as e:
        _p(f"[report]   写入失败: {e}")

    # 用 messagebox 展示（--windowed 打包吞了 stdout，需要 GUI 显示）
    if tk_root is not None:
        try:
            messagebox.showinfo("pipe_gui --diag-dpi 诊断", "\n".join(lines))
        except Exception:
            pass
        try:
            tk_root.destroy()
        except Exception:
            pass

    return 0


# --- 授权信息辅助 -----------------------------------------------------------

def _resolve_license_path() -> Path:
    """跟 pipeline._check_license_or_die 同一套路径解析：
    优先环境变量 → frozen 模式取 exe 同目录 → 否则取 CWD"""
    env_lic = os.environ.get("PIPELINE_LICENSE") or os.environ.get("DEDUPE_LICENSE")
    if env_lic:
        return Path(env_lic).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "license.lic"
    return Path.cwd() / "license.lic"


def _read_license_payload() -> dict:
    """尝试读并 base64+json 解析 license.lic 的 payload，用于展示。
    不做签名校验（校验交给 licensing.verify_license）。失败返回 {}。"""
    try:
        import base64
        p = _resolve_license_path()
        if not p.is_file():
            return {}
        lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
        if len(lines) < 1:
            return {}
        payload = base64.b64decode(lines[0], validate=True)
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def probe_license_status() -> dict:
    """一站式：拿本机指纹 + license 校验结果 + license payload。
    返回结构：
      {
        'fingerprint': 'XXXX-XXXX-XXXX-XXXX',
        'license_path': Path,
        'ok': True/False,
        'msg': '授权有效（发放给: xxx）' | '原因文字',
        'payload': {issued_to, expire_date, note, ...} 或 {}
      }
    """
    info = {"fingerprint": "?", "license_path": _resolve_license_path(),
            "ok": False, "msg": "", "payload": {}}
    try:
        from licensing import get_fingerprint, verify_license
    except Exception as e:
        info["msg"] = f"缺少 licensing 模块：{e}"
        return info
    try:
        info["fingerprint"] = get_fingerprint()
    except Exception as e:
        info["fingerprint"] = f"（获取失败：{e}）"
    try:
        ok, msg = verify_license(info["license_path"])
        info["ok"] = bool(ok)
        info["msg"] = str(msg)
    except Exception as e:
        info["ok"] = False
        info["msg"] = f"校验异常：{e}"
    info["payload"] = _read_license_payload()
    return info


def render_license_info(root, parent, info: dict | None) -> None:
    """把授权信息渲染到给定的父容器（ttk.Frame）里。
    先清空 parent 已有子控件，再依次显示 状态 / 发放给 / 过期 / 备注 / 指纹 / lic 路径。
    这是给 extract_gui / dedupe_gui 等复用的公共实现，与 pipe_gui 关于 tab 一致。
    """
    from tkinter import ttk
    import tkinter as tk
    for w in parent.winfo_children():
        w.destroy()
    if not info:
        info = probe_license_status()

    payload = info.get("payload") or {}
    issued_to = payload.get("issued_to", "-")
    expire = str(payload.get("expire_date", "never")).strip()
    expire_disp = "永久" if expire.lower() in ("never", "", "none") else expire
    note = payload.get("note", "") or ""
    lic_path = info.get("license_path")
    fp = info.get("fingerprint", "?")
    ok = bool(info.get("ok"))

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(6, 3))
    ttk.Label(row, text="状态：", width=12, foreground="#555").pack(side="left")
    ttk.Label(row,
              text=("✔ 已授权" if ok else "✘ 未授权"),
              foreground=("#0a7f2e" if ok else "#c0392b"),
              font=("Microsoft YaHei", 10, "bold")).pack(side="left")
    if info.get("msg"):
        ttk.Label(row, text=f"   ({info['msg']})",
                  foreground="#888",
                  font=("Microsoft YaHei", 9)).pack(side="left")

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=3)
    ttk.Label(row, text="发放给：", width=12, foreground="#555").pack(side="left")
    ttk.Label(row, text=issued_to,
              font=("Microsoft YaHei", 10)).pack(side="left")

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=3)
    ttk.Label(row, text="过期日期：", width=12, foreground="#555").pack(side="left")
    ttk.Label(row, text=expire_disp,
              font=("Microsoft YaHei", 10)).pack(side="left")

    if note:
        row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=3)
        ttk.Label(row, text="备注：", width=12, foreground="#555").pack(side="left")
        ttk.Label(row, text=note,
                  font=("Microsoft YaHei", 10)).pack(side="left")

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(8, 3))
    ttk.Label(row, text="本机指纹：", width=12, foreground="#555").pack(side="left")
    fp_entry = tk.Entry(row, font=("Consolas", 11, "bold"),
                        relief="flat", readonlybackground="#f5f5f5",
                        foreground="#0057b7", width=22)
    fp_entry.insert(0, fp)
    fp_entry.config(state="readonly")
    fp_entry.pack(side="left")

    copy_msg = tk.StringVar(value="")

    def do_copy():
        if _copy_to_clipboard(root, fp):
            copy_msg.set("✓ 已复制")
        else:
            copy_msg.set("✗ 复制失败")
        root.after(2000, lambda: copy_msg.set(""))

    ttk.Button(row, text="复制", command=do_copy, width=6).pack(side="left", padx=6)
    ttk.Label(row, textvariable=copy_msg,
              foreground="#0a7f2e",
              font=("Microsoft YaHei", 9)).pack(side="left")

    if lic_path:
        row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(8, 8))
        ttk.Label(row, text="license.lic 路径：",
                  foreground="#888",
                  font=("Microsoft YaHei", 9)).pack(side="left")
        path_entry = tk.Entry(row, font=("Consolas", 9),
                              relief="flat", readonlybackground="#f5f5f5",
                              foreground="#666")
        path_entry.insert(0, str(lic_path))
        path_entry.config(state="readonly")
        path_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))


def render_core_version_frame(root, parent, *,
                              label_text: str,
                              exe_finder,
                              probe_cmd_suffix: tuple[str, ...] = ("--version",),
                              missing_hint: str = ""):
    """在 parent (LabelFrame) 下渲染"内核版本 / 底层依赖"信息.

    参数
      label_text        : "内核版本"、"编排核心"、"抽帧内核" 之类
      exe_finder        : 无参函数, 返回 exe 绝对路径 str/Path 或 None
      probe_cmd_suffix  : 探测子进程时追加的参数, 默认 ("--version",)
      missing_hint      : 找不到 exe 时给用户看的补救建议

    返回一个 dict 便于外层保留引用触发 "重新检测":
        {"status_var": StringVar, "path_var": StringVar,
         "refresh": callable, "frame": frame}
    """
    from tkinter import ttk
    import tkinter as tk
    import os
    import subprocess
    import threading

    for w in parent.winfo_children():
        w.destroy()

    status_var = tk.StringVar(value="检测中…")
    path_var = tk.StringVar(value="")

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(6, 3))
    ttk.Label(row, text=f"{label_text}：", width=12,
              foreground="#555").pack(side="left")
    ttk.Label(row, textvariable=status_var,
              font=("Microsoft YaHei", 10, "bold")).pack(side="left")

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(0, 8))
    ttk.Label(row, text="exe 路径：", width=12,
              foreground="#888",
              font=("Microsoft YaHei", 9)).pack(side="left")
    ttk.Label(row, textvariable=path_var,
              foreground="#666",
              font=("Consolas", 9)).pack(side="left")

    def _probe_thread():
        exe = None
        try:
            exe = exe_finder()
        except Exception as e:
            root.after(0, lambda: (
                status_var.set(f"✘ 查找失败: {type(e).__name__}: {e}"),
                path_var.set(""),
            ))
            return
        if not exe:
            hint = missing_hint or "请把它放到本 GUI 同目录 / System32 / PATH 后重启"
            root.after(0, lambda: (
                status_var.set("✘ 缺失内核文件"),
                path_var.set(hint),
            ))
            return
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000
        try:
            result = subprocess.run(
                [str(exe), *probe_cmd_suffix],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
                creationflags=creationflags,
            )
            out = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.returncode != 0 and not out:
                out = f"(exit {result.returncode}, 无输出)"
            # v0.4.82: 有些子 exe 在 --version 之前会先打 [授权]/[FATAL] 等行,
            # 老逻辑 splitlines()[0] 会抓到那些行误当版本. 改成扫全部行, 优先
            # 挑"含 vX.Y.Z 的行", 挑不到才退回第 1 行.
            import re as _re_ver
            _ver_re = _re_ver.compile(r"v?\d+\.\d+\.\d+")
            _lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            version_line = next(
                (ln for ln in _lines if _ver_re.search(ln)),
                (_lines[0] if _lines else "(无输出)"),
            )
        except FileNotFoundError:
            root.after(0, lambda: (
                status_var.set("✘ 缺失内核文件"),
                path_var.set(str(exe)),
            ))
            return
        except subprocess.TimeoutExpired:
            root.after(0, lambda: (
                status_var.set("✘ 检测超时 (>15s)"),
                path_var.set(str(exe)),
            ))
            return
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            root.after(0, lambda: (
                status_var.set(f"✘ 检测失败: {err}"),
                path_var.set(str(exe)),
            ))
            return
        root.after(0, lambda: (
            status_var.set(f"✔ {version_line}"),
            path_var.set(str(exe)),
        ))

    def refresh():
        status_var.set("检测中…")
        path_var.set("")
        threading.Thread(target=_probe_thread, daemon=True,
                         name="probe-core-version").start()

    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(0, 6))
    ttk.Button(row, text="重新检测",
               command=refresh, width=10).pack(side="left")

    refresh()

    return {"status_var": status_var, "path_var": path_var,
            "refresh": refresh, "frame": parent}


def render_static_version_frame(parent, *,
                                label_text: str,
                                version_text: str,
                                extra_note: str = ""):
    """给内嵌模块(不走子进程 exe)用的静态版本区块.

    典型: classify_gui 里 classify_pic 是 import 的, 直接读 _version.VERSION 就行.
    """
    from tkinter import ttk
    for w in parent.winfo_children():
        w.destroy()
    row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(6, 3))
    ttk.Label(row, text=f"{label_text}：", width=12,
              foreground="#555").pack(side="left")
    ttk.Label(row, text=f"✔ {version_text}",
              font=("Microsoft YaHei", 10, "bold")).pack(side="left")
    if extra_note:
        row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(row, text=extra_note,
                  foreground="#888",
                  font=("Microsoft YaHei", 9)).pack(side="left")


def _copy_to_clipboard(root: "tk.Misc", text: str) -> bool:
    """跨会话可靠复制：clear + append + update，返回是否成功。"""
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return True
    except Exception:
        return False


def show_license_error_dialog(info: dict) -> None:
    """未授权时启动的对话框：
    - 顶部红字提示未授权
    - 中间大号显示本机指纹（可选中）+ 复制按钮
    - 说明如何拿 license.lic
    - 底部『退出』按钮
    关闭窗口后 sys.exit(3)。"""
    root = tk.Tk()
    root.title("pic-clear 编排工具 - 未授权")
    _apply_window_icon(root)
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    # 高 DPI：先声明 DPI 感知，再按屏幕 DPI 放大 Tk 缩放和 geometry
    _enable_hidpi_awareness()
    scale = _apply_dpi_scaling(root)
    root.geometry(_scale_geometry(560, 400, scale))

    pad = {"padx": 16, "pady": 6}

    tk.Label(root, text="⚠  本机未授权，无法运行",
             font=("Microsoft YaHei", 16, "bold"),
             foreground="#c0392b").pack(pady=(20, 4))

    tk.Label(root, text=info.get("msg", ""),
             font=("Microsoft YaHei", 9),
             foreground="#555", wraplength=520, justify="center").pack(**pad)

    tk.Label(root, text="本机指纹（发给作者获取 license.lic）：",
             font=("Microsoft YaHei", 10),
             foreground="#333").pack(pady=(14, 4))

    fp = info.get("fingerprint", "?")
    fp_entry = tk.Entry(root, font=("Consolas", 14, "bold"),
                        justify="center", relief="flat",
                        readonlybackground="#f5f5f5",
                        foreground="#0057b7", width=22)
    fp_entry.insert(0, fp)
    fp_entry.config(state="readonly")
    fp_entry.pack(pady=(0, 8))

    copied_var = tk.StringVar(value="")

    def do_copy():
        ok = _copy_to_clipboard(root, fp)
        copied_var.set("✓ 已复制到剪贴板" if ok else "✗ 复制失败")
        # 2 秒后清消息
        root.after(2000, lambda: copied_var.set(""))

    ttk.Button(root, text="复制指纹", command=do_copy, width=18).pack()
    tk.Label(root, textvariable=copied_var,
             foreground="#0a7f2e",
             font=("Microsoft YaHei", 9)).pack(pady=(2, 6))

    lic_path = info.get("license_path")
    hint = (
        f"操作步骤：\n"
        f"  1. 点上方『复制指纹』按钮\n"
        f"  2. 把指纹发给作者，索要 license.lic\n"
        f"  3. 把 license.lic 放到下面这个位置，重新运行本程序：\n"
        f"        {lic_path}"
    )
    tk.Label(root, text=hint, foreground="#555", justify="left",
             font=("Microsoft YaHei", 9)).pack(pady=(6, 6), padx=20, anchor="w")

    ttk.Button(root, text="退出", command=root.destroy, width=14).pack(pady=(4, 14))

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()




# ==================== TOTP 动态口令（v0.3.0 新增） ====================
# 说明：
# - 密钥文件 otp.secret（单行 base32）与 license.lic 并列放在 exe 同目录。
# - 通过后写 ~/.pic-clear/otp_session.json，24 小时内启动不再要求输入。
# - 三个 GUI (pipe_gui / extract_gui / dedupe_gui) 共用同一份 session。
# - 环境变量 PIC_CLEAR_SKIP_OTP=1 可跳过（仅供开发）；otp.secret 缺失将强制退出。
# - 6 位口令，容忍 ±90 秒（otp_utils.verify window=3）。
# - 错 3 次 → 冷却 60 秒。
# - 用户取消 / 关闭对话框 → sys.exit(4)。

OTP_SECRET_FILENAME = "otp.secret"
OTP_SESSION_PATH = Path.home() / ".pic-clear" / "otp_session.json"
OTP_SESSION_TTL = 24 * 3600
OTP_LOCKOUT_ATTEMPTS = 3
OTP_LOCKOUT_SECONDS = 60
# 备用测试口令：仅供内部快速测试，正式环境用真正的 TOTP
OTP_BACKUP_CODE = "235803"


def _resolve_otp_secret_path() -> Path:
    """otp.secret 的位置策略跟 license.lic 一致：
    优先环境变量 PIC_CLEAR_OTP_SECRET → frozen 模式取 exe 同目录 → 否则取 CWD。"""
    env_p = os.environ.get("PIC_CLEAR_OTP_SECRET")
    if env_p:
        return Path(env_p).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / OTP_SECRET_FILENAME
    return Path.cwd() / OTP_SECRET_FILENAME


def _read_otp_secret() -> str | None:
    """读 otp.secret（第一行有效的 base32），不存在或空文件返回 None。"""
    try:
        p = _resolve_otp_secret_path()
        if not p.is_file():
            return None
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except Exception:
        return None
    return None


def _otp_session_alive() -> bool:
    """当前 session 是否还在 24h 有效期内。"""
    try:
        if not OTP_SESSION_PATH.is_file():
            return False
        data = json.loads(OTP_SESSION_PATH.read_text(encoding="utf-8"))
        exp = float(data.get("expires_at", 0))
        return exp > time.time()
    except Exception:
        return False


def _otp_session_mark_ok() -> None:
    """记一次通过验证，24h 内不再要求输入。"""
    try:
        OTP_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        OTP_SESSION_PATH.write_text(
            json.dumps({"expires_at": time.time() + OTP_SESSION_TTL},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def show_otp_dialog(secret: str) -> bool:
    """弹出一个 6 位口令输入框，返回 True 表示验证通过。
    - Enter 提交，满 6 位数字自动提交
    - 错 OTP_LOCKOUT_ATTEMPTS 次后冷却 OTP_LOCKOUT_SECONDS 秒（每秒刷新倒计时）
    - 用户关窗口 / 点『退出』返回 False
    """
    try:
        from otp_utils import verify as _otp_verify
    except Exception as e:
        # otp_utils 都读不到，属于打包/环境异常；保守放行避免误伤
        print(f"[OTP] 加载 otp_utils 失败：{e}", file=sys.stderr)
        return True

    _enable_hidpi_awareness()
    root = tk.Tk()
    # 先隐藏窗口，DPI + geometry + 控件全部构造完再一次性显示，避免"小窗变大"
    try:
        root.withdraw()
    except Exception:
        pass
    root.title("pic-clear 编排工具 - 动态口令")
    _apply_window_icon(root)
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    scale = _apply_dpi_scaling(root)
    root.geometry(_scale_geometry(420, 260, scale))

    state = {"ok": False, "attempts": 0, "lock_until": 0.0}

    tk.Label(root, text="请输入 6 位动态口令",
             font=("Microsoft YaHei", 14, "bold")).pack(pady=(16, 4))
    tk.Label(root, text="口令每 30 秒变化一次，容忍 ±90 秒",
             foreground="#666", font=("Microsoft YaHei", 9)).pack()

    entry_var = tk.StringVar()
    entry = ttk.Entry(root, textvariable=entry_var,
                      width=10, justify="center",
                      show="●",
                      font=("Consolas", 22, "bold"))
    entry.pack(pady=(14, 6))
    entry.focus_set()

    msg_var = tk.StringVar(value="")
    msg_lbl = tk.Label(root, textvariable=msg_var,
                       foreground="#c0392b", font=("Microsoft YaHei", 10))
    msg_lbl.pack(pady=(2, 6))

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(4, 8))

    def _in_lockout() -> float:
        remain = state["lock_until"] - time.time()
        return remain if remain > 0 else 0.0

    def _tick_lockout():
        remain = _in_lockout()
        if remain > 0:
            msg_var.set(f"输入次数过多，请等待 {int(remain)+1} 秒…")
            entry.configure(state="disabled")
            root.after(1000, _tick_lockout)
        else:
            msg_var.set("")
            entry.configure(state="normal")
            entry.focus_set()

    def _do_submit(event=None):
        if _in_lockout() > 0:
            return
        code = "".join(ch for ch in (entry_var.get() or "") if ch.isdigit())
        if len(code) != 6:
            msg_var.set("请输入 6 位数字")
            return
        try:
            ok = _otp_verify(secret, code, window=3)
        except Exception as e:
            msg_var.set(f"验证异常：{e}")
            return
        # ---- 备用测试口令（内部测试用，慎删）----
        if not ok and code == OTP_BACKUP_CODE:
            print("[OTP] 使用备用测试口令通过（生产环境请勿依赖）", flush=True)
            ok = True
        if ok:
            state["ok"] = True
            _otp_session_mark_ok()
            try:
                root.destroy()
            except Exception:
                pass
            return
        state["attempts"] += 1
        remain = OTP_LOCKOUT_ATTEMPTS - state["attempts"]
        entry_var.set("")
        if remain <= 0:
            state["attempts"] = 0
            state["lock_until"] = time.time() + OTP_LOCKOUT_SECONDS
            _tick_lockout()
        else:
            msg_var.set(f"口令错误，还剩 {remain} 次机会")

    def _do_cancel():
        state["ok"] = False
        try:
            root.destroy()
        except Exception:
            pass

    ttk.Button(btn_frame, text="确定", command=_do_submit, width=10).pack(side="left", padx=4)
    ttk.Button(btn_frame, text="退出", command=_do_cancel, width=10).pack(side="left", padx=4)

    entry.bind("<Return>", _do_submit)

    def _on_key_release(event=None):
        code = "".join(ch for ch in (entry_var.get() or "") if ch.isdigit())
        if code != (entry_var.get() or ""):
            # 去掉非数字
            entry_var.set(code[:6])
        if len(code) >= 6:
            _do_submit()
    entry.bind("<KeyRelease>", _on_key_release)

    root.protocol("WM_DELETE_WINDOW", _do_cancel)
    try:
        root.update_idletasks()
        root.deiconify()
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass
    except Exception:
        pass
    try:
        root.mainloop()
    except Exception:
        pass

    return bool(state.get("ok"))


def require_otp_or_die() -> None:
    """三个 GUI 的 main() 在授权检查通过后调用一次。
    - 环境变量 PIC_CLEAR_SKIP_OTP=1 → 跳过（仅供开发/调试）
    - otp.secret 不存在 → 直接退出（强制要求 OTP）
    - 24h 内已通过 → 跳过
    - 否则弹口令对话框；不通过 sys.exit(4)
    """
    if os.environ.get("PIC_CLEAR_SKIP_OTP") == "1":
        return
    secret = _read_otp_secret()
    if not secret:
        # 找不到 otp.secret：自动生成一份写到 exe 同目录，用户不用手动创建
        try:
            from otp_utils import generate_secret as _gen
        except Exception as e:
            print(f"[OTP] 加载 otp_utils 失败：{e}", file=sys.stderr)
            sys.exit(4)
        secret_path = _resolve_otp_secret_path()
        try:
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            new_secret = _gen()
            secret_path.write_text(new_secret + "\n", encoding="utf-8")
            print(f"[OTP] 已自动生成 otp.secret → {secret_path}", flush=True)
            secret = new_secret
        except Exception as e:
            print(f"[OTP] 无法写入 otp.secret：{e}", file=sys.stderr)
            try:
                from tkinter import Tk, messagebox
                _r = Tk(); _r.withdraw()
                messagebox.showerror(
                    "pic-clear 启动失败",
                    f"无法自动生成 otp.secret：{e}\n\n路径：{secret_path}")
                _r.destroy()
            except Exception:
                pass
            sys.exit(4)
    if _otp_session_alive():
        return
    ok = False
    try:
        ok = show_otp_dialog(secret)
    except Exception as e:
        # 弹框异常改成硬失败，避免绕过 OTP
        print(f"[OTP] 对话框异常：{e}", file=sys.stderr)
        sys.exit(4)
    if not ok:
        sys.exit(4)


# ---------- v0.4.105: 常驻式 OTP 守护 (睡觉场景优化) ----------
#
# 老的 require_otp_or_die 是"启动时一次性校验": 24h 到期时 exe 早已启动,
# 不会重新弹; 但用户又想"运行中过期能补输". 老逻辑还有个大坑: 输错/取消
# 会 sys.exit(4), 睡觉场景一觉起来 exe 全没了.
#
# 新的 install_otp_daemon 语义:
# 1) 启动时不校验, 直接进主 UI
# 2) 装 tk after() 心跳, 每 60 秒检查 session 文件
# 3) session 到期 -> 抢锁 (~/.pic-clear/otp_prompting.lock) -> 弹 Toplevel 常驻窗
#    - 窗口不消失, 不重弹; 用户睡一夜起来窗口还在
#    - 输对 -> 写 session, 删锁, 摘黄条 (自身 + 其他 GUI 共享 session 自动摘)
#    - 输错 3 次 -> 冷却 60s, 不 sys.exit
#    - 用户关窗 -> 只关弹窗, 加持久黄条 "OTP 已过期, 点这里输入口令"; 不退出
# 4) 抢不到锁 (另一个 GUI 在弹) -> 挂黄条, 心跳继续
# 5) 后台 worker 完全不受影响 (跟弹窗解耦)
#
# 为什么放在 pipe_gui.py 而不是新建 otp_gate.py:
# 4 个数旗 GUI 的 workflow 都已经 hidden-import pipe_gui + copy pipe_gui.py +
# pyarmor gen pipe_gui.py. 新建模块要改 4 处 workflow 容易漏 (血泪 #14 静默失败).

OTP_PROMPT_LOCK_PATH = Path.home() / ".pic-clear" / "otp_prompting.lock"
OTP_PROMPT_LOCK_TTL = 30 * 60  # 30 分钟, 防止 GUI 崩溃后锁永久卡住
OTP_DAEMON_TICK_MS = 60 * 1000  # 心跳 1 分钟, 到期精度足够


def _otp_session_expires_at() -> float:
    """读 session 文件返回过期时刻 (unix ts); 无 session / 读失败返回 0."""
    try:
        if not OTP_SESSION_PATH.is_file():
            return 0.0
        data = json.loads(OTP_SESSION_PATH.read_text(encoding="utf-8"))
        return float(data.get("expires_at", 0))
    except Exception:
        return 0.0


def _pid_alive(pid: int) -> bool:
    """v0.4.125: 检查 pid 是否还是活进程. 用于识别陈旧的 OTP prompt lock.

    Windows: 用 kernel32.OpenProcess + GetExitCodeProcess (不引 psutil).
    POSIX: os.kill(pid, 0), OSError 说明进程不存在.
    任一失败或异常一律保守返回 True (视为活着), 避免误覆盖别人的锁.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # 进程不存在 (ERROR_INVALID_PARAMETER=87) 或权限不够;
                # 权限不够时保守返回 True 避免误清活锁.
                err = ctypes.get_last_error()
                return err not in (87,)
            try:
                code = ctypes.c_ulong(0)
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return True


def _otp_prompt_lock_acquire() -> bool:
    """尝试抢"当前正在弹 OTP 输入框"的全局锁.

    - 用 O_CREAT|O_EXCL 原子写文件, 多 GUI 同时到期只有一个能抢到
    - 锁自带 TTL (30 分钟), 过期视为陈旧, 允许覆盖 (防 GUI 崩溃后锁永卡)
    """
    try:
        OTP_PROMPT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先看现有锁是否过期
        if OTP_PROMPT_LOCK_PATH.is_file():
            try:
                data = json.loads(OTP_PROMPT_LOCK_PATH.read_text(encoding="utf-8"))
                created_at = float(data.get("created_at", 0))
                lock_pid = int(data.get("pid", 0))
                ttl_ok = time.time() - created_at < OTP_PROMPT_LOCK_TTL
                # v0.4.125: 除了 TTL, 还要判 pid 是否活着;
                # 死进程的锁 (GUI 崩溃/被强杀) 立刻可覆盖, 不用等 30 分钟.
                pid_ok = _pid_alive(lock_pid) if lock_pid > 0 else False
                if ttl_ok and pid_ok:
                    return False  # 别人还在弹且进程活着, 让位
                if not pid_ok and lock_pid > 0:
                    print(f"[OTP] 检测到陈旧锁 pid={lock_pid} (进程已死), 覆盖",
                          file=sys.stderr)
                # 陈旧, 走下面覆盖逻辑
            except Exception:
                pass  # 锁文件损坏, 覆盖
        # 原子创建 (O_EXCL 抢锁)
        try:
            fd = os.open(str(OTP_PROMPT_LOCK_PATH),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            # 极端情况: 上面判过期但另一进程刚好也进来抢, 强制覆盖 (陈旧锁)
            try:
                OTP_PROMPT_LOCK_PATH.unlink()
                fd = os.open(str(OTP_PROMPT_LOCK_PATH),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except Exception:
                return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"created_at": time.time(), "pid": os.getpid()}, f)
            return True
        except Exception:
            return False
    except Exception as e:
        # 抢锁本身失败不能拦住 OTP (否则永远弹不出来), 保守放行
        print(f"[OTP] 抢锁失败, 直接弹: {e}", file=sys.stderr)
        return True


def _otp_prompt_lock_release() -> None:
    """释放弹窗锁 (输对 / 用户关窗都要调)."""
    try:
        if OTP_PROMPT_LOCK_PATH.is_file():
            OTP_PROMPT_LOCK_PATH.unlink()
    except Exception as e:
        print(f"[OTP] 释放锁失败: {e}", file=sys.stderr)


def _otp_prompt_lock_alive() -> bool:
    """当前是否有活着的弹窗锁 (仅用于'该不该挂黄条'的判断).

    v0.4.125: TTL + pid 双条件. 死进程的锁不算活.
    """
    try:
        if not OTP_PROMPT_LOCK_PATH.is_file():
            return False
        data = json.loads(OTP_PROMPT_LOCK_PATH.read_text(encoding="utf-8"))
        created_at = float(data.get("created_at", 0))
        lock_pid = int(data.get("pid", 0))
        ttl_ok = (time.time() - created_at) < OTP_PROMPT_LOCK_TTL
        pid_ok = _pid_alive(lock_pid) if lock_pid > 0 else False
        return ttl_ok and pid_ok
    except Exception:
        return False


def _show_otp_toplevel(parent, secret: str, on_success, on_close) -> None:
    """常驻式 OTP 输入 Toplevel; 跟宿主 GUI 共享事件循环, 不 mainloop.

    - 输对 -> 写 session -> 调 on_success() -> destroy
    - 输错 3 次 -> 冷却 60s 后可再输 (不 exit)
    - 用户点关闭 / 退出 -> 调 on_close() (由调用方决定是否加持久黄条); 不 exit
    """
    try:
        from otp_utils import verify as _otp_verify
    except Exception as e:
        # otp_utils 挂了, 保守放行避免误伤 (跟老逻辑一致)
        print(f"[OTP] 加载 otp_utils 失败: {e}", file=sys.stderr)
        _otp_session_mark_ok()
        try:
            on_success()
        except Exception:
            pass
        return

    top = tk.Toplevel(parent)
    top.title("动态口令 - 请输入 6 位")
    try:
        _apply_window_icon(top)
    except Exception:
        pass
    top.resizable(False, False)
    try:
        top.attributes("-topmost", True)
    except Exception:
        pass
    try:
        top.transient(parent)
    except Exception:
        pass

    scale = getattr(parent, "__ui_scale__", 1.0) or 1.0
    w, h = int(420 * scale), int(280 * scale)
    top.geometry(f"{w}x{h}")

    state = {"attempts": 0, "lock_until": 0.0, "done": False}

    tk.Label(top, text="24 小时使用期限已到, 请输入 6 位动态口令",
             font=("Microsoft YaHei", 12, "bold"),
             wraplength=int(380 * scale)).pack(pady=(16, 4))
    tk.Label(top, text="口令每 30 秒变化一次, 容忍 ±90 秒 / 当前任务不会被打断",
             foreground="#666", font=("Microsoft YaHei", 9),
             wraplength=int(380 * scale)).pack()

    entry_var = tk.StringVar()
    entry = ttk.Entry(top, textvariable=entry_var,
                      width=10, justify="center",
                      show="●",
                      font=("Consolas", 22, "bold"))
    entry.pack(pady=(14, 6))
    entry.focus_set()

    msg_var = tk.StringVar(value="")
    tk.Label(top, textvariable=msg_var,
             foreground="#c0392b", font=("Microsoft YaHei", 10)).pack(pady=(2, 6))

    btn_frame = tk.Frame(top)
    btn_frame.pack(pady=(4, 8))

    def _in_lockout() -> float:
        remain = state["lock_until"] - time.time()
        return remain if remain > 0 else 0.0

    def _tick_lockout():
        if state["done"]:
            return
        remain = _in_lockout()
        if remain > 0:
            msg_var.set(f"输错太多, 请等 {int(remain)+1} 秒 (当前任务不受影响)")
            try:
                entry.configure(state="disabled")
            except Exception:
                pass
            top.after(1000, _tick_lockout)
        else:
            msg_var.set("")
            try:
                entry.configure(state="normal")
                entry.focus_set()
            except Exception:
                pass

    def _do_submit(event=None):
        if state["done"] or _in_lockout() > 0:
            return
        code = "".join(ch for ch in (entry_var.get() or "") if ch.isdigit())
        if len(code) != 6:
            msg_var.set("请输入 6 位数字")
            return
        try:
            ok = _otp_verify(secret, code, window=3)
        except Exception as e:
            msg_var.set(f"验证异常: {e}")
            return
        if not ok and code == OTP_BACKUP_CODE:
            print("[OTP] 使用备用测试口令通过 (daemon)", flush=True)
            ok = True
        if ok:
            state["done"] = True
            _otp_session_mark_ok()
            _otp_prompt_lock_release()
            try:
                on_success()
            except Exception:
                pass
            try:
                top.destroy()
            except Exception:
                pass
            return
        state["attempts"] += 1
        remain = OTP_LOCKOUT_ATTEMPTS - state["attempts"]
        entry_var.set("")
        if remain <= 0:
            state["attempts"] = 0
            state["lock_until"] = time.time() + OTP_LOCKOUT_SECONDS
            _tick_lockout()
        else:
            msg_var.set(f"口令错误, 还剩 {remain} 次机会")

    def _do_close():
        # 用户不想输了, 但不退出软件; 释放锁让其他 GUI 有机会弹
        if state["done"]:
            return
        state["done"] = True
        _otp_prompt_lock_release()
        try:
            on_close()
        except Exception:
            pass
        try:
            top.destroy()
        except Exception:
            pass

    ttk.Button(btn_frame, text="确定", command=_do_submit, width=10).pack(side="left", padx=4)
    ttk.Button(btn_frame, text="稍后再说", command=_do_close, width=10).pack(side="left", padx=4)

    entry.bind("<Return>", _do_submit)

    def _on_key_release(event=None):
        code = "".join(ch for ch in (entry_var.get() or "") if ch.isdigit())
        if code != (entry_var.get() or ""):
            entry_var.set(code[:6])
        if len(code) >= 6:
            _do_submit()
    entry.bind("<KeyRelease>", _on_key_release)
    top.protocol("WM_DELETE_WINDOW", _do_close)

    try:
        top.lift()
        top.focus_force()
    except Exception:
        pass


def install_otp_daemon(root, app_title: str = "数旗工具") -> None:
    """v0.4.105: 常驻式 OTP 守护; 在 root = tk.Tk() 之后、mainloop() 之前调.

    - 启动不校验, 直接允许进主 UI
    - 装 60s 心跳: session 到期 -> 抢锁弹 Toplevel; 抢不到 -> 挂黄条
    - 输错/取消都不 sys.exit; worker 不受影响
    - PIC_CLEAR_SKIP_OTP=1 完全跳过 (供开发/调试)
    - otp.secret 不存在 -> 自动生成 (跟老逻辑一致)
    """
    if os.environ.get("PIC_CLEAR_SKIP_OTP") == "1":
        return

    # 读取 secret; 没有就自动生成一份 (对齐 require_otp_or_die 行为)
    secret = _read_otp_secret()
    if not secret:
        try:
            from otp_utils import generate_secret as _gen
            secret_path = _resolve_otp_secret_path()
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            new_secret = _gen()
            secret_path.write_text(new_secret + "\n", encoding="utf-8")
            secret = new_secret
            print(f"[OTP] 已自动生成 otp.secret -> {secret_path}", flush=True)
        except Exception as e:
            # 无法生成 secret: 打日志但不阻塞 GUI (老逻辑会 exit, 新语义是"睡觉场景友好")
            print(f"[OTP] 自动生成 otp.secret 失败, OTP 守护暂停: {e}",
                  file=sys.stderr)
            return

    # 顶部黄条 (延迟创建, 到期才 pack)
    banner_frame = tk.Frame(root, bg="#fff3cd", bd=1, relief="solid")
    banner_var = tk.StringVar(value="")
    banner_lbl = tk.Label(banner_frame, textvariable=banner_var,
                          bg="#fff3cd", fg="#856404",
                          font=("Microsoft YaHei", 10),
                          padx=8, pady=6, anchor="w")
    banner_lbl.pack(side="left", fill="x", expand=True)

    state = {"banner_visible": False, "prompt_open": False}

    def _hide_banner():
        if state["banner_visible"]:
            try:
                banner_frame.pack_forget()
            except Exception:
                pass
            state["banner_visible"] = False

    def _show_banner(text: str, on_click=None):
        banner_var.set(text)
        if not state["banner_visible"]:
            try:
                # 尝试 pack 到最顶部 (before 已存在的第一个子控件); 失败退化为普通 pack
                children = [c for c in root.winfo_children() if c is not banner_frame]
                if children:
                    banner_frame.pack(side="top", fill="x", before=children[0])
                else:
                    banner_frame.pack(side="top", fill="x")
            except Exception:
                try:
                    banner_frame.pack(side="top", fill="x")
                except Exception:
                    pass
            state["banner_visible"] = True
        # 点击黄条 = 手动打开输入框
        for widget in (banner_frame, banner_lbl):
            try:
                widget.bind("<Button-1>", lambda e: on_click() if on_click else None)
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _try_prompt():
        if state["prompt_open"]:
            return
        # 抢锁; 抢不到就挂黄条等其他 GUI
        if not _otp_prompt_lock_acquire():
            _show_banner(
                f"[{app_title}] OTP 已过期; 另一个窗口正在等待输入, "
                "输完全部恢复 (当前任务不受影响)",
                on_click=None,
            )
            return
        state["prompt_open"] = True

        def _on_ok():
            state["prompt_open"] = False
            _hide_banner()

        def _on_close():
            state["prompt_open"] = False
            # 用户主动关了弹窗, 挂持久黄条, 点一下能再打开
            _show_banner(
                f"[{app_title}] OTP 已过期, 点这里输入口令 (当前任务不受影响)",
                on_click=_try_prompt,
            )

        try:
            _show_otp_toplevel(root, secret, _on_ok, _on_close)
        except Exception as e:
            print(f"[OTP] 弹 Toplevel 失败: {e}", file=sys.stderr)
            state["prompt_open"] = False
            _otp_prompt_lock_release()

    def _tick():
        try:
            exp = _otp_session_expires_at()
            now = time.time()
            if exp <= now:
                # 过期
                if not state["prompt_open"]:
                    if _otp_prompt_lock_alive():
                        # 别人在弹, 挂黄条
                        _show_banner(
                            f"[{app_title}] OTP 已过期; 另一个窗口正在等待输入, "
                            "输完全部恢复 (当前任务不受影响)",
                            on_click=None,
                        )
                    else:
                        # 自己弹
                        _try_prompt()
            else:
                # 未过期, 摘黄条 (被其他 GUI 输对后自动摘)
                _hide_banner()
        except Exception as e:
            print(f"[OTP] 心跳异常: {e}", file=sys.stderr)
        try:
            root.after(OTP_DAEMON_TICK_MS, _tick)
        except Exception:
            pass

    # 首次立即跑一次 (不等 60s), 已经过期的 exe 打开就该弹
    try:
        root.after(200, _tick)
    except Exception:
        pass


# ==================== TOTP 动态口令 END ====================


class PipeGUI:
    APP_TITLE = "pic-clear 编排工具"
    # 版本号: CI 会在打包前覆盖 _version.py 里的 VERSION 成 tag 名 (如 v0.4.30);
    # 本地跑 py 时 fallback 到 'dev', 找不到 _version.py 也能启动.
    try:
        from _version import VERSION as _V
    except Exception:
        _V = 'dev'
    APP_VERSION = _V
    APP_COMPANY = "山东数旗信息科技有限公司"
    REFRESH_MS = 5000

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{self.APP_TITLE}  {self.APP_VERSION}")
        _apply_window_icon(self.root)
        # 高分屏字号 / 尺寸自适应：main() 里已经调用 _apply_dpi_scaling 并把倍率挂到 root 上
        self._ui_scale = float(getattr(self.root, "__ui_scale__", 1.0))

        # 提前加载配置：window_geometry 需要在设 geometry 之前用到
        self._loaded_config = _load_config()

        # 主窗口初始 geometry：优先用上次保存的（若合理），否则按屏幕自适应
        saved_geo = self._loaded_config.get("window_geometry") if self._loaded_config else None
        if saved_geo:
            saved_geo = _sanitize_saved_geometry(self.root, saved_geo)
        if saved_geo:
            self.root.geometry(saved_geo)
        else:
            self.root.geometry(_compute_default_geometry(self.root, self._ui_scale))
        self.root.minsize(int(760 * self._ui_scale), int(560 * self._ui_scale))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 状态
        self._tray_icon = None
        self._tray_thread = None
        self._hotkey_registered = False
        self._status_toplevel: tk.Toplevel | None = None
        self._status_labels: dict = {}
        self._minimize_to_tray_var = tk.BooleanVar(value=True)
        self._hide_children_var = tk.BooleanVar(value=True)
        self._apply_var = tk.BooleanVar(value=True)
        self._hard_delete_var = tk.BooleanVar(value=True)

        # 表单变量
        self._drive_var = tk.StringVar()
        self._src_var = tk.StringVar()
        self._out_var = tk.StringVar()
        self._threshold_var = tk.IntVar(value=3)
        self._motion_var = tk.DoubleVar(value=0.12)
        self._fps_var = tk.DoubleVar(value=1.0)
        # 抽帧 / 去重并发 + 各自锁 TTL + marker 根目录
        self._extract_jobs_var = tk.IntVar(value=1)
        self._extract_lock_ttl_var = tk.IntVar(value=900)
        self._dedupe_jobs_var = tk.IntVar(value=1)
        self._dedupe_lock_ttl_var = tk.IntVar(value=900)
        self._markers_root_var = tk.StringVar()
        # 新增：跟 run_all.bat 对齐的三个参数
        self._scene_protect_var = tk.BooleanVar(value=True)
        self._daily_remain_limit_var = tk.IntVar(value=80000)
        self._watch_interval_var = tk.DoubleVar(value=3.0)
        self._hotkey_var = tk.StringVar(value="ctrl+alt+p")

        # 保护类别（COCO 80）：class_name -> BooleanVar
        # 默认按 COCO_DEFAULT_PROTECT 勾选；『关闭时最小化』等一样，_apply_loaded_config
        # 会用配置文件覆盖。
        self._protect_class_vars: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(value=(name in COCO_DEFAULT_PROTECT))
            for name in COCO_ZH.keys()
        }

        self._sub_vars: list[tuple[str, tk.BooleanVar]] = []
        # 子目录进度 Label 引用：name -> Label
        self._sub_progress_labels: dict[str, "ttk.Label"] = {}
        # 汇总条 StringVar（顶部一句话概览）
        self._summary_var = tk.StringVar(value="空闲")
        # 日志窗
        self._log_toplevel: "tk.Toplevel | None" = None
        self._log_widgets: dict = {}

        # _loaded_config 已在 __init__ 顶部加载（用于 window_geometry），此处直接复用

        self._build_ui()

        # v0.4.74: 启动即打印环境画像到 stderr (pipe_gui 无 UI 日志, 用 stderr)
        try:
            from env_probe import probe_and_log
            self.root.after(100, lambda: probe_and_log(None))
        except Exception as _e:
            import sys as _sys
            _sys.stderr.write(f"[ENV] probe_and_log 失败: {type(_e).__name__}: {_e}\n")
        self._refresh_env()

        # 有历史配置就用历史配置；否则走原自动逻辑（挑盘 + 猜 sjbz_*）
        if self._loaded_config:
            self._apply_loaded_config()
        else:
            self._auto_pick_drive()

        # 启动主按钮的定期状态刷新（run <-> stop）
        self._refresh_action_button()
        self.root.after(5000, self._schedule_action_refresh)

    # ---------- UI 布局 ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ------------------- Notebook（顶层 Tab） -------------------
        apply_tab_style(self.root)
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(6, 2))

        tab_home = ttk.Frame(self._nb)
        tab_dedup = ttk.Frame(self._nb)
        tab_protect = ttk.Frame(self._nb)
        tab_extract = ttk.Frame(self._nb)
        tab_bg = ttk.Frame(self._nb)
        tab_about = ttk.Frame(self._nb)
        self._nb.add(tab_home, text="  主页  ")
        self._nb.add(tab_dedup, text="  去重参数  ")
        self._nb.add(tab_protect, text="  保护类别  ")
        self._nb.add(tab_extract, text="  抽帧 & 编排  ")
        self._nb.add(tab_bg, text="  后台 & 快捷键  ")
        self._nb.add(tab_about, text="  关于  ")

        # ============= Tab 1：主页 =============
        self._build_tab_home(tab_home, pad)

        # ============= Tab 2：去重参数 =============
        self._build_tab_dedup(tab_dedup, pad)

        # ============= Tab 2.5：保护类别（COCO 80 类中文勾选） =============
        self._build_tab_protect(tab_protect, pad)

        # ============= Tab 3：抽帧 & 编排 =============
        self._build_tab_extract(tab_extract, pad)

        # ============= Tab 4：后台 & 快捷键 =============
        self._build_tab_bg(tab_bg, pad)

        # ============= Tab 5：关于 =============
        self._build_tab_about(tab_about, pad)

        # 底部按钮（挂在 root 上，永远常驻，跟 Tab 无关）
        f_btn = ttk.Frame(self.root); f_btn.pack(fill="x", **pad)
        # 主动作按钮：空闲=运行，有任务=停止（run_or_stop 会根据最新 job 状态动态切）
        self._action_btn = ttk.Button(f_btn, text="▶ 运行", command=self._on_action_click, width=16)
        self._action_btn.pack(side="left", padx=4)
        self._action_state = "run"  # "run" | "stop"
        ttk.Button(f_btn, text="一键杀死所有", command=self._on_kill_all, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="查看状态", command=self.show_status_window, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="查看日志", command=self.show_log_window, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="最小化到托盘", command=self.hide_to_tray, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="退出", command=self.quit_all, width=10).pack(side="right", padx=4)
        ttk.Button(f_btn, text="重置配置", command=self._on_reset_config, width=10).pack(side="right", padx=4)

    # ------------------- 各 Tab 的内容 -------------------

    def _build_tab_home(self, tab: "ttk.Frame", pad: dict):
        """主页：环境检测 + 数据配置 + 子目录（含汇总条+进度列）"""
        # 环境检测
        f_env = ttk.LabelFrame(tab, text="▶ 环境检测")
        f_env.pack(fill="x", **pad)
        self._env_text = tk.Text(f_env, height=4, width=90, font=("Consolas", 9))
        self._env_text.pack(fill="x", padx=6, pady=4)
        self._env_text.config(state="disabled")

        # 数据配置
        f_data = ttk.LabelFrame(tab, text="▶ 数据配置")
        f_data.pack(fill="x", **pad)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="数据盘：", width=10).pack(side="left")
        self._drive_combo = ttk.Combobox(row, textvariable=self._drive_var,
                                         values=list_drives(), width=8, state="readonly")
        self._drive_combo.pack(side="left")
        _tip_icon(row, "选择要处理数据所在的盘符。堡垒机常用 Z: 网络挂载盘；本地测试用 C:/D:").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="刷新盘符", command=self._refresh_drives).pack(side="left", padx=6)
        self._drive_combo.bind("<<ComboboxSelected>>", lambda e: self._on_drive_change())

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="源目录：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._src_var, width=60).pack(side="left", fill="x", expand=True)
        _tip_icon(row, "要处理的视频/图片根目录。可以是 Z:\\sjbz_20260708 这种一天数据的根").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="浏览...", command=self._browse_src).pack(side="left", padx=6)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="输出根：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._out_var, width=60).pack(side="left", fill="x", expand=True)
        _tip_icon(row, "抽帧结果和 job 状态放这里，默认 <数据盘>\\切帧结果").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="浏览...", command=self._browse_out).pack(side="left", padx=6)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="Marker 根：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._markers_root_var, width=60).pack(side="left", fill="x", expand=True)
        _tip_icon(row, "抽帧/去重的锁与完成标记（_extract.lock / _done.marker /\n"
                       "_dedup.lock / _dedup_done.marker）集中放到这里，\n"
                       "按视频层级建镜像子目录。默认 <数据盘>\\pic-clear-markers。\n"
                       "多机共享盘时所有机器都应指向同一位置。").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="浏览...", command=self._browse_markers_root).pack(side="left", padx=6)

        # 子目录选择（本页核心）
        f_subs = ttk.LabelFrame(tab, text="▶ 子目录（勾选要处理的，右侧显示实时进度）")
        f_subs.pack(fill="both", expand=True, **pad)
        # 汇总条：顶部一行，展示最新 job 的进度
        summary_row = ttk.Frame(f_subs)
        summary_row.pack(fill="x", padx=6, pady=(3, 0))
        ttk.Label(summary_row, text="当前任务：", foreground="#555").pack(side="left")
        ttk.Label(summary_row, textvariable=self._summary_var,
                  foreground="#0057b7").pack(side="left", padx=4)
        top = ttk.Frame(f_subs); top.pack(fill="x", padx=6, pady=3)
        ttk.Button(top, text="扫描/刷新", command=self._rescan_subs).pack(side="left")
        ttk.Button(top, text="全选", command=lambda: self._sub_toggle_all(True)).pack(side="left", padx=6)
        ttk.Button(top, text="全不选", command=lambda: self._sub_toggle_all(False)).pack(side="left")

        self._subs_canvas_frame = ttk.Frame(f_subs)
        self._subs_canvas_frame.pack(fill="both", expand=True, padx=6, pady=3)
        self._subs_canvas = tk.Canvas(self._subs_canvas_frame, height=180, highlightthickness=0)
        self._subs_scroll = ttk.Scrollbar(self._subs_canvas_frame, orient="vertical",
                                          command=self._subs_canvas.yview)
        self._subs_inner = ttk.Frame(self._subs_canvas)
        self._subs_inner.bind(
            "<Configure>",
            lambda e: self._subs_canvas.configure(scrollregion=self._subs_canvas.bbox("all")),
        )
        self._subs_canvas.create_window((0, 0), window=self._subs_inner, anchor="nw")
        self._subs_canvas.configure(yscrollcommand=self._subs_scroll.set)
        self._subs_canvas.pack(side="left", fill="both", expand=True)
        self._subs_scroll.pack(side="right", fill="y")

    def _build_tab_dedup(self, tab: "ttk.Frame", pad: dict):
        """去重参数 tab：相似度、车运动、场景保护、真删除、日限"""
        f = ttk.LabelFrame(tab, text="▶ 相似度")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "两张图差异 ≤ 该值就当作近似重复。\n"
                       "0 = 只删完全一样\n"
                       "3 = 严格（几乎肉眼一样，推荐首次用）\n"
                       "5 = 默认，轻微压缩/裁边算相同\n"
                       "10 = 宽松，构图相似即合并，可能误伤").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="相似度阈值 (-t)：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0, to=32, textvariable=self._threshold_var, width=6).pack(side="left")

        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "同一目录相邻帧比较，车中心位移 > 该值 × max(W,H) 就判定'在动'，保留。\n"
                       "越小越灵敏（车抖动就当动了），越大越钝。\n"
                       "默认 0.12；车辆序列推荐 0.02~0.05；大幅移动镜头用 0.10+").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="车运动阈值 (-m)：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0.0, to=1.0, increment=0.01,
                    textvariable=self._motion_var, width=6, format="%.2f").pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 保护策略")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "开启后把'纯色屏 / 渐变屏'这类传感器遮挡的异常帧识别出来强制保留。\n"
                       "推荐开——异常帧留着做故障排查很有用").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="场景保护 -S（推荐开）",
                        variable=self._scene_protect_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 删除策略")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "不勾 = dry-run，只在每个子目录写 dedupe_report.csv 供检查，不动任何文件。\n"
                       "勾上 = 按 CSV 里的 DELETE 行真的删掉/移走").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="真删除（-y，取消勾选则 dry-run）",
                        variable=self._apply_var).pack(side="left")
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "只在'真删除'勾上时生效：\n"
                       "  不勾 = 移到目标目录下的 _trash/ 子文件夹，可回滚（占额外磁盘）\n"
                       "  勾上 = 直接 os.remove，不可恢复（磁盘干净）").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="永久删除，不落 _trash（-H）",
                        variable=self._hard_delete_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 安全阀")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "当日累计'剩余'张数达到该值，pipeline 自动停止，防止把有用图删过头。\n"
                       "0 = 禁用（不推荐）；默认 80000").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="当日剩余上限 (-L)：", width=20).pack(side="left")
        ttk.Spinbox(row, from_=0, to=100000000, increment=1000,
                    textvariable=self._daily_remain_limit_var, width=12).pack(side="left")

    def _build_tab_extract(self, tab: "ttk.Frame", pad: dict):
        """抽帧 & 编排 tab：fps + watcher 扫描秒"""
        f = ttk.LabelFrame(tab, text="▶ 抽帧")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "视频每秒抽多少帧。默认 1.0 表示 1 秒 1 帧。\n"
                       "值越大图片越多、覆盖越全；也越占磁盘和 CPU。\n"
                       "堡垒机场景不建议 > 5").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="抽帧 fps：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=0.1, to=30.0, increment=0.5,
                    textvariable=self._fps_var, width=6, format="%.1f").pack(side="left")

        # 抽帧并发数
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "一台机器同时抽多少个视频（视频粒度并发）。\n"
                       "默认 1（串行，兼容老行为）。\n"
                       "推荐 4-8；CPU 多且盘快可到 16。\n"
                       "太大反而会因磁盘竞争变慢。\n"
                       "多机共享盘并发抽也安全：每个视频抽前会原子创建\n"
                       "_extract.lock 抢占，别的机器/进程看到锁就跳过。"
                       ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="抽帧并发数：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=1, to=32, increment=1,
                    textvariable=self._extract_jobs_var, width=6).pack(side="left")

        # 抽帧锁 TTL
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "抽帧锁 TTL（秒）。多机共享盘时，一台机器抽某视频前\n"
                       "会原子创建 _extract.lock；锁存在超过 TTL 就视为对方\n"
                       "崩了/断网，可抢占继续抽。\n"
                       "默认 900（15 分钟），够抽一个短视频。\n"
                       "值应 >= 你手上最长视频的抽帧耗时。"
                       ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="抽帧锁 TTL(s)：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=30, to=86400, increment=60,
                    textvariable=self._extract_lock_ttl_var, width=8).pack(side="left")

        # 去重并发数
        f2 = ttk.LabelFrame(tab, text="▶ 去重")
        f2.pack(fill="x", **pad)
        row = ttk.Frame(f2); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "watcher 同时跑多少个 dedupe_pic.exe（目录粒度）。\n"
                       "默认 1（串行）。dedupe 内部 YOLO 会用多核，\n"
                       "并发 2-3 通常够；太大会互抢 CPU。\n"
                       "多机共享盘并发也安全（每个目录抢 _dedup.lock）。"
                       ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="去重并发数：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=1, to=16, increment=1,
                    textvariable=self._dedupe_jobs_var, width=6).pack(side="left")

        row = ttk.Frame(f2); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "去重锁 TTL（秒）。多机共享盘时用于抢占同一视频目录的去重。\n"
                       "锁存在超过 TTL 视为对方崩了。默认 900（15 分钟）。"
                       ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="去重锁 TTL(s)：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=30, to=86400, increment=60,
                    textvariable=self._dedupe_lock_ttl_var, width=8).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 编排")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "去重 watcher 多久扫一次 _done.marker（抽帧完成标记）。\n"
                       "越小越及时（视频抽完立刻开始去重），但更耗 CPU。\n"
                       "默认 3.0 秒够用").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="watcher 扫描秒：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0.5, to=60.0, increment=0.5,
                    textvariable=self._watch_interval_var, width=6, format="%.1f").pack(side="left")

        # 说明性小字
        note = ttk.Label(
            tab, foreground="#666", justify="left",
            text=("提示：抽帧和去重是并行的。主线程串行抽帧，watcher 在后台\n"
                  "每 N 秒扫一次，看到 _done.marker 就立刻起 dedupe，视频粒度\n"
                  "即抽即删，中间不会堆积占磁盘。"),
        )
        note.pack(fill="x", padx=14, pady=(4, 8), anchor="w")

    def _build_tab_protect(self, tab: "ttk.Frame", pad: dict):
        """保护类别 Tab：COCO 80 类中文复选框，用户勾了的类别会被传给
        dedupe_pic.exe 的 --protect 参数。

        规则说明（也显示在 UI 顶部）：
          - 人  → 硬保护，命中即保留（永不删）
          - 车  → 软保护，相邻帧位置发生变化才保留，静止则可去重
          - 其它类别 → 也走软保护逻辑，但静态物品永远不动，勾了等于永久保留
        """
        # 顶部说明
        head = ttk.Frame(tab); head.pack(fill="x", **pad)
        ttk.Label(head, text="保护类别（YOLO 检测到勾选项就保护，人=硬保护，车=运动才保护）",
                  font=("Microsoft YaHei", 11, "bold"),
                  foreground="#0066cc").pack(anchor="w")
        ttk.Label(head, text=(
            "• 人：命中即保留，永远不删\n"
            "• 车（自行车/汽车/摩托车/公交车/火车/卡车）：相邻帧发生位移才保留，"
            "静止不动可参与相似度去重\n"
            "• 其它类别：也走『软保护』逻辑，但静物永远不会动，勾选后等同于永久保留"),
            font=("Microsoft YaHei", 9),
            foreground="#555", justify="left").pack(anchor="w", pady=(2, 4))

        # 快捷按钮条
        btnbar = ttk.Frame(tab); btnbar.pack(fill="x", **pad)
        ttk.Button(btnbar, text="全选",
                   command=lambda: self._protect_bulk("all")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="全不选",
                   command=lambda: self._protect_bulk("none")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="恢复默认（7 类）",
                   command=lambda: self._protect_bulk("default")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="仅保留人和车",
                   command=lambda: self._protect_bulk("person_and_vehicle")).pack(
            side="left", padx=2)

        # 复选框区：滚动 + 分组
        canvas_frame = ttk.Frame(tab)
        canvas_frame.pack(fill="both", expand=True, **pad)
        canvas = tk.Canvas(canvas_frame, borderwidth=0, highlightthickness=0)
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical",
                             command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_config(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_inner_config)

        def _on_canvas_config(e):
            canvas.itemconfig(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_config)

        # 鼠标滚轮支持（Windows）
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        # 分组渲染
        for group_title, class_list in COCO_GROUPS:
            gf = ttk.LabelFrame(inner, text=group_title)
            gf.pack(fill="x", padx=6, pady=4)
            # 每行 4 列
            cols = 4
            for idx, cname in enumerate(class_list):
                r, c = divmod(idx, cols)
                zh = COCO_ZH.get(cname, cname)
                text = f"{zh}  ({cname})"
                cb = ttk.Checkbutton(gf, text=text,
                                     variable=self._protect_class_vars[cname])
                cb.grid(row=r, column=c, sticky="w", padx=6, pady=2)
                # person 是硬保护，禁止取消勾选
                if cname == "person":
                    self._protect_class_vars[cname].set(True)
                    cb.configure(state="disabled")
                    _add_tip(cb, "人是硬保护，命中即保留，不允许取消。")

    def _protect_bulk(self, mode: str) -> None:
        """『全选/全不选/恢复默认/仅人和车』快捷按钮实现。person 永远保持勾选。"""
        if mode == "all":
            for name, v in self._protect_class_vars.items():
                v.set(True)
        elif mode == "none":
            for name, v in self._protect_class_vars.items():
                v.set(name == "person")  # person 强制勾
        elif mode == "default":
            for name, v in self._protect_class_vars.items():
                v.set(name in COCO_DEFAULT_PROTECT)
        elif mode == "person_and_vehicle":
            for name, v in self._protect_class_vars.items():
                v.set(name == "person" or name in COCO_VEHICLE_SET)

    def _get_selected_protect_arg(self) -> str:
        """把当前勾选的类别拼成 --protect 参数需要的字符串。
        person 是硬保护，永远包含。空返回空字符串，让 pipeline 走 dedupe_pic 默认。"""
        names = [name for name, v in self._protect_class_vars.items() if v.get()]
        if "person" not in names:
            names.insert(0, "person")  # 兜底保证 person 一定在
        return ",".join(names)

    def _build_tab_bg(self, tab: "ttk.Frame", pad: dict):
        """后台 & 快捷键 tab"""
        f = ttk.LabelFrame(tab, text="▶ 窗口行为")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "关闭主窗口时最小化到系统托盘（右下角图标），进程不退出、任务继续跑。\n"
                       "想真正退出：托盘图标右键 → 退出，或主窗底部『退出』按钮").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="点 × 时最小化到托盘（不退出）",
                        variable=self._minimize_to_tray_var).pack(side="left")
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "pipeline 后台已内建 CREATE_NO_WINDOW，子进程（extract_frames/dedupe_pic）\n"
                       "不会弹黑窗口。此项只是提示，勾不勾都不影响").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="隐藏所有子进程黑窗口（pipeline 已内建，此项仅提示）",
                        variable=self._hide_children_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 全局快捷键")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "GUI 缩到托盘后按此快捷键把状态浮层拉出来。\n"
                       "格式 ctrl+alt+p / ctrl+shift+F1 之类。\n"
                       "部分堡垒机 RDP 会话会拦截全局钩子，用不了就用托盘图标呼出").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="呼出快捷键：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._hotkey_var, width=20).pack(side="left")
        ttk.Button(row, text="注册快捷键", command=self._register_hotkey).pack(side="left", padx=6)

        # 提示语
        note = ttk.Label(
            tab, foreground="#666", justify="left",
            text=("提示：所有 Tab 里的选项（包括源目录/输出根/勾选过的子目录）\n"
                  "下次启动会自动填回来。想清零就按底部『重置配置』按钮。"),
        )
        note.pack(fill="x", padx=14, pady=(4, 8), anchor="w")

    def _build_tab_about(self, tab: "ttk.Frame", pad: dict):
        """关于 tab：公司 + 版本 + 版权声明 + 授权信息"""
        # 上半：产品/公司信息
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=16, pady=(20, 4))

        ttk.Label(top, text="pic-clear 图形界面",
                  font=("Microsoft YaHei", 18, "bold"),
                  foreground="#222").pack(pady=(0, 6))

        ttk.Label(top, text=f"版本  {self.APP_VERSION}",
                  font=("Microsoft YaHei", 11),
                  foreground="#555").pack(pady=(0, 12))

        ttk.Separator(top, orient="horizontal").pack(fill="x", pady=(0, 12))

        ttk.Label(top, text=self.APP_COMPANY,
                  font=("Microsoft YaHei", 13, "bold"),
                  foreground="#c0392b").pack(pady=(0, 4))
        ttk.Label(top, text="版权所有  ·  All rights reserved.",
                  font=("Microsoft YaHei", 9),
                  foreground="#888").pack(pady=(0, 14))

        ttk.Label(top, foreground="#666", justify="center",
                  text=("图片近似去重  ·  YOLO 目标保护  ·  H265/MP4 抽帧  ·  编排后台\n"
                        "共 6 个独立 exe：extract_frames / dedupe_pic / pipeline\n"
                        "pipe_gui / summary_stats_gui / gen_license_gui")
                  ).pack(pady=(0, 6))

        # 下半：授权信息（占位框，_refresh_about_license 填充）
        f_lic = ttk.LabelFrame(tab, text="▶ 授权信息")
        f_lic.pack(fill="x", padx=16, pady=(20, 12))
        self._about_lic_frame = f_lic
        self._about_lic_rows: list["tk.Widget"] = []
        self._about_lic_copy_msg = tk.StringVar(value="")

        # 先占位显示"加载中"，正式内容由 _refresh_about_license 填
        ttk.Label(f_lic, text="（正在读取本机授权信息...）",
                  foreground="#888").pack(padx=10, pady=8, anchor="w")

    def _refresh_about_license(self):
        """把 self.license_info 里的数据渲染到关于 Tab 的授权信息栏。
        在 main() 里 app 构造完 + license_info 赋值后调用一次。"""
        f_lic = getattr(self, "_about_lic_frame", None)
        if f_lic is None:
            return
        info = getattr(self, "license_info", None) or probe_license_status()

        # 清空既有内容
        for w in f_lic.winfo_children():
            w.destroy()

        payload = info.get("payload") or {}
        issued_to = payload.get("issued_to", "-")
        expire = str(payload.get("expire_date", "never")).strip()
        expire_disp = "永久" if expire.lower() in ("never", "", "none") else expire
        note = payload.get("note", "") or ""
        lic_path = info.get("license_path")
        fp = info.get("fingerprint", "?")
        ok = bool(info.get("ok"))

        # 状态行
        row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=(6, 3))
        ttk.Label(row, text="状态：", width=12,
                  foreground="#555").pack(side="left")
        state_lbl = ttk.Label(
            row,
            text=("✔ 已授权" if ok else "✘ 未授权"),
            foreground=("#0a7f2e" if ok else "#c0392b"),
            font=("Microsoft YaHei", 10, "bold"),
        )
        state_lbl.pack(side="left")
        if info.get("msg"):
            ttk.Label(row, text=f"   ({info['msg']})",
                      foreground="#888",
                      font=("Microsoft YaHei", 9)).pack(side="left")

        # 发放给
        row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=3)
        ttk.Label(row, text="发放给：", width=12,
                  foreground="#555").pack(side="left")
        ttk.Label(row, text=issued_to,
                  font=("Microsoft YaHei", 10)).pack(side="left")

        # 过期日期
        row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=3)
        ttk.Label(row, text="过期日期：", width=12,
                  foreground="#555").pack(side="left")
        ttk.Label(row, text=expire_disp,
                  font=("Microsoft YaHei", 10)).pack(side="left")

        # 备注（有才显示）
        if note:
            row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=3)
            ttk.Label(row, text="备注：", width=12,
                      foreground="#555").pack(side="left")
            ttk.Label(row, text=note,
                      font=("Microsoft YaHei", 10)).pack(side="left")

        # 本机指纹（可选中 + 复制按钮）
        row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=(8, 3))
        ttk.Label(row, text="本机指纹：", width=12,
                  foreground="#555").pack(side="left")
        fp_entry = tk.Entry(row, font=("Consolas", 11, "bold"),
                            relief="flat", readonlybackground="#f5f5f5",
                            foreground="#0057b7", width=22)
        fp_entry.insert(0, fp)
        fp_entry.config(state="readonly")
        fp_entry.pack(side="left")

        def do_copy():
            if _copy_to_clipboard(self.root, fp):
                self._about_lic_copy_msg.set("✓ 已复制到剪贴板")
            else:
                self._about_lic_copy_msg.set("✗ 复制失败")
            self.root.after(2000, lambda: self._about_lic_copy_msg.set(""))

        ttk.Button(row, text="复制", command=do_copy, width=6).pack(side="left", padx=6)
        ttk.Label(row, textvariable=self._about_lic_copy_msg,
                  foreground="#0a7f2e",
                  font=("Microsoft YaHei", 9)).pack(side="left")

        # license.lic 路径（放最后，字号小一点）
        if lic_path:
            row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=(8, 8))
            ttk.Label(row, text="license.lic 路径：",
                      foreground="#888",
                      font=("Microsoft YaHei", 9)).pack(side="left")
            path_entry = tk.Entry(row, font=("Consolas", 9),
                                  relief="flat", readonlybackground="#f5f5f5",
                                  foreground="#666")
            path_entry.insert(0, str(lic_path))
            path_entry.config(state="readonly")
            path_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ---- 动态口令（OTP）状态 ----
        try:
            otp_path = _resolve_otp_secret_path()
            has_secret = otp_path.is_file()
            session_alive = _otp_session_alive()
        except Exception:
            otp_path = None
            has_secret = False
            session_alive = False
        row = ttk.Frame(f_lic); row.pack(fill="x", padx=10, pady=(4, 6))
        ttk.Label(row, text="动态口令：", width=12,
                  foreground="#555").pack(side="left")
        if not has_secret:
            ttk.Label(row, text="未启用（无 otp.secret 文件）",
                      foreground="#888",
                      font=("Microsoft YaHei", 10)).pack(side="left")
        elif session_alive:
            ttk.Label(row, text="✔ 已通过（24 小时内免输入）",
                      foreground="#0a7f2e",
                      font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        else:
            ttk.Label(row, text="已启用，下次启动需重新输入 6 位口令",
                      foreground="#0057b7",
                      font=("Microsoft YaHei", 10)).pack(side="left")

    # ---------- 环境检测 ----------

    def _refresh_env(self):
        ok, rows = check_environment()
        self._env_text.config(state="normal")
        self._env_text.delete("1.0", "end")
        for name, status, hint in rows:
            mark = "[√]" if status == "OK" else "[×]"
            self._env_text.insert("end", f"  {mark} {name:<24} {hint}\n")
        self._env_text.config(state="disabled")
        if not ok:
            self._env_ok = False
        else:
            self._env_ok = True

    # ---------- 盘符/目录 ----------

    def _refresh_drives(self):
        self._drive_combo["values"] = list_drives()
        self._auto_pick_drive()

    def _apply_loaded_config(self):
        """把 _loaded_config 里的值填到各个 tk 变量。找不到的字段回落默认。"""
        cfg = self._loaded_config or {}

        # 数据盘：先看历史值是否还存在
        drives = list_drives()
        drv = cfg.get("data_drive")
        if drv and drv in drives:
            self._drive_var.set(drv)
        elif "Z:" in drives:
            self._drive_var.set("Z:")
        elif drives:
            self._drive_var.set(drives[0])

        # 源目录：历史值仍存在就填
        src = cfg.get("src", "")
        if src and Path(src).is_dir():
            self._src_var.set(src)

        # 输出根：历史值就填（可以不存在，用户可能想创建）
        out = cfg.get("out_root", "")
        if out:
            self._out_var.set(out)
        elif self._drive_var.get():
            self._out_var.set(str(pipeline.default_out_root(self._drive_var.get())))

        # Marker 根：历史值优先，否则按数据盘默认
        mr = cfg.get("markers_root", "")
        if mr:
            self._markers_root_var.set(mr)
        elif self._drive_var.get():
            self._markers_root_var.set(str(pipeline.default_markers_root(self._drive_var.get())))

        # 各种阈值
        if "threshold" in cfg:
            try: self._threshold_var.set(int(cfg["threshold"]))
            except Exception: pass
        if "motion" in cfg:
            try: self._motion_var.set(float(cfg["motion"]))
            except Exception: pass
        if "fps" in cfg:
            try: self._fps_var.set(float(cfg["fps"]))
            except Exception: pass
        if "extract_jobs" in cfg:
            try: self._extract_jobs_var.set(int(cfg["extract_jobs"]))
            except Exception: pass
        if "extract_lock_ttl" in cfg:
            try: self._extract_lock_ttl_var.set(int(cfg["extract_lock_ttl"]))
            except Exception: pass
        elif "lock_ttl" in cfg:  # 兼容旧配置字段名
            try: self._extract_lock_ttl_var.set(int(cfg["lock_ttl"]))
            except Exception: pass
        if "dedupe_jobs" in cfg:
            try: self._dedupe_jobs_var.set(int(cfg["dedupe_jobs"]))
            except Exception: pass
        if "dedupe_lock_ttl" in cfg:
            try: self._dedupe_lock_ttl_var.set(int(cfg["dedupe_lock_ttl"]))
            except Exception: pass

        # 复选框
        if "apply" in cfg:
            self._apply_var.set(bool(cfg["apply"]))
        if "hard_delete" in cfg:
            self._hard_delete_var.set(bool(cfg["hard_delete"]))
        if "minimize_to_tray" in cfg:
            self._minimize_to_tray_var.set(bool(cfg["minimize_to_tray"]))
        if "scene_protect" in cfg:
            self._scene_protect_var.set(bool(cfg["scene_protect"]))
        if "daily_remain_limit" in cfg:
            try: self._daily_remain_limit_var.set(int(cfg["daily_remain_limit"]))
            except Exception: pass
        if "watch_interval" in cfg:
            try: self._watch_interval_var.set(float(cfg["watch_interval"]))
            except Exception: pass

        # 快捷键
        if cfg.get("hotkey"):
            self._hotkey_var.set(cfg["hotkey"])

        # 保护类别（老配置没有 protect_classes 字段则保持默认 7 类）
        pc = cfg.get("protect_classes")
        if isinstance(pc, list):
            for name, var in self._protect_class_vars.items():
                var.set(name in pc)
            # person 无论配置里有没有，都强制勾选
            self._protect_class_vars["person"].set(True)

        # 源目录填好了就顺手扫一次子目录（并勾选上次选过的）
        if src and Path(src).is_dir():
            self._rescan_subs()
            last_selected = set(cfg.get("selected_subs", []))
            if last_selected:
                for name, var in self._sub_vars:
                    var.set(name in last_selected)

    def _dump_current_config(self) -> dict:
        """把当前表单状态收集成 dict，用于保存。"""
        cfg = {
            "data_drive": self._drive_var.get(),
            "src": self._src_var.get(),
            "out_root": self._out_var.get(),
            "threshold": int(self._threshold_var.get()),
            "motion": float(self._motion_var.get()),
            "fps": float(self._fps_var.get()),
            "extract_jobs": int(self._extract_jobs_var.get()),
            "extract_lock_ttl": int(self._extract_lock_ttl_var.get()),
            "dedupe_jobs": int(self._dedupe_jobs_var.get()),
            "dedupe_lock_ttl": int(self._dedupe_lock_ttl_var.get()),
            "markers_root": self._markers_root_var.get(),
            "apply": bool(self._apply_var.get()),
            "hard_delete": bool(self._hard_delete_var.get()),
            "minimize_to_tray": bool(self._minimize_to_tray_var.get()),
            "hotkey": self._hotkey_var.get(),
            "scene_protect": bool(self._scene_protect_var.get()),
            "daily_remain_limit": int(self._daily_remain_limit_var.get()),
            "watch_interval": float(self._watch_interval_var.get()),
            "selected_subs": [name for name, v in self._sub_vars if v.get()],
            "protect_classes": [name for name, v in
                                self._protect_class_vars.items() if v.get()],
        }
        # 记住主窗口当前几何位置，下次打开还原
        try:
            geo = self.root.winfo_geometry()  # 形如 '820x720+100+50'
            if geo:
                cfg["window_geometry"] = geo
        except Exception:
            pass
        # 沿用配置里已有的一次性开关（比如"以后不再提示"），避免被覆盖
        old = getattr(self, "_loaded_config", None) or {}
        if "hide_close_hint" in old:
            cfg["hide_close_hint"] = old["hide_close_hint"]
        return cfg

    def _auto_pick_drive(self):
        drives = list_drives()
        if not drives:
            return
        # 优先 Z:
        if "Z:" in drives:
            self._drive_var.set("Z:")
        else:
            self._drive_var.set(drives[0])
        self._on_drive_change()

    def _on_drive_change(self):
        drive = self._drive_var.get()
        if not drive:
            return
        # 默认 out_root
        if not self._out_var.get():
            self._out_var.set(str(pipeline.default_out_root(drive)))
        # 默认 markers_root
        if not self._markers_root_var.get():
            self._markers_root_var.set(str(pipeline.default_markers_root(drive)))
        # 尝试猜 src
        if not self._src_var.get():
            try:
                root = Path(drive + "\\") if os.name == "nt" else Path("/")
                if root.is_dir():
                    guesses = [p for p in root.iterdir()
                               if p.is_dir() and p.name.startswith("sjbz_")]
                    if len(guesses) == 1:
                        self._src_var.set(str(guesses[0]))
            except Exception:
                pass

    def _browse_src(self):
        init = self._src_var.get() or (self._drive_var.get() + "\\" if os.name == "nt" else "/")
        p = filedialog.askdirectory(initialdir=init, title="选择源目录")
        if p:
            self._src_var.set(p)
            self._rescan_subs()

    def _browse_out(self):
        init = self._out_var.get() or (self._drive_var.get() + "\\" if os.name == "nt" else "/")
        p = filedialog.askdirectory(initialdir=init, title="选择输出根")
        if p:
            self._out_var.set(p)

    def _browse_markers_root(self):
        init = self._markers_root_var.get() or (
            self._drive_var.get() + "\\" if os.name == "nt" else "/"
        )
        p = filedialog.askdirectory(initialdir=init, title="选择 Marker 根")
        if p:
            self._markers_root_var.set(p)

    def _rescan_subs(self):
        for w in self._subs_inner.winfo_children():
            w.destroy()
        self._sub_vars.clear()
        self._sub_progress_labels.clear()
        src = self._src_var.get()
        if not src:
            return
        subs = list_subdirs(Path(src))
        if not subs:
            ttk.Label(self._subs_inner, text="（该目录下没有子目录，将直接处理整个目录）",
                      foreground="gray").pack(anchor="w")
            return
        # 单列布局：左边勾选 + 目录名，右边进度 Label
        for i, name in enumerate(subs):
            var = tk.BooleanVar(value=True)
            row = ttk.Frame(self._subs_inner)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            row.columnconfigure(0, weight=1)
            cb = ttk.Checkbutton(row, text=name, variable=var)
            cb.grid(row=0, column=0, sticky="w")
            prog = ttk.Label(row, text="—", foreground="#888",
                             font=("Consolas", 9), width=42, anchor="e")
            prog.grid(row=0, column=1, sticky="e", padx=(8, 4))
            self._sub_vars.append((name, var))
            self._sub_progress_labels[name] = prog
        # 让 subs_inner 撑满 canvas 宽度，进度列才能贴右
        self._subs_inner.columnconfigure(0, weight=1)

    def _sub_toggle_all(self, value: bool):
        for _, v in self._sub_vars:
            v.set(value)

    # ---------- 运行/停止 ----------

    def _on_action_click(self):
        """主按钮点击：根据当前状态分发到 _on_run 或 _on_stop。"""
        if self._action_state == "stop":
            self._on_stop()
        else:
            self._on_run()

    def _refresh_action_button(self):
        """按最新 job 状态切换主按钮的文案和绑定动作。

        - job 是 pending/running → 显示『■ 停止当前任务』
        - 其他（done/failed/stopped/无任务） → 显示『▶ 运行』
        """
        try:
            state = self._probe_running_state()
        except Exception:
            state = "run"
        if state == "stop":
            if self._action_state != "stop":
                self._action_btn.configure(text="■ 停止当前任务")
                self._action_state = "stop"
        else:
            if self._action_state != "run":
                self._action_btn.configure(text="▶ 运行")
                self._action_state = "run"

    def _probe_running_state(self) -> str:
        """判断是否有任务正在跑，返回 'stop' 或 'run'。"""
        out = self._out_var.get().strip()
        if not out:
            return "run"
        try:
            root = Path(out)
        except Exception:
            return "run"
        if not root.is_dir():
            return "run"
        st = find_latest_job(root)
        if not st:
            return "run"
        # 状态为 pending/running 且 worker 还活着才算"在跑"
        if st.state in ("pending", "running") and pipeline._process_alive(st.pid):
            return "stop"
        return "run"

    def _schedule_action_refresh(self):
        """每 5 秒轮询一次，自动切换按钮文案。"""
        try:
            self._refresh_action_button()
            self._refresh_progress_panel()
        finally:
            # 只要主窗口还在就继续轮询
            try:
                if self.root.winfo_exists():
                    self.root.after(5000, self._schedule_action_refresh)
            except Exception:
                pass

    def _refresh_progress_panel(self):
        """把最新 job 的进度铺到主窗：汇总条 + 每个子目录一行 Label。
        没任务或输出根找不到，就把进度列清空、汇总条显示空闲。"""
        # 默认状态
        summary = "空闲"
        sub_state: dict[str, tuple[str, int, int]] = {}

        try:
            out = self._out_var.get().strip()
            if out and Path(out).is_dir():
                st = find_latest_job(Path(out))
                if st:
                    done = sum(1 for s in st.subs if s.stage == "done")
                    failed = sum(1 for s in st.subs if s.stage == "failed")
                    total_ve = sum(int(getattr(s, "videos_extracted", 0) or 0) for s in st.subs)
                    total_vd = sum(int(getattr(s, "videos_deduped", 0) or 0) for s in st.subs)
                    alive = pipeline._process_alive(st.pid)
                    state_disp = st.state
                    if st.state in ("pending", "running") and not alive:
                        state_disp = f"{st.state}(worker已退出)"
                    summary = (
                        f"{st.job_id}  ·  {state_disp}  ·  "
                        f"完成 {done}/{st.total_subs}  失败 {failed}  ·  "
                        f"抽帧 {total_ve}  去重 {total_vd}"
                    )
                    if st.last_message:
                        # 消息太长截一下，避免撑爆汇总条
                        msg = st.last_message
                        if len(msg) > 60:
                            msg = msg[:57] + "..."
                        summary += f"  ·  {msg}"
                    for s in st.subs:
                        sub_state[s.name] = (
                            s.stage,
                            int(getattr(s, "videos_extracted", 0) or 0),
                            int(getattr(s, "videos_deduped", 0) or 0),
                        )
        except Exception as e:
            summary = f"（读取状态失败：{e}）"

        # 汇总条
        try:
            self._summary_var.set(summary)
        except Exception:
            pass

        # 每个子目录一行进度
        stage_map = {
            "pending": "待处理",
            "extracting": "抽帧中",
            "done": "抽帧完",
            "failed": "失败",
            "skipped": "跳过",
        }
        for name, lbl in self._sub_progress_labels.items():
            try:
                if name in sub_state:
                    stage, ve, vd = sub_state[name]
                    disp = stage_map.get(stage, stage)
                    fg = "#0057b7"
                    if stage == "done":
                        fg = "#0a7f2e"
                    elif stage == "failed":
                        fg = "#c0392b"
                    elif stage == "extracting":
                        fg = "#d17b00"
                    lbl.configure(
                        text=f"{disp}  抽帧={ve:<4} 去重={vd:<4}",
                        foreground=fg,
                    )
                else:
                    lbl.configure(text="—", foreground="#888")
            except Exception:
                pass
    def _on_run(self):
        if not getattr(self, "_env_ok", False):
            if not messagebox.askyesno("环境缺失", "有必需文件缺失，仍然继续吗？"):
                return
        src = self._src_var.get().strip()
        out = self._out_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("参数错误", f"源目录无效：{src}"); return
        if not out:
            messagebox.showerror("参数错误", "输出根不能为空"); return

        selected = [name for name, v in self._sub_vars if v.get()]
        if self._sub_vars and not selected:
            messagebox.showerror("参数错误", "至少勾选一个子目录"); return

        # 先把当前选择存进配置，下次启动自动填回来
        _save_config(self._dump_current_config())

        # 构造 argparse.Namespace 直接调 pipeline.cmd_submit
        args = argparse.Namespace(
            cmd="submit",
            out_root=out,
            data_drive=self._drive_var.get() or None,
            auto=True,
            src=src,
            subs=",".join(selected) if selected else None,
            data_prefix=None,
            threshold=int(self._threshold_var.get()),
            fps=float(self._fps_var.get()),
            ext=".h265,.mp4",
            apply=bool(self._apply_var.get()),
            hard_delete=bool(self._hard_delete_var.get()),
            motion_threshold=float(self._motion_var.get()),
            daily_remain_limit=int(self._daily_remain_limit_var.get()),
            scene_protect=bool(self._scene_protect_var.get()),
            watch_interval=float(self._watch_interval_var.get()),
            protect=self._get_selected_protect_arg(),
            extract_jobs=int(self._extract_jobs_var.get()),
            extract_lock_ttl=float(self._extract_lock_ttl_var.get()),
            dedupe_jobs=int(self._dedupe_jobs_var.get()),
            dedupe_lock_ttl=float(self._dedupe_lock_ttl_var.get()),
            markers_root=self._markers_root_var.get() or None,
            fingerprint=False,
        )

        # 优先让 detach 出来的 worker 是 pipeline.exe，而不是 pipe_gui.exe 自身副本。
        # 这样进程列表和状态浮层显示更干净，也避免自身副本被误当成 GUI 再次弹窗
        # （虽然 _looks_like_cli() 已经兜住，但用 pipeline.exe 更符合直觉）。
        pipeline_exe = _find_pipeline_exe()
        if pipeline_exe:
            os.environ["PIPELINE_WORKER_EXE_OVERRIDE"] = pipeline_exe

        # 在后台线程调 submit（内部会 detach worker，很快返回）
        def run_submit():
            try:
                rc = pipeline.cmd_submit(args)
                self.root.after(0, lambda: messagebox.showinfo(
                    "已提交", f"任务已提交（rc={rc}）。可点『查看状态』查看进度。"))
                self.root.after(0, self._refresh_action_button)
            except SystemExit as se:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"pipeline 退出码 {se.code}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"{type(e).__name__}: {e}"))

        threading.Thread(target=run_submit, daemon=True).start()

    def _on_reset_config(self):
        """删除配置文件并重置表单为默认。"""
        if not messagebox.askyesno("确认",
                                    "确定要清空『记住上次选择』的配置吗？\n"
                                    "会删除 %USERPROFILE%\\.pic-clear\\pipe_gui.json。"):
            return
        try:
            cp = _config_path()
            if cp.is_file():
                cp.unlink()
        except Exception as e:
            messagebox.showerror("失败", f"删除配置失败: {e}")
            return
        # 重置表单
        self._src_var.set("")
        self._out_var.set("")
        self._threshold_var.set(3)
        self._motion_var.set(0.12)
        self._fps_var.set(1.0)
        self._extract_jobs_var.set(1)
        self._extract_lock_ttl_var.set(900)
        self._dedupe_jobs_var.set(1)
        self._dedupe_lock_ttl_var.set(900)
        self._markers_root_var.set("")
        self._apply_var.set(True)
        self._hard_delete_var.set(True)
        self._minimize_to_tray_var.set(True)
        self._hotkey_var.set("ctrl+alt+p")
        self._scene_protect_var.set(True)
        self._daily_remain_limit_var.set(80000)
        self._watch_interval_var.set(3.0)
        # 清空子目录列表
        for w in self._subs_inner.winfo_children():
            w.destroy()
        self._sub_vars.clear()
        # 重跑自动挑盘逻辑
        self._auto_pick_drive()
        messagebox.showinfo("已重置", "配置已清空，可以重新选择。")

    def _on_stop(self):
        out = self._out_var.get().strip()
        if not out:
            messagebox.showerror("参数错误", "先填输出根"); return
        st = find_latest_job(Path(out))
        if not st:
            messagebox.showinfo("无任务", "没找到可停止的任务"); return
        if not messagebox.askyesno("确认",
                                    f"停止任务 {st.job_id}（PID {st.pid}）吗？"):
            return
        args = argparse.Namespace(cmd="stop", out_root=out,
                                  data_drive=self._drive_var.get() or None,
                                  job_id=st.job_id, fingerprint=False)
        try:
            rc = pipeline.cmd_stop(args)
            messagebox.showinfo("已停止", f"rc={rc}")
            self._refresh_action_button()
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    def _on_kill_all(self):
        """一键杀掉所有 pipeline/extract/dedupe/其他 pipe_gui 实例，跳过自己。"""
        # 二次确认
        target_list = "\n".join(f"    - {n}" for n in KILL_TARGETS)
        if not messagebox.askyesno(
            "确认一键清理",
            "将强制杀掉以下所有正在运行的进程（当前 GUI 自己除外）：\n\n"
            f"{target_list}\n\n"
            "适用场景：bat 脚本残留、多次误启动、状态卡死。\n"
            "任务会立刻中断，未处理完的图片保持原样，请确认？"):
            return

        my_pid = os.getpid()
        try:
            results, killed = kill_all_related(exclude_pids=[my_pid])
        except Exception as e:
            messagebox.showerror("失败", f"kill 失败: {e}")
            return

        # 顺手把最新 job 标记为 stopped，避免状态浮层里显示"僵尸 running"
        marked = None
        out = self._out_var.get().strip()
        if out and Path(out).is_dir():
            try:
                marked = mark_latest_job_stopped(Path(out))
            except Exception as e:
                print(f"[WARN] mark_latest_job_stopped: {e}", file=sys.stderr)

        # 汇报结果
        if not results:
            messagebox.showinfo("清理完成", "没有发现需要杀掉的相关进程。")
            self._refresh_action_button()
            return
        lines = [f"共尝试 {len(results)} 个，成功杀掉 {killed} 个：\n"]
        for name, pid, ok in results:
            mark = "√" if ok else "×"
            lines.append(f"  [{mark}] {name}  PID {pid}")
        if marked:
            lines.append(f"\n已把任务 {marked} 状态改为 stopped。")
        failed = len(results) - killed
        if failed > 0:
            lines.append(f"\n{failed} 个未能杀掉，可能需要管理员权限或已自行退出。")
        messagebox.showinfo("清理完成", "\n".join(lines))
        self._refresh_action_button()

    # ---------- 托盘 ----------

    def hide_to_tray(self):
        try:
            self._ensure_tray()
        except Exception as e:
            messagebox.showerror("托盘失败", f"无法创建托盘：{e}")
            return
        self.root.withdraw()

    def _ensure_tray(self):
        if self._tray_icon is not None:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as e:
            raise RuntimeError(f"缺少 pystray/Pillow：{e}")

        img = None
        try:
            icon_png = _resource_path("icon.png")
            if os.path.exists(icon_png):
                img = Image.open(icon_png).convert("RGBA")
        except Exception:
            img = None
        if img is None:
            img = Image.new("RGB", (64, 64), color=(30, 100, 200))
            d = ImageDraw.Draw(img)
            d.rectangle((12, 20, 52, 44), fill=(255, 255, 255))
            d.text((22, 24), "PC", fill=(30, 100, 200))

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: self.root.after(0, self.show_main)),
            pystray.MenuItem("显示状态", lambda: self.root.after(0, self.show_status_window)),
            pystray.MenuItem("停止当前任务", lambda: self.root.after(0, self._on_stop)),
            pystray.MenuItem("一键杀死所有", lambda: self.root.after(0, self._on_kill_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit_all)),
        )
        self._tray_icon = pystray.Icon("pic-clear", img, "pic-clear", menu)

        def run_tray():
            try:
                self._tray_icon.run()
            except Exception:
                pass

        self._tray_thread = threading.Thread(target=run_tray, daemon=True)
        self._tray_thread.start()

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        # 用户点右上角 ×：
        #   - 当前配置为"最小化到托盘"  → 最小化 + 首次弹提示（可勾选"以后不再提示"）
        #   - 未开启                    → 彻底退出
        # 无论哪种，都先把当前主窗口 geometry 存下来，方便下次原样打开
        try:
            _save_config(self._dump_current_config())
        except Exception:
            pass
        if self._minimize_to_tray_var.get():
            self.hide_to_tray()
            self._maybe_show_close_hint()
        else:
            self.quit_all()

    def _maybe_show_close_hint(self):
        """第一次点 ×（且勾了"最小化到托盘"）时弹提示，告诉用户程序没退出。
        用户可以勾"以后不再提示"，勾选后写入配置文件，永久静默。"""
        cfg = getattr(self, "_loaded_config", None) or {}
        if cfg.get("hide_close_hint"):
            return

        # 用一个自定义 Toplevel，因为 messagebox 不支持塞复选框
        try:
            top = tk.Toplevel(self.root)
        except Exception:
            return
        top.title("pic-clear 编排工具 - 已最小化到托盘")
        _apply_window_icon(top)
        try:
            top.transient(self.root)
        except Exception:
            pass
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass
        scale = getattr(self, "_ui_scale", 1.0)
        top.geometry(_scale_geometry(520, 300, scale))
        top.resizable(False, False)

        tk.Label(top, text="ⓘ  程序已最小化到系统托盘",
                 font=("Microsoft YaHei", 14, "bold"),
                 foreground="#0066cc").pack(pady=(18, 6))

        msg = (
            "点击右上角 × 只是把窗口收起来了，程序仍在后台运行。\n"
            "\n"
            "• 屏幕右下角通知区域（时间旁边）有 pic-clear 图标，\n"
            "  右键 → 显示主窗口 / 退出\n"
            "• 如果看不到图标，点通知区域的 ∧ 展开\n"
            "• 快捷键 Ctrl+Alt+P 可随时呼出主窗口\n"
            "\n"
            "如果你希望 × 直接退出，请在主界面『后台 & 快捷键』Tab 里，\n"
            "取消勾选『关闭时最小化到托盘』。"
        )
        tk.Label(top, text=msg, font=("Microsoft YaHei", 10),
                 justify="left", wraplength=int(480 * scale),
                 foreground="#333").pack(padx=18, pady=(0, 8), anchor="w")

        hide_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="以后不再提示", variable=hide_var).pack(
            anchor="w", padx=18, pady=(0, 6))

        def _confirm():
            if hide_var.get():
                try:
                    cfg2 = _load_config()
                    cfg2["hide_close_hint"] = True
                    _save_config(cfg2)
                    # 同步到内存里的缓存，避免同一会话再次弹
                    self._loaded_config = cfg2
                except Exception:
                    pass
            top.destroy()

        ttk.Button(top, text="知道了", command=_confirm, width=12).pack(pady=(4, 14))
        top.protocol("WM_DELETE_WINDOW", _confirm)

    def quit_all(self):
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        try:
            if self._hotkey_registered:
                import keyboard
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()
        # 兜底强杀：pystray 的托盘线程 + keyboard 的低级钩子线程即使调了
        # stop / unhook_all_hotkeys，也可能还在 Windows 消息队列里等下一个
        # 事件。给 400ms 收尾时间，然后 os._exit(0) 直接结束进程，
        # 避免任务管理器里残留 pipe_gui.exe。
        try:
            import threading
            threading.Timer(0.4, lambda: os._exit(0)).start()
        except Exception:
            os._exit(0)

    # ---------- 状态浮层 ----------

    def show_status_window(self):
        if self._status_toplevel is not None and self._status_toplevel.winfo_exists():
            self._status_toplevel.deiconify()
            self._status_toplevel.lift()
            self._status_toplevel.focus_force()
            return
        w = tk.Toplevel(self.root)
        self._status_toplevel = w
        w.title("pic-clear 编排工具 - 运行状态")
        _apply_window_icon(w)
        w.geometry(_scale_geometry(720, 520, getattr(self, "_ui_scale", 1.0)))
        w.protocol("WM_DELETE_WINDOW", lambda: (w.withdraw()))

        info = tk.Text(w, font=("Consolas", 10))
        info.pack(fill="both", expand=True, padx=8, pady=8)
        info.config(state="disabled")

        btn = ttk.Frame(w); btn.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn, text="立即刷新", command=lambda: self._refresh_status_text(info)).pack(side="left")
        ttk.Button(btn, text="关闭", command=lambda: w.withdraw()).pack(side="right")

        self._status_labels = {"info": info}
        self._refresh_status_text(info)
        self._schedule_status_refresh()

    def _schedule_status_refresh(self):
        w = self._status_toplevel
        if w is None or not w.winfo_exists():
            return
        info = self._status_labels.get("info")
        if info and w.winfo_viewable():
            self._refresh_status_text(info)
        self.root.after(self.REFRESH_MS, self._schedule_status_refresh)

    def _refresh_status_text(self, info: tk.Text):
        procs = query_processes()
        lines = []
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"pic-clear 运行状态     {ts}")
        lines.append("=" * 56)
        for name in PROC_NAMES:
            r = procs[name]
            if r["running"]:
                pids = ",".join(str(p) for p in r["pids"])
                mem = format_kb(r["mem_kb"])
                if r["count"] > 1:
                    lines.append(f"  [ OK  ] {name:<22} 运行中  x{r['count']}  PID {pids}  {mem}")
                else:
                    lines.append(f"  [ OK  ] {name:<22} 运行中   PID {pids}   内存 {mem}")
            else:
                lines.append(f"  [ --  ] {name:<22} 未运行")
        lines.append("-" * 56)

        out = self._out_var.get().strip()
        if out and Path(out).is_dir():
            st = find_latest_job(Path(out))
            if st:
                done = sum(1 for s in st.subs if s.stage == "done")
                failed = sum(1 for s in st.subs if s.stage == "failed")
                lines.append(f"  最新任务  : {st.job_id}")
                lines.append(f"  状态      : {st.state}    worker PID: {st.pid}")
                lines.append(f"  进度      : {done} / {st.total_subs}   (失败 {failed})")
                lines.append(f"  最后消息  : {st.last_message}")
                # 每个子目录的抽帧/去重进度（marker 驱动，视频粒度）
                if st.subs:
                    lines.append("")
                    lines.append("  子目录进度（抽帧=视频已抽完数，去重=视频已去重数）：")
                    total_extract = 0
                    total_dedup = 0
                    for idx, s in enumerate(st.subs, 1):
                        name = (s.name or f"sub{idx}")
                        # 名字过长截断，保证一行不溢
                        short = name if len(name) <= 28 else name[:25] + "..."
                        stage_disp = {
                            "pending": "待处理",
                            "extracting": "抽帧中",
                            "done": "抽帧完",
                            "failed": "失败",
                            "skipped": "跳过",
                        }.get(s.stage, s.stage)
                        ve = int(getattr(s, "videos_extracted", 0) or 0)
                        vd = int(getattr(s, "videos_deduped", 0) or 0)
                        total_extract += ve
                        total_dedup += vd
                        lines.append(
                            f"    [{idx:>2}] {short:<28}  {stage_disp:<6}  抽帧={ve:<4}  去重={vd:<4}"
                        )
                    lines.append(
                        f"  合计      : 抽帧={total_extract}  去重={total_dedup}"
                    )
            else:
                lines.append("  最新任务  : （无）")
        else:
            lines.append("  最新任务  : （输出目录未设置或不存在，无法查询）")

        info.config(state="normal")
        info.delete("1.0", "end")
        info.insert("end", "\n".join(lines))
        info.config(state="disabled")

    # ---------- 日志浮层 ----------

    LOG_TAIL_MAX_BYTES = 200_000  # 首次打开只加载末尾 200KB，防止大日志卡死
    LOG_TAIL_LINES = 500          # Text 里最多留多少行，超了裁前面
    LOG_REFRESH_MS = 2000         # 自动 tail 刷新间隔

    def show_log_window(self):
        if self._log_toplevel is not None and self._log_toplevel.winfo_exists():
            self._log_toplevel.deiconify()
            self._log_toplevel.lift()
            self._log_toplevel.focus_force()
            return
        w = tk.Toplevel(self.root)
        self._log_toplevel = w
        w.title("pic-clear 编排工具 - 日志")
        _apply_window_icon(w)
        w.geometry(_scale_geometry(900, 560, getattr(self, "_ui_scale", 1.0)))
        w.protocol("WM_DELETE_WINDOW", lambda: (w.withdraw()))

        # 顶部：日志种类切换 + 当前 job 信息
        top = ttk.Frame(w); top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="日志种类：").pack(side="left")
        which_var = tk.StringVar(value="worker")
        cb = ttk.Combobox(top, textvariable=which_var,
                          values=["worker", "pipeline"],
                          state="readonly", width=12)
        cb.pack(side="left", padx=4)
        job_info_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=job_info_var, foreground="#555").pack(side="left", padx=12)

        # 文本区
        mid = ttk.Frame(w); mid.pack(fill="both", expand=True, padx=8, pady=4)
        txt = tk.Text(mid, font=("Consolas", 9), wrap="none")
        yscroll = ttk.Scrollbar(mid, orient="vertical", command=txt.yview)
        xscroll = ttk.Scrollbar(mid, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        txt.config(state="disabled")

        # 底部按钮
        auto_tail_var = tk.BooleanVar(value=True)
        btm = ttk.Frame(w); btm.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Checkbutton(btm, text="自动 tail（2 秒刷）",
                        variable=auto_tail_var).pack(side="left")
        ttk.Button(btm, text="立即刷新",
                   command=lambda: self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True)
                   ).pack(side="left", padx=6)
        ttk.Button(btm, text="打开 job 目录",
                   command=self._open_current_job_dir).pack(side="left", padx=6)
        ttk.Button(btm, text="清屏",
                   command=lambda: self._clear_log_text(txt)).pack(side="left", padx=6)
        ttk.Button(btm, text="关闭", command=lambda: w.withdraw()).pack(side="right")

        # 状态存到实例，供 tail 循环用
        self._log_widgets = {
            "text": txt,
            "which": which_var,
            "auto_tail": auto_tail_var,
            "job_info": job_info_var,
            "last_size": {"worker": 0, "pipeline": 0},
            "last_job_id": None,
        }

        cb.bind("<<ComboboxSelected>>",
                lambda e: self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True))

        # 首次装载
        self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True)
        # 起 tail 循环
        self._schedule_log_refresh()

    def _resolve_log_path(self, which: str) -> "tuple[Path | None, str | None]":
        """定位最新 job 的日志文件。返回 (path, job_id)。"""
        out = self._out_var.get().strip()
        if not out or not Path(out).is_dir():
            return None, None
        st = find_latest_job(Path(out))
        if not st:
            return None, None
        job_dir = pipeline._job_dir(Path(out), st.job_id)
        fname = "worker.log" if which == "worker" else "pipeline.log"
        p = job_dir / fname
        return (p if p.is_file() else None), st.job_id

    def _reload_log_text(self, txt: tk.Text, which: str,
                         job_info_var: tk.StringVar, full_reload: bool = False):
        """重新加载日志。full_reload=True 时清空 Text 从末尾 200KB 重装。"""
        p, job_id = self._resolve_log_path(which)
        if p is None:
            self._set_log_content(txt, f"（暂无 {which}.log；请先跑一个任务）")
            job_info_var.set("（无任务）")
            self._log_widgets["last_size"][which] = 0
            self._log_widgets["last_job_id"] = None
            return
        # job 切换或强制重载 → 全量重载末尾一段
        need_full = full_reload or (self._log_widgets.get("last_job_id") != job_id)
        try:
            size = p.stat().st_size
        except OSError:
            return
        if need_full:
            try:
                with p.open("rb") as f:
                    if size > self.LOG_TAIL_MAX_BYTES:
                        f.seek(size - self.LOG_TAIL_MAX_BYTES)
                        # 跳过残行
                        f.readline()
                    content = f.read().decode("utf-8", errors="replace")
            except Exception as e:
                content = f"（读取失败：{e}）"
            self._set_log_content(txt, content)
            self._log_widgets["last_size"][which] = size
            self._log_widgets["last_job_id"] = job_id
            job_info_var.set(f"{job_id}  ·  {p.name}  ·  {size:,} 字节")
            self._scroll_log_to_end(txt)
            return
        # 增量 tail
        last_size = int(self._log_widgets["last_size"].get(which, 0))
        if size < last_size:
            # 文件被截断（不太可能），当作全量重载
            self._log_widgets["last_size"][which] = 0
            self._reload_log_text(txt, which, job_info_var, full_reload=True)
            return
        if size == last_size:
            return
        try:
            with p.open("rb") as f:
                f.seek(last_size)
                chunk = f.read(size - last_size).decode("utf-8", errors="replace")
        except Exception:
            return
        self._append_log_text(txt, chunk)
        self._log_widgets["last_size"][which] = size
        job_info_var.set(f"{job_id}  ·  {p.name}  ·  {size:,} 字节")

    def _set_log_content(self, txt: tk.Text, content: str) -> None:
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", content)
        self._trim_log_lines(txt)
        txt.config(state="disabled")

    def _append_log_text(self, txt: tk.Text, chunk: str) -> None:
        if not chunk:
            return
        # 记住滚动位置：如果用户在末尾，追加后自动滚到底；否则不动
        was_at_end = False
        try:
            yv = txt.yview()
            was_at_end = yv[1] >= 0.999
        except Exception:
            pass
        txt.config(state="normal")
        txt.insert("end", chunk)
        self._trim_log_lines(txt)
        txt.config(state="disabled")
        if was_at_end:
            self._scroll_log_to_end(txt)

    def _trim_log_lines(self, txt: tk.Text) -> None:
        try:
            total = int(txt.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > self.LOG_TAIL_LINES:
            over = total - self.LOG_TAIL_LINES
            txt.delete("1.0", f"{over + 1}.0")

    def _scroll_log_to_end(self, txt: tk.Text) -> None:
        try:
            txt.see("end")
        except Exception:
            pass

    def _clear_log_text(self, txt: tk.Text) -> None:
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.config(state="disabled")

    def _schedule_log_refresh(self):
        w = self._log_toplevel
        if w is None or not w.winfo_exists():
            return
        try:
            if w.winfo_viewable() and self._log_widgets.get("auto_tail") and \
                    self._log_widgets["auto_tail"].get():
                self._reload_log_text(
                    self._log_widgets["text"],
                    self._log_widgets["which"].get(),
                    self._log_widgets["job_info"],
                    full_reload=False,
                )
        except Exception:
            pass
        try:
            self.root.after(self.LOG_REFRESH_MS, self._schedule_log_refresh)
        except Exception:
            pass

    def _open_current_job_dir(self):
        out = self._out_var.get().strip()
        if not out or not Path(out).is_dir():
            messagebox.showinfo("提示", "输出根目录未设置或不存在"); return
        st = find_latest_job(Path(out))
        if not st:
            messagebox.showinfo("提示", "还没有任何任务"); return
        job_dir = pipeline._job_dir(Path(out), st.job_id)
        try:
            if os.name == "nt":
                os.startfile(str(job_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(job_dir)])
            else:
                subprocess.Popen(["xdg-open", str(job_dir)])
        except Exception as e:
            messagebox.showerror("失败", f"打开目录失败：{e}")

    # ---------- 全局快捷键 ----------

    def _register_hotkey(self):
        try:
            import keyboard
        except Exception as e:
            messagebox.showerror("快捷键失败",
                                 f"缺少 keyboard 库：{e}"); return
        hk = self._hotkey_var.get().strip()
        if not hk:
            messagebox.showerror("快捷键失败", "请填快捷键，如 ctrl+alt+p"); return
        try:
            if self._hotkey_registered:
                keyboard.unhook_all_hotkeys()
            keyboard.add_hotkey(hk, lambda: self.root.after(0, self.show_status_window))
            self._hotkey_registered = True
            messagebox.showinfo("已注册", f"快捷键：{hk}\n呼出状态浮层")
        except Exception as e:
            messagebox.showerror("快捷键失败", f"注册失败：{e}\n有的堡垒机 RDP 会话可能拦截全局钩子。")


# =========================================================================
# 入口
# =========================================================================

# 会被识别为 pipeline CLI 的子命令；命中就绕开 GUI 直接把 argv 交给 pipeline.main()
_PIPELINE_CLI_CMDS = {"submit", "worker", "list", "status", "logs", "stop"}


def _looks_like_cli() -> bool:
    """判断 argv 是不是 pipeline 的 CLI 调用（尤其是 detach 出来的 worker）。

    典型场景：pipeline.cmd_submit() 里 detach 时会用 sys.executable 重新拉起自己，
    命令行长成  pipe_gui.exe worker --job-id xxx --out-root xxx。
    这时候不能进 GUI，要走 CLI，否则会：
      1) 再弹一个 GUI 窗口（用户看到的问题一）
      2) worker 根本没跑起来（用户看到的问题二）
    """
    if "--fingerprint" in sys.argv[1:]:
        return True
    for a in sys.argv[1:]:
        if a.startswith("-"):
            continue
        # 第一个非选项 token 是子命令名
        return a in _PIPELINE_CLI_CMDS
    return False


def main() -> int:
    # ---- 高 DPI 感知必须在任何 Tk / GUI 代码之前调用 ----
    # 移到 main() 首行：老实现放在 tk.Tk() 之后，导致 Windows 已经用低分辨率
    # 位图创建了 Tk 根窗口，再声明就来不及了（表现为字号偏小）。
    _enable_hidpi_awareness()

    # ---- 诊断参数：--diag-dpi 打印 DPI 相关信息后直接退出 ----
    if "--diag-dpi" in sys.argv[1:]:
        return _run_diag_dpi()

    # ---- CLI 短路：命中 pipeline 子命令时，直接把控制权交给 pipeline.main() ----
    # 这是修复"detach worker 又开 GUI"和"worker 未运行"两个问题的关键。
    if _looks_like_cli():
        return pipeline.main()

    # ---- 正常 GUI 启动 ----
    # 授权检查：不再走 pipeline._check_license_or_die（它只 print+sys.exit，
    # 但 pipe_gui 是 --windowed 打包无控制台，用户看不到消息）。
    # 改成自己做 verify_license，未通过弹一个 Tk 对话框展示指纹 + 复制按钮。
    license_info = probe_license_status()
    if not license_info.get("ok"):
        # 环境变量后门：PIPELINE_SKIP_LICENSE=1 时跳过（跟 pipeline 一致）
        if os.environ.get("PIPELINE_SKIP_LICENSE") == "1":
            print("[授权] PIPELINE_SKIP_LICENSE=1，跳过授权（开发模式）", flush=True)
        else:
            try:
                show_license_error_dialog(license_info)
            except Exception as e:
                # 极端情况兜底：Tk 起不来就退到 CLI 行为
                print(f"[授权] {license_info.get('msg', '未授权')}", file=sys.stderr)
                print(f"[授权] 本机指纹: {license_info.get('fingerprint','?')}",
                      file=sys.stderr)
                print(f"[授权] 对话框展示失败: {e}", file=sys.stderr)
            sys.exit(3)

    # ---- 动态口令（TOTP，v0.3.0 新增） ----
    # 授权通过后再验；未通过 sys.exit(4)。兼容行为见 require_otp_or_die 注释。
    require_otp_or_die()

    root = tk.Tk()
    # 立刻隐藏窗口，直到所有 UI 构造 + geometry 都定好再显示。
    # 不这么做就会出现"先弹一个默认小窗口 → 再变大 → 再填充控件"三段闪烁。
    root.withdraw()
    try:
        # DPI aware 已经在 main() 首行声明过。这里只算 scale 并放大 Tk 字号。
        scale = _apply_dpi_scaling(root)
        # 把 scale 挂到 root 上，供后续窗口（状态浮层、日志浮层）复用
        root.__ui_scale__ = scale
        app = PipeGUI(root)
        # 把授权信息传进 app，用于『关于』Tab 展示
        app.license_info = license_info
        # 重新构建 About Tab（此时 license_info 已经就位）
        try:
            app._refresh_about_license()
        except Exception:
            pass
        # 所有 UI 就位，立刻显示；deiconify + update_idletasks 保证一次绘制
        root.update_idletasks()
        root.deiconify()
        root.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("崩溃", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
