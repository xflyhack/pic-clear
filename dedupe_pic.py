# -*- coding: utf-8 -*-
"""
dedupe_pic.py — 扫描目录中的图片（jpg/jpeg/png 等），基于感知哈希 (dHash)
找出内容"接近相同"的图片组，默认仅输出报告 (dry-run)；加 --apply 才真正删除。

设计目标：
- 单文件脚本，方便 PyInstaller 打包为独立 exe（无需目标机安装 Python）。
- 使用 dHash (8x8 差分哈希, 64bit) + Hamming 距离，做"近似重复"聚类。
- 【可选】用 YOLOv8n ONNX 做目标检测，识别到保护类别（人 / 车 / 电车等）
  的图片一律 KEEP，不参与删除。
- 删除策略可选：largest / oldest / shortest-path。
- 删除前默认软删除到 --trash-dir，可 --hard-delete 直接删除。
"""

from __future__ import annotations

import argparse
import io
import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageFile
except ImportError:
    sys.stderr.write(
        "[FATAL] 缺少 Pillow 库。请先 `pip install Pillow`，"
        "或使用已打包好的 exe。\n"
    )
    sys.exit(2)

# 部分被截断的 JPEG 也尽量解码
ImageFile.LOAD_TRUNCATED_IMAGES = True


def _force_utf8_stdio() -> None:
    """PyInstaller 在 Windows 上 stdout 默认使用 cp1252/GBK，中文输出会崩溃。
    这里强制切成 UTF-8，errors=replace 兜底，防止极端字符再次抛异常。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(buf, encoding="utf-8", errors="replace"),
                )


_force_utf8_stdio()


def _disable_windows_quickedit() -> None:
    """关闭 Windows 控制台的"快速编辑模式"。

    Windows cmd/控制台默认启用 QuickEdit：用户在窗口里点一下鼠标就会进入
    选择/暂停状态，所有 stdout 写入都会阻塞，直到按回车/Esc 才继续。
    这会导致程序看起来"卡死"，实际是被终端挂起。这里在启动时主动把它关掉。
    非 Windows 或拿不到控制台句柄时静默跳过。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_INSERT_MODE = 0x0020
        ENABLE_MOUSE_INPUT = 0x0010

        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = mode.value
        new_mode |= ENABLE_EXTENDED_FLAGS
        new_mode &= ~ENABLE_QUICK_EDIT_MODE
        new_mode &= ~ENABLE_MOUSE_INPUT
        new_mode &= ~ENABLE_INSERT_MODE
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


_disable_windows_quickedit()


# ----------------------------- dHash ---------------------------------------

