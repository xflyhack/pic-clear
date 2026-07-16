# -*- coding: utf-8 -*-
"""
classify_pic.py — 对**已经去重完**的图片目录做二次分类。

v1 规则：
  规则 1 舱外活体检测   —— YOLO 检出 person + 载具（bicycle/motorcycle）
                          [骑行 / 滑板 / 三轮上的人]
  规则 2 人体关键点     —— YOLO-pose 检出 person 且**同框无载具**，
                          关节可见数够 [步行 / 站立]
  规则 4 前备箱防夹     —— 路径含前视关键字 + person 命中即可（车内视角）
  规则 5 前机盖开关     —— 少样本 embedding 匹配（走 embed_detector）
  规则 3 动态手势       —— 暂不做

输出目录结构（嵌套 = 目录组织，不代表包含关系）：

  camera/
  ├── 舱外活体检测/               ← 规则 1（互斥于规则 2）
  ├── 人体关键点/                  ← 规则 2
  │   ├── 前备箱防夹检测/          ← 规则 4
  │   └── 前机盖开关检测/          ← 规则 5
  └── <未处理的原子目录>

一张图命中多桶各复制一份（真 copy，覆盖同名）。
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

try:
    from PIL import Image, ImageFile
except ImportError:
    sys.stderr.write("[FATAL] 缺少 Pillow 库。\n")
    sys.exit(2)

ImageFile.LOAD_TRUNCATED_IMAGES = True


def _force_utf8_stdio() -> None:
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
                    sys, stream_name,
                    io.TextIOWrapper(buf, encoding="utf-8", errors="replace"),
                )


_force_utf8_stdio()

# ---------------------------------------------------------------------------
#  常量
# ---------------------------------------------------------------------------

BUCKET_LIVENESS = "舱外活体检测"       # 规则 1（YOLO：人+载具）
BUCKET_KEYPOINT = "人体关键点"         # 规则 2（YOLO-pose：步行/站立）
BUCKET_FRUNK = "前备箱防夹检测"        # 规则 4（路径关键字+人）
BUCKET_HOOD = "前机盖开关检测"         # 规则 5（embedding）
BUCKET_GESTURE = "动态手势"            # 规则 3（占位，暂不做）
BUCKET_OCCLUSION = "遮挡"              # 纯 embedding，跟活体/关键点平级

BUCKET_NAMES = {
    BUCKET_LIVENESS, BUCKET_KEYPOINT, BUCKET_FRUNK, BUCKET_HOOD,
    BUCKET_GESTURE, BUCKET_OCCLUSION,
}

# COCO 里视为"载具"的类别（用于区分骑行 vs 步行）
VEHICLE_CLASSES = frozenset({
    "bicycle", "motorcycle", "skateboard",  # 三轮车 COCO 没有，接受漏检
})

DEFAULT_IMAGE_EXT = ("jpg", "jpeg", "png", "bmp", "webp")

DEFAULT_FRONT_KEYWORDS: tuple[str, ...] = (
    "前视", "左前周视", "右前周视", "左前环视", "右前环视",
)


# ---------------------------------------------------------------------------
#  配置 & 结果
# ---------------------------------------------------------------------------


@dataclass
class ClassifyConfig:
    in_root: Path
    out_root: Path
    camera_dir_name: str = "camera"
    filter_keywords: tuple[str, ...] = ()          # 子目录名包含即跳过
    front_keywords: tuple[str, ...] = DEFAULT_FRONT_KEYWORDS
    image_extensions: tuple[str, ...] = DEFAULT_IMAGE_EXT

    yolo_model: str | None = None
    pose_model: str | None = None
    embed_model: str | None = None
    rules_dir: Path | None = None                  # rules/<桶名>/*.png 样例图

    # 规则 1/2 通用
    person_conf: float = 0.3                       # 鱼眼数据小人多，默认放低
    person_area_ratio: float = 0.005               # 面积占比下限
    vehicle_iou_thres: float = 0.05                # 人-车框有重合视为"骑行"

    # 规则 2
    kp_visible_min: int = 8                        # 17 个 kpt 至少可见数
    kp_vis_thres: float = 0.4

    # embedding 相似度阈值：可给每个桶单独设，未设走 embed_sim_default
    embed_sim_default: float = 0.75
    embed_sim_per_bucket: dict[str, float] = field(default_factory=dict)

    limit: int = 0
    report_path: Path | None = None


@dataclass
class Stats:
    scanned: int = 0
    liveness: int = 0
    keypoint: int = 0
    frunk: int = 0
    hood: int = 0
    occlusion: int = 0
    embed_extra: int = 0        # embedding 补充命中的图片次数（跨桶累加）
    copied_original: int = 0
    copied_bucket: int = 0
    skipped_filter: int = 0
    errors: int = 0


@dataclass
class ClassifyResult:
    rel_path: Path
    buckets: list[str] = field(default_factory=list)
    person_conf: float = 0.0
    vehicle_hit: str = ""
    kp_visible: int = 0
    embed_matches: dict = field(default_factory=dict)   # bucket -> (sim, sample_name)
    error: str = ""


# ---------------------------------------------------------------------------
#  工具
# ---------------------------------------------------------------------------


def _has_keyword(name: str, keywords: tuple[str, ...]) -> bool:
    return any(k and k in name for k in keywords)


def _copy_overwrite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _iter_images(root: Path, extensions: tuple[str, ...]) -> Iterable[Path]:
    ext_set = {e.lower().lstrip(".") for e in extensions}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            dot = name.rfind(".")
            if dot < 0:
                continue
            if name[dot + 1:].lower() in ext_set:
                yield Path(dirpath) / name


def _bucket_path_for(rule: str, camera_out: Path) -> Path:
    if rule == BUCKET_LIVENESS:
        return camera_out / BUCKET_LIVENESS
    if rule == BUCKET_KEYPOINT:
        return camera_out / BUCKET_KEYPOINT
    if rule == BUCKET_FRUNK:
        return camera_out / BUCKET_KEYPOINT / BUCKET_FRUNK
    if rule == BUCKET_HOOD:
        return camera_out / BUCKET_KEYPOINT / BUCKET_HOOD
    if rule == BUCKET_GESTURE:
        return camera_out / BUCKET_KEYPOINT / BUCKET_GESTURE
    if rule == BUCKET_OCCLUSION:
        return camera_out / BUCKET_OCCLUSION
    raise ValueError(f"unknown rule: {rule}")


def _iou(a: tuple[float, float, float, float],
         b: tuple[float, float, float, float]) -> float:
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    iw = max(0.0, ix2 - ix1); ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ub = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    u = ua + ub - inter
    return inter / u if u > 0 else 0.0


def path_matches_front_camera(rel: Path, keywords: tuple[str, ...]) -> bool:
    s = str(rel).replace("\\", "/")
    return any(k and k in s for k in keywords)


# ---------------------------------------------------------------------------
#  单张图分类
# ---------------------------------------------------------------------------


def classify_one(
    img: Path,
    rel: Path,
    yolo,
    pose,
    embed,               # embed_detector.EmbedMatcher | None
    cfg: ClassifyConfig,
) -> ClassifyResult:
    r = ClassifyResult(rel_path=rel)

    # YOLO：一次拿到 person + vehicles
    try:
        _has, hits, vehicles, size = yolo.detect_full(
            img, protect={"person"}
        )
    except Exception as e:
        r.error = f"yolo:{e}"
        return r
    if size is None:
        return r

    W, H = size
    img_area = float(W * H) if W and H else 1.0

    # 面积过滤 person
    def _area(b):
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    persons = [
        d for d in hits
        if d.class_name == "person"
        and d.confidence >= cfg.person_conf
        and _area(d.box_xyxy) / img_area >= cfg.person_area_ratio
    ]

    # 载具（COCO vehicles 中我们关心的那几类）
    ride_vehicles = [
        d for d in vehicles
        if d.class_name in VEHICLE_CLASSES
        and d.confidence >= cfg.person_conf
    ]

    if persons:
        target = max(persons, key=lambda d: d.confidence)
        r.person_conf = target.confidence

        # 判定"骑行"：person 框与任一载具框有重合
        matched_vehicle = None
        for v in ride_vehicles:
            if _iou(target.box_xyxy, v.box_xyxy) >= cfg.vehicle_iou_thres:
                matched_vehicle = v
                break

        if matched_vehicle is not None:
            r.buckets.append(BUCKET_LIVENESS)
            r.vehicle_hit = matched_vehicle.class_name
        else:
            # 步行/站立：进 pose 判定关键点
            if pose is not None:
                try:
                    poses = pose.detect(img)
                except Exception as e:
                    r.error = f"pose:{e}"
                    poses = []
                if poses:
                    best = max(
                        poses,
                        key=lambda p: _iou(p.box_xyxy, target.box_xyxy),
                    )
                    if _iou(best.box_xyxy, target.box_xyxy) >= 0.2:
                        r.kp_visible = best.visible_count(cfg.kp_vis_thres)
                        if r.kp_visible >= cfg.kp_visible_min:
                            r.buckets.append(BUCKET_KEYPOINT)

        # 规则 4：前备箱（路径关键字 + person 命中即可）
        if path_matches_front_camera(rel, cfg.front_keywords):
            r.buckets.append(BUCKET_FRUNK)

    # 通用 embedding：对每个"有样例"的桶都算一次相似度，超阈值即加桶
    if embed is not None:
        try:
            matches = embed.all_matches(img)
        except Exception as e:
            r.error = (r.error + ";" if r.error else "") + f"embed:{e}"
            matches = {}
        r.embed_matches = matches
        for bucket_name, (sim, _sample) in matches.items():
            if bucket_name not in BUCKET_NAMES:
                # 用户在 rules/ 下自建了新桶名？跳过，避免写错目录
                continue
            thres = cfg.embed_sim_per_bucket.get(
                bucket_name, cfg.embed_sim_default
            )
            if sim >= thres and bucket_name not in r.buckets:
                r.buckets.append(bucket_name)

    return r


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _parse_bucket_thres(pairs: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pair in pairs:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


def _find_camera_dirs(
    in_root: Path,
    camera_name: str,
    log=None,
) -> list[Path]:
    """递归找所有名为 camera_name 的目录。
    - 用 pathlib 的 .name 兼容 Windows 正/反斜杠混排
    - 匹配不区分大小写，防止 Camera/CAMERA 漏掉
    - 前 20 层目录打调试日志，便于排查 "扫不到" 的疑难杂症
    """
    result: list[Path] = []
    target = camera_name.lower()
    walked = 0
    for dirpath, dirs, _files in os.walk(in_root):
        walked += 1
        name = Path(dirpath).name
        if walked <= 20 and log is not None:
            log(f"  [walk] {dirpath}  name={name!r}  subdirs={dirs[:5]}")
        if name.lower() == target:
            result.append(Path(dirpath))
            dirs[:] = []
    if log is not None:
        log(f"  [walk] 总共遍历 {walked} 个目录，命中 {len(result)} 个")
    return result


def process_camera_dir(
    camera_in: Path,
    cfg: ClassifyConfig,
    yolo, pose, embed,
    stats: Stats,
    writer,
    log: LogFn,
    cancel: CancelFn | None,
) -> None:
    rel_camera = camera_in.relative_to(cfg.in_root)
    camera_out = cfg.out_root / rel_camera
    camera_out.mkdir(parents=True, exist_ok=True)
    log(f"[camera] {camera_in} → {camera_out}")

    for sub in sorted(p for p in camera_in.iterdir() if p.is_dir()):
        if cancel and cancel():
            return
        sub_name = sub.name
        if sub_name in BUCKET_NAMES:
            continue
        if _has_keyword(sub_name, cfg.filter_keywords):
            stats.skipped_filter += 1
            log(f"  [跳过] {sub_name}（命中过滤关键字）")
            continue
        _process_sub_dir(
            sub, camera_in, camera_out, cfg,
            yolo, pose, embed, stats, writer, log, cancel,
        )


def _process_sub_dir(
    sub: Path,
    camera_in: Path,
    camera_out: Path,
    cfg: ClassifyConfig,
    yolo, pose, embed,
    stats: Stats,
    writer,
    log: LogFn,
    cancel: CancelFn | None,
) -> None:
    for img in _iter_images(sub, cfg.image_extensions):
        if cancel and cancel():
            return
        if cfg.limit and stats.scanned >= cfg.limit:
            return
        stats.scanned += 1

        rel_in = img.relative_to(cfg.in_root)
        rel_to_camera = img.relative_to(camera_in)

        # 1) 镜像复制
        try:
            _copy_overwrite(img, camera_out / rel_to_camera)
            stats.copied_original += 1
        except Exception as e:
            stats.errors += 1
            log(f"  [错误] 镜像复制失败 {img}: {e}")
            continue

        # 2) 分类
        r = classify_one(img, rel_in, yolo, pose, embed, cfg)
        if r.error:
            stats.errors += 1
        if BUCKET_LIVENESS in r.buckets:
            stats.liveness += 1
        if BUCKET_KEYPOINT in r.buckets:
            stats.keypoint += 1
        if BUCKET_FRUNK in r.buckets:
            stats.frunk += 1
        if BUCKET_HOOD in r.buckets:
            stats.hood += 1
        if BUCKET_OCCLUSION in r.buckets:
            stats.occlusion += 1

        for bucket in r.buckets:
            dst = _bucket_path_for(bucket, camera_out) / rel_to_camera
            try:
                _copy_overwrite(img, dst)
                stats.copied_bucket += 1
            except Exception as e:
                stats.errors += 1
                log(f"  [错误] 桶复制失败 {img} -> {dst}: {e}")

        if writer is not None:
            embed_str = ";".join(
                f"{b}:{sim:.2f}({name})"
                for b, (sim, name) in sorted(r.embed_matches.items())
            )
            writer.writerow([
                str(rel_in), "|".join(r.buckets),
                f"{r.person_conf:.3f}", r.vehicle_hit,
                r.kp_visible,
                embed_str,
                r.error,
            ])

        if stats.scanned % 200 == 0:
            log(
                f"  [进度] {stats.scanned} 张  "
                f"活体={stats.liveness} 关节={stats.keypoint} "
                f"前备箱={stats.frunk} 前机盖={stats.hood} "
                f"遮挡={stats.occlusion}"
            )


def run(
    cfg: ClassifyConfig,
    log: LogFn = _default_log,
    cancel: CancelFn | None = None,
) -> Stats:
    if not cfg.in_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在: {cfg.in_root}")
    cfg.out_root.mkdir(parents=True, exist_ok=True)

    from detector import YoloDetector, resolve_model_path
    from pose_detector import PoseDetector, resolve_pose_model_path

    yolo_path = resolve_model_path(cfg.yolo_model)
    if yolo_path is None:
        raise FileNotFoundError("找不到 yolov8n.onnx")
    log(f"[模型] YOLO: {yolo_path}")
    yolo = YoloDetector(yolo_path, conf_thres=cfg.person_conf)

    pose_path = resolve_pose_model_path(cfg.pose_model)
    if pose_path is None:
        log("[WARN] 找不到 yolov8n-pose.onnx，规则 2 将跳过")
        pose = None
    else:
        log(f"[模型] POSE: {pose_path}")
        pose = PoseDetector(pose_path, conf_thres=cfg.person_conf)

    # 少样本 embed（找不到样例或模型就跳过规则 5）
    embed = None
    if cfg.rules_dir is not None and cfg.rules_dir.is_dir():
        try:
            from embed_detector import EmbedMatcher, resolve_embed_model_path
            emb_path = resolve_embed_model_path(cfg.embed_model)
            if emb_path is None:
                log("[WARN] 找不到 mobilenetv3_embed.onnx，规则 5 跳过")
            else:
                log(f"[模型] EMBED: {emb_path}")
                embed = EmbedMatcher(emb_path, cfg.rules_dir)
                embed.load_prototypes(log=log)
        except Exception as e:
            log(f"[WARN] embed 初始化失败: {e}，规则 5 跳过")
            embed = None
    else:
        log("[提示] 未提供 rules 目录，embedding 匹配全部跳过")

    camera_dirs = _find_camera_dirs(cfg.in_root, cfg.camera_dir_name, log=log)
    log(f"[扫描] 找到 {len(camera_dirs)} 个 {cfg.camera_dir_name}/ 目录")
    if not camera_dirs:
        return Stats()

    stats = Stats()
    start = time.time()

    report_path = cfg.report_path or (cfg.out_root / "classify_report.csv")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rel_path", "buckets",
            "person_conf", "vehicle",
            "kp_visible",
            "embed_matches",
            "error",
        ])
        for cam in camera_dirs:
            if cancel and cancel():
                log("[取消] 收到停止信号")
                break
            process_camera_dir(
                cam, cfg, yolo, pose, embed, stats, writer, log, cancel,
            )
            if cfg.limit and stats.scanned >= cfg.limit:
                log(f"[限制] 已达 --limit={cfg.limit}")
                break

    elapsed = time.time() - start
    log("=" * 60)
    log(f"[完成] 扫描 {stats.scanned} 张 耗时 {elapsed:.1f}s")
    log(f"  规则 1 舱外活体   : {stats.liveness}")
    log(f"  规则 2 人体关键点 : {stats.keypoint}")
    log(f"  规则 4 前备箱防夹 : {stats.frunk}")
    log(f"  规则 5 前机盖     : {stats.hood}")
    log(f"  遮挡              : {stats.occlusion}")
    log(f"  镜像复制 / 桶复制 : {stats.copied_original} / {stats.copied_bucket}")
    log(f"  过滤跳过 / 错误   : {stats.skipped_filter} / {stats.errors}")
    log(f"  报告              : {report_path}")
    log("=" * 60)
    return stats


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="对去重后的图片二次分类：舱外活体 / 关键点 / 前备箱 / 前机盖"
    )
    p.add_argument("--in-root", required=True, type=Path)
    p.add_argument("--out-root", required=True, type=Path)
    p.add_argument("--camera-name", type=str, default="camera")
    p.add_argument("--filter-keywords", type=str, default="")
    p.add_argument("--front-keywords", type=str,
                   default=",".join(DEFAULT_FRONT_KEYWORDS))
    p.add_argument("--extensions", type=str,
                   default=",".join(DEFAULT_IMAGE_EXT))
    p.add_argument("--model", type=str, default=None)
    p.add_argument("--pose-model", type=str, default=None)
    p.add_argument("--embed-model", type=str, default=None)
    p.add_argument("--rules-dir", type=Path, default=None,
                   help="少样本样例根目录，例如 rules/")
    p.add_argument("--person-conf", type=float, default=0.3)
    p.add_argument("--person-area", type=float, default=0.005)
    p.add_argument("--vehicle-iou", type=float, default=0.05)
    p.add_argument("--kp-visible-min", type=int, default=8)
    p.add_argument("--kp-vis-thres", type=float, default=0.4)
    p.add_argument("--embed-sim-default", type=float, default=0.75,
                   help="embedding 相似度阈值（所有桶的默认值）")
    p.add_argument("--embed-sim-bucket", action="append", default=[],
                   help="某个桶的单独阈值：--embed-sim-bucket 遮挡=0.7 可多次")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--skip-license", action="store_true",
                   help="调试用：跳过 license 校验")
    return p.parse_args()


def _check_license_or_die() -> None:
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        print(f"[FATAL] 无法加载 licensing 模块: {e}", file=sys.stderr)
        sys.exit(2)

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
    print("=" * 60)
    sys.exit(3)


def main() -> int:
    if "--fingerprint" in sys.argv:
        try:
            from licensing import get_fingerprint
            print(get_fingerprint())
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return 2
        return 0

    if "--skip-license" not in sys.argv:
        _check_license_or_die()
    a = parse_args()
    cfg = ClassifyConfig(
        in_root=a.in_root.resolve(),
        out_root=a.out_root.resolve(),
        camera_dir_name=a.camera_name,
        filter_keywords=tuple(
            s.strip() for s in a.filter_keywords.split(",") if s.strip()
        ),
        front_keywords=tuple(
            s.strip() for s in a.front_keywords.split(",") if s.strip()
        ),
        image_extensions=tuple(
            s.strip().lower().lstrip(".") for s in a.extensions.split(",") if s.strip()
        ),
        yolo_model=a.model,
        pose_model=a.pose_model,
        embed_model=a.embed_model,
        rules_dir=a.rules_dir.resolve() if a.rules_dir else None,
        person_conf=a.person_conf,
        person_area_ratio=a.person_area,
        vehicle_iou_thres=a.vehicle_iou,
        kp_visible_min=a.kp_visible_min,
        kp_vis_thres=a.kp_vis_thres,
        embed_sim_default=a.embed_sim_default,
        embed_sim_per_bucket=_parse_bucket_thres(a.embed_sim_bucket),
        limit=a.limit,
        report_path=a.report,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
