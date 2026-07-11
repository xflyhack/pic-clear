# -*- coding: utf-8 -*-
"""
dedupe_pic.py — 扫描目录中的图片（jpg/jpeg/png 等），基于感知哈希 (dHash)
找出内容"接近相同"的图片组，默认仅输出报告 (dry-run)；加 --apply 才真正删除。

设计目标：
- 单文件脚本，方便 PyInstaller 打包为独立 exe（无需目标机安装 Python）。
- 忽略扩展名，靠 Pillow 探测真实图像格式；无法解码的文件跳过并记录。
- 使用 dHash (8x8 差分哈希, 64bit) + Hamming 距离，做"近似重复"聚类。
- 删除策略可选：保留文件最大 / 最早修改 / 路径最短的那张。
- 删除前默认将被删文件移动到回收目录 (--trash-dir)，可 --hard-delete 直接删除。
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
    root: Path, extensions: set[str] | None, log_every: int = 200
) -> tuple[list[Item], list[Path]]:
    items: list[Item] = []
    failed: list[Path] = []
    count = 0
    current_dir: str | None = None
    t0 = time.time()
    for p in iter_files(root, extensions):
        # 进入新目录时打印一次，便于观察进度
        parent = str(p.parent)
        if parent != current_dir:
            current_dir = parent
            print(f"  [dir] {current_dir}", flush=True)
        count += 1
        try:
            st = p.stat()
        except OSError:
            failed.append(p)
            continue
        h = dhash(p)
        if h is None:
            failed.append(p)
        else:
            items.append(Item(p, st.st_size, st.st_mtime, h))
        if count % log_every == 0:
            elapsed = time.time() - t0
            rate = count / elapsed if elapsed > 0 else 0
            print(
                f"  ...累计已扫 {count} 个文件，成功 {len(items)}，"
                f"失败 {len(failed)}，速率 {rate:.1f} 文件/秒",
                flush=True,
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
    t0 = time.time()
    for i in range(n):
        hi = items[i].phash
        for j in range(i + 1, n):
            if hamming(hi, items[j].phash) <= threshold:
                union(i, j)
        if (i + 1) % 500 == 0:
            print(
                f"  ...已比较 {i + 1}/{n}，耗时 {time.time() - t0:.1f}s",
                flush=True,
            )

    groups: dict[int, list[Item]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(items[idx])
    return [g for g in groups.values() if len(g) >= 2]


# --------------------------- 选择保留 ---------------------------------------

def pick_keeper(group: list[Item], strategy: str) -> Item:
    if strategy == "largest":
        return max(group, key=lambda x: (x.size, -x.mtime))
    if strategy == "oldest":
        return min(group, key=lambda x: (x.mtime, -x.size))
    if strategy == "shortest-path":
        return min(group, key=lambda x: (len(str(x.path)), -x.size))
    raise ValueError(f"未知策略: {strategy}")


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
            ["group_id", "action", "path", "size_bytes", "mtime", "phash_hex"]
        )
        for gid, group in enumerate(groups, 1):
            keeper = pick_keeper(group, strategy)
            for item in group:
                action = "KEEP" if item is keeper else "DELETE"
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
        keeper = pick_keeper(group, strategy)
        for item in group:
            if item is keeper:
                continue
            try:
                if hard_delete or trash_dir is None:
                    item.path.unlink()
                else:
                    # 保留原相对路径结构到 trash_dir
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


def main() -> int:
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
    print(f"  执行删除 : {'是' if args.apply else '否 (dry-run)'}")
    if args.apply:
        if args.hard_delete or args.trash_dir is None:
            print("  删除方式 : 直接删除 (unlink)")
        else:
            print(f"  删除方式 : 移动到 {args.trash_dir}")
    print("=" * 60)

    t0 = time.time()
    items, failed = build_index(args.root, extensions)
    print(
        f"[扫描完成] 有效图片 {len(items)}，失败/跳过 {len(failed)}，"
        f"耗时 {time.time() - t0:.1f}s"
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