def dhash(image_path: Path, hash_size: int = 8) -> int | None:
    """计算图片的 dHash，返回 64-bit 整数；失败返回 None。"""
    try:
        with Image.open(image_path) as img:
            img = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.LANCZOS
            )
            pixels = list(img.tobytes())  # 8-bit L 模式，每字节即一个像素
    except Exception:
        return None

    bits = 0
    idx = 0
    width = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * width + col]
            right = pixels[row * width + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
            idx += 1
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# ----------------------------- 扫描 -----------------------------------------

@dataclass
class Item:
    path: Path
    size: int
    mtime: float
    phash: int
    is_protected: bool = False
    detected_classes: tuple[str, ...] = ()
    max_conf: float = 0.0
    # 运动检测相关
    vehicle_boxes: tuple[tuple[float, float, float, float], ...] = ()
    image_size: tuple[int, int] | None = None
    motion_protected: bool = False   # 相邻帧车变化 → True
    motion_reason: str = ""          # 变化原因描述（写入 CSV）


def count_files(root: Path, extensions: set[str] | None) -> int:
    """预扫一遍统计总文件数；不解码，只走 os.walk，速度很快。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if extensions:
                dot = name.rfind(".")
                if dot < 0:
                    continue
                if name[dot + 1:].lower() in extensions:
                    total += 1
            else:
                total += 1
    return total


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


class ProgressReporter:
    """按时间节流的进度打印器，支持 done/total、速率、ETA。

    触发打印条件（任一即触发）：
      - 距上次打印超过 min_interval 秒
      - 累计处理了 min_count_step 张（保证快机器也有心跳）
      - force=True
    """

    def __init__(
        self,
        total: int,
        prefix: str = "",
        min_interval: float = 3.0,
        min_count_step: int = 200,
    ) -> None:
        self.total = total
        self.prefix = prefix
        self.min_interval = min_interval
        self.min_count_step = max(1, min_count_step)
        self.start = time.time()
        self.last_print = 0.0
        self.last_done = 0
        self.done = 0

    def update(self, done: int, extra: str = "", force: bool = False) -> None:
        self.done = done
        now = time.time()
        by_time = (now - self.last_print) >= self.min_interval
        by_count = (done - self.last_done) >= self.min_count_step
        if not (force or by_time or by_count):
            return
        self.last_print = now
        self.last_done = done
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        remain = (self.total - done) / rate if rate > 0 else float("nan")
        pct = 100.0 * done / self.total if self.total else 0.0
        msg = (
            f"{self.prefix} {done}/{self.total} ({pct:.1f}%)  "
            f"速率 {rate:.1f}/s  已用 {_fmt_eta(elapsed)}  剩余 ~{_fmt_eta(remain)}"
        )
        if extra:
            msg += f"  {extra}"
        print(msg, flush=True)

    def finish(self, extra: str = "") -> None:
        elapsed = time.time() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        print(
            f"{self.prefix} 完成 {self.done}/{self.total}  "
            f"平均 {rate:.1f}/s  总耗时 {_fmt_eta(elapsed)}"
            + (f"  {extra}" if extra else ""),
            flush=True,
        )


def iter_files(root: Path, extensions: set[str] | None) -> Iterable[Path]:
    """
    递归遍历 root。如果 extensions 为空，则返回所有文件（靠 Pillow 判定是否图片）。
    否则只返回后缀在 extensions 内的文件（大小写不敏感）。
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if extensions:
                if p.suffix.lower().lstrip(".") in extensions:
                    yield p
            else:
                yield p


def build_index(
    root: Path,
    extensions: set[str] | None,
    detector=None,
    protect_classes: set[str] | None = None,
    total: int | None = None,
    progress_interval: float = 5.0,
) -> tuple[list[Item], list[Path]]:
    """扫描 root 下所有匹配图片，计算 dHash；如果提供了 detector，
    还会跑目标检测并在 Item 上打上 is_protected 标记。

    total: 预扫得到的总文件数；用于计算百分比 / ETA。
    progress_interval: 进度打印的最小时间间隔（秒）。
    """
    items: list[Item] = []
    failed: list[Path] = []
    count = 0
    current_dir: str | None = None
    protect_hits = 0

    stage = "扫描+检测" if detector is not None else "扫描"
    reporter = ProgressReporter(
        total or 0,
        prefix=f"  [{stage}]",
        min_interval=progress_interval,
        min_count_step=50,
    )

    for p in iter_files(root, extensions):
        parent = str(p.parent)
        if parent != current_dir:
            current_dir = parent
            print(f"  [dir] {current_dir}", flush=True)
        count += 1
        try:
            st = p.stat()
        except OSError:
            failed.append(p)
            reporter.update(count)
            continue
        h = dhash(p)
        if h is None:
            failed.append(p)
            reporter.update(count)
            continue

        item = Item(p, st.st_size, st.st_mtime, h)
        if detector is not None and protect_classes:
            try:
                protected, hits, vehicles, size = detector.detect_full(
                    p, protect_classes
                )
            except Exception as e:
                protected, hits, vehicles, size = False, [], [], None
                print(f"  [warn] 检测失败 {p}: {e}", flush=True)
            # 保护口径（默认规则）：
            #   有 person             -> 硬保护 (is_protected=True)
            #   只有车类，没有 person -> 不硬保护，交给"相邻帧车运动"判定
            #                            （动了就 motion_protected=True 保留，
            #                              没动就参与相似度去重被删）
            has_person = any(d.class_name == "person" for d in hits)
            if has_person:
                item.is_protected = True
                item.detected_classes = tuple(
                    sorted({d.class_name for d in hits})
                )
                item.max_conf = max((d.confidence for d in hits), default=0.0)
                protect_hits += 1
            elif hits:
                # 只有车类命中：记录类别方便 CSV 查看，但不设 is_protected
                item.detected_classes = tuple(
                    sorted({d.class_name for d in hits})
                )
                item.max_conf = max((d.confidence for d in hits), default=0.0)
            item.vehicle_boxes = tuple(d.box_xyxy for d in vehicles)
            item.image_size = size
        items.append(item)

        extra = (
            f"受保护 {protect_hits}" if detector is not None else ""
        )
        reporter.update(count, extra=extra, force=(count == 1))

    reporter.finish(
        extra=(f"受保护 {protect_hits}") if detector is not None else ""
    )
    return items, failed


# ------------------------- 近似聚类 ----------------------------------------

def cluster(items: list[Item], threshold: int) -> list[list[Item]]:
    """
    简单的近邻聚类：O(N^2) 距离比较，用 union-find 合并。
    N 到几万级别都能接受；十万级建议加 BK-Tree，本脚本先保证正确+简单。
    """
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    print(f"[聚类] 开始两两比较，共 {n} 个哈希，阈值 <= {threshold}", flush=True)
    reporter = ProgressReporter(
        n, prefix="  [聚类]", min_interval=5.0, min_count_step=500,
    )
    for i in range(n):
        hi = items[i].phash
        for j in range(i + 1, n):
            if hamming(hi, items[j].phash) <= threshold:
                union(i, j)
        reporter.update(i + 1, force=(i == 0))
    reporter.finish()

    groups: dict[int, list[Item]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(items[idx])
    return [g for g in groups.values() if len(g) >= 2]


# ------------------------ 相邻帧车辆变化标记 ---------------------------------

def mark_motion_changes(
    items: list[Item],
    motion_threshold: float,
    iou_threshold: float = 0.5,
) -> int:
    """
    在同一目录内按文件名字典序排序，逐对比较相邻帧车辆状态；
    若发生变化，则将两帧都标记 motion_protected=True。
    返回被标记的图片总数。
    """
    try:
        from detector import vehicle_changed  # type: ignore
    except ImportError:
        from importlib import import_module
        vehicle_changed = import_module("detector").vehicle_changed  # type: ignore

    # 按 parent 目录分组
    by_dir: dict[str, list[Item]] = {}
    for it in items:
        by_dir.setdefault(str(it.path.parent), []).append(it)

    marked = 0
    for dir_path, seq in by_dir.items():
        if len(seq) < 2:
            continue
        # 按文件名字典序
        seq.sort(key=lambda x: x.path.name)
        for i in range(1, len(seq)):
            prev, curr = seq[i - 1], seq[i]
            # 尺寸缺失就跳过（一般不会）
            size = curr.image_size or prev.image_size
            if size is None:
                continue
            changed, reason = vehicle_changed(
                list(prev.vehicle_boxes),
                list(curr.vehicle_boxes),
                size,
                motion_threshold=motion_threshold,
                iou_threshold=iou_threshold,
            )
            if changed:
                for it in (prev, curr):
                    if not it.motion_protected:
                        it.motion_protected = True
                        it.motion_reason = reason
                        marked += 1
    return marked


# --------------------------- 选择保留 ---------------------------------------

def pick_keeper(group: list[Item], strategy: str) -> Item:
    if strategy == "largest":
        return max(group, key=lambda x: (x.size, -x.mtime))
    if strategy == "oldest":
        return min(group, key=lambda x: (x.mtime, -x.size))
    if strategy == "shortest-path":
        return min(group, key=lambda x: (len(str(x.path)), -x.size))
    raise ValueError(f"未知策略: {strategy}")


def _needs_keep(x: Item) -> bool:
    """任一保护信号命中即强制保留：含保护类别 或 前后帧车辆变化。"""
    return x.is_protected or x.motion_protected


def decide_actions(group: list[Item], strategy: str) -> dict[int, str]:
    """
    组内每个 item 的 action 决定：
      - 触发任一保护信号（含保护类别 / 相邻帧车变化）→ 全部 KEEP
      - 其余未保护的图，按 strategy 选一张 KEEP，其余 DELETE
    """
    actions: dict[int, str] = {}
    protected = [x for x in group if _needs_keep(x)]
    unprotected = [x for x in group if not _needs_keep(x)]

    for x in protected:
        actions[id(x)] = "KEEP"

    if not unprotected:
        return actions

    keeper = pick_keeper(unprotected, strategy)
    for x in unprotected:
        actions[id(x)] = "KEEP" if x is keeper else "DELETE"
    return actions


# --------------------------- 输出报告 ---------------------------------------

def write_report(
    groups: list[list[Item]],
    strategy: str,
    report_path: Path,
    failed: list[Path],
    failed_path: Path,
) -> tuple[int, int]:
    total_dup = 0
    total_bytes = 0
    with report_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "group_id", "action", "path", "size_bytes", "mtime",
                "phash_hex", "is_protected", "detected_classes", "max_conf",
                "motion_protected", "motion_reason",
            ]
        )
        for gid, group in enumerate(groups, 1):
            actions = decide_actions(group, strategy)
            for item in group:
                action = actions.get(id(item), "KEEP")
                if action == "DELETE":
                    total_dup += 1
                    total_bytes += item.size
                w.writerow(
                    [
                        gid,
                        action,
                        str(item.path),
                        item.size,
                        time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(item.mtime),
                        ),
                        f"{item.phash:016x}",
                        "yes" if item.is_protected else "no",
                        "|".join(item.detected_classes),
                        f"{item.max_conf:.3f}" if item.max_conf else "",
                        "yes" if item.motion_protected else "no",
                        item.motion_reason,
                    ]
                )
    if failed:
        with failed_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["path"])
            for p in failed:
                w.writerow([str(p)])
    return total_dup, total_bytes


# --------------------------- 删除执行 ---------------------------------------

def do_delete(
    groups: list[list[Item]],
    strategy: str,
    trash_dir: Path | None,
    hard_delete: bool,
) -> tuple[int, int, list[str]]:
    deleted = 0
    freed = 0
    errors: list[str] = []
    for group in groups:
        actions = decide_actions(group, strategy)
        for item in group:
            if actions.get(id(item), "KEEP") != "DELETE":
                continue
            try:
                if hard_delete or trash_dir is None:
                    item.path.unlink()
                else:
                    rel = item.path.name
                    target = trash_dir / f"{int(time.time() * 1000)}_{rel}"
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item.path), str(target))
                deleted += 1
                freed += item.size
            except OSError as e:
                errors.append(f"{item.path}: {e}")
    return deleted, freed, errors


# ------------------------------- main ---------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "扫描目录，基于感知哈希查找近似重复图片。"
            "默认 dry-run 仅输出 CSV 报告；加 --apply 才真正删除。"
        )
    )
    p.add_argument(
        "root",
        type=Path,
        help="要扫描的根目录，例如 D:\\ 或 D:\\pics",
    )
    p.add_argument(
        "--ext",
        default="jpg,jpeg,png,bmp,gif,webp",
        help="要扫描的扩展名（逗号分隔），传 'all' 则忽略扩展名扫描所有文件。默认: %(default)s",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Hamming 距离阈值，0=完全相同，越大越宽松，建议 3~10。默认: %(default)s",
    )
    p.add_argument(
        "--strategy",
        choices=["largest", "oldest", "shortest-path"],
        default="largest",
        help="每组保留哪一张。默认: %(default)s",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=Path("dedupe_report.csv"),
        help="报告输出路径，默认当前目录 dedupe_report.csv",
    )
    p.add_argument(
        "--failed-report",
        type=Path,
        default=Path("dedupe_failed.csv"),
        help="无法解码的文件列表输出路径",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="真正执行删除。默认只生成报告不删。",
    )
    # ---- 目标检测保护 ----
    p.add_argument(
        "--no-protect",
        action="store_true",
        help="关闭目标检测保护，回到纯 dHash 去重（速度更快）。",
    )
    p.add_argument(
        "--allow-no-detector",
        action="store_true",
        help="检测器初始化失败时降级到纯 dHash（默认失败即退出）。"
             "适用于 Windows 缺 VC++ Runtime 的堡垒机场景。",
    )
    p.add_argument(
        "--model",
        default=None,
        help=(
            "yolov8n.onnx 模型路径。默认自动查找 exe 同目录 / "
            "PyInstaller 打包目录 / 当前工作目录。"
        ),
    )
    p.add_argument(
        "--protect",
        default="person,bicycle,car,motorcycle,bus,train,truck",
        help=(
            "要检测的 COCO 类别（逗号分隔）。默认: %(default)s。 "
            "注意：其中 person 是硬保护，命中即保留；其余车类只有在"
            "相邻帧位置发生变化时才会被 motion 保护，否则参与相似度去重。"
        ),
    )
    p.add_argument(
        "--conf",
        type=float,
        default=0.35,
        help="目标检测置信度阈值（0~1，越大越严格）。默认: %(default)s",
    )
    p.add_argument(
        "--motion-threshold",
        type=float,
        default=0.05,
        help=(
            "同目录相邻帧车辆运动阈值（相对 max(W,H) 的比例，0=最灵敏）。"
            "任一车中心位移超过该比例，或 IoU 匹配失败，均视为发生变化。"
            "默认: %(default)s"
        ),
    )
    p.add_argument(
        "--trash-dir",
        type=Path,
        default=None,
        help="删除时先移动到该目录（软删除）。不指定则永久删除。",
    )
    p.add_argument(
        "--hard-delete",
        action="store_true",
        help="强制永久删除，即使指定了 --trash-dir 也直接 unlink。",
    )
    return p.parse_args()


def _check_license_or_die() -> None:
    """授权校验：不通过则打印指纹和错误信息后退出。"""
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        print(f"[FATAL] 无法加载 licensing 模块: {e}", file=sys.stderr)
        sys.exit(2)

    # 优先级：环境变量 DEDUPE_LICENSE > exe 同目录 > 当前工作目录
    # - PyInstaller onefile 打包后 sys.frozen=True，sys.executable 指向 exe 本体
    # - 未打包（开发模式）用 cwd，方便本地测试
    env_lic = os.environ.get("DEDUPE_LICENSE")
    if env_lic:
        license_path = Path(env_lic).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        license_path = Path(sys.executable).resolve().parent / "license.lic"
    else:
        license_path = Path.cwd() / "license.lic"

    ok, msg = verify_license(license_path)
    if ok:
        print(f"[授权] {msg}", flush=True)
        return

    fp = get_fingerprint()
    print("=" * 60)
    print("[授权] 程序未获得有效授权，无法运行。")
    print(f"[授权] 原因: {msg}")
    print(f"[授权] license 期望位置: {license_path}")
    print()
    print(f"[授权] 本机指纹: {fp}")
    print("[授权] 请将上面这行指纹发给作者，获取 license.lic，")
    print("       并放到 dedupe_pic.exe 同目录后重新运行。")
    print("=" * 60)
    sys.exit(3)


def main() -> int:
    # 先做 pre-flight 授权检查（早于任何业务逻辑）
    if "--fingerprint" in sys.argv:
        # 方便用户单独查指纹，不做任何其他事
        try:
            from licensing import get_fingerprint
            print(get_fingerprint())
        except Exception as e:
            print(f"[ERROR] 无法计算指纹: {e}", file=sys.stderr)
            return 2
        return 0

    _check_license_or_die()

    args = parse_args()

    if not args.root.exists():
        print(f"[ERROR] 根目录不存在: {args.root}", file=sys.stderr)
        return 2

    if args.ext.strip().lower() == "all":
        extensions = None
    else:
        extensions = {
            e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()
        }

    print("=" * 60)
    print(f"  扫描根目录: {args.root}")
    print(f"  扩展名过滤: {extensions if extensions else '不过滤（所有文件）'}")
    print(f"  相似阈值 : {args.threshold} (Hamming 距离)")
    print(f"  保留策略 : {args.strategy}")
    print(f"  目标检测 : {'关闭' if args.no_protect else '启用（YOLOv8n）'}")
    if not args.no_protect:
        print(f"  保护规则 : 有 person -> 硬保护；只有车类 -> 动了才保护")
        print(f"  运动阈值 : {args.motion_threshold} (同目录相邻帧车变化)")
    print(f"  执行删除 : {'是' if args.apply else '否 (dry-run)'}")
    if args.apply:
        if args.hard_delete or args.trash_dir is None:
            print("  删除方式 : 直接删除 (unlink)")
        else:
            print(f"  删除方式 : 移动到 {args.trash_dir}")
    print("=" * 60)

    detector = None
    protect_set: set[str] = set()
    if not args.no_protect:
        try:
            import detector as _det_mod  # type: ignore
        except ImportError:
            _det_mod = None  # type: ignore
        if _det_mod is None:
            # 单文件打包场景：detector.py 内嵌进 exe，走这条路
            try:
                from importlib import import_module

                _det_mod = import_module("detector")
            except Exception as e:
                print(
                    f"[FATAL] 无法加载 detector 模块: {e}\n"
                    "请传 --no-protect 关闭目标检测，或检查打包完整性。",
                    file=sys.stderr,
                )
                return 2

        model_path = _det_mod.resolve_model_path(args.model)
        if model_path is None:
            print(
                "[FATAL] 找不到 yolov8n.onnx 模型文件。请传 --model PATH，"
                "或与 exe 同目录放 yolov8n.onnx，或加 --no-protect 跳过检测。",
                file=sys.stderr,
            )
            return 2

        protect_set = {
            s.strip() for s in args.protect.split(",") if s.strip()
        }
        print(f"[检测] 加载模型: {model_path}")
        print(f"[检测] 保护类别: {sorted(protect_set)}  conf>={args.conf}")
        _t = time.time()
        print("[检测] 正在初始化 ONNX Runtime，这可能需要几秒...", flush=True)
        try:
            detector = _det_mod.YoloDetector(
                str(model_path), conf_thres=args.conf
            )
        except Exception as e:
            print(f"[警告] 初始化检测器失败: {e}", file=sys.stderr)
            print("[警告] 常见原因：Windows Server 缺少 VC++ Redistributable 2019+")
            print("       下载并安装： https://aka.ms/vs/17/release/vc_redist.x64.exe")
            if args.allow_no_detector:
                print("[降级] --allow-no-detector 已生效，改用纯 dHash 模式（无人/车保护）")
                print("       ⚠ 请注意：不会保护含人/车的图片，仅按外观相似度去重！")
                detector = None
                protect_set = set()
            else:
                print("       想跳过检测继续跑：加 --allow-no-detector 或 --no-protect")
                return 2
        print(f"[检测] 模型就绪，耗时 {time.time()-_t:.1f}s", flush=True)

    if detector is None and not args.no_protect and not args.allow_no_detector:
        # 上面已经 return 了，这里只是防御性再判一下
        pass

    print("[预扫] 正在统计文件总数...", flush=True)
    _t_pre = time.time()
    total = count_files(args.root, extensions)
    print(
        f"[预扫] 共发现 {total} 个待处理文件，耗时 {time.time()-_t_pre:.1f}s",
        flush=True,
    )
    if total == 0:
        print("[结束] 没有可处理的文件。")
        return 0

    t0 = time.time()
    items, failed = build_index(
        args.root, extensions,
        detector=detector, protect_classes=protect_set,
        total=total,
    )
    print(
        f"[扫描完成] 有效图片 {len(items)}，失败/跳过 {len(failed)}，"
        f"耗时 {_fmt_eta(time.time() - t0)}"
    )

    if detector is not None and items:
        m = mark_motion_changes(items, motion_threshold=args.motion_threshold)
        print(
            f"[运动] 同目录相邻帧车辆变化：标记保护 {m} 张"
            f"（motion_threshold={args.motion_threshold}）"
        )

    if not items:
        print("[结束] 没有可处理的图片。")
        return 0

    t1 = time.time()
    groups = cluster(items, args.threshold)
    print(
        f"[聚类完成] 发现 {len(groups)} 组近似重复，耗时 {time.time() - t1:.1f}s"
    )

    total_dup, total_bytes = write_report(
        groups, args.strategy, args.report, failed, args.failed_report
    )
    print(f"[报告] 写入 {args.report}")
    if failed:
        print(f"[报告] 失败清单写入 {args.failed_report}")
    print(
        f"[报告] 待删除 {total_dup} 个文件，"
        f"可释放约 {total_bytes / 1024 / 1024:.1f} MB"
    )

    if not args.apply:
        print()
        print("这是 dry-run 模式，未删除任何文件。")
        print("请打开 CSV 报告人工确认后，重新加 --apply 执行删除。")
        return 0

    print()
    print("即将执行删除，按 Ctrl+C 可中止。5 秒后开始...")
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        print("\n[用户取消]")
        return 130

    deleted, freed, errors = do_delete(
        groups, args.strategy, args.trash_dir, args.hard_delete
    )
    print(
        f"[删除完成] 成功 {deleted} 个，释放 {freed / 1024 / 1024:.1f} MB，"
        f"失败 {len(errors)} 个"
    )
    if errors:
        for e in errors[:20]:
            print(f"  - {e}")
        if len(errors) > 20:
            print(f"  ...（另外 {len(errors) - 20} 条省略）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
