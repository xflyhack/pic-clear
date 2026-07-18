# -*- coding: utf-8 -*-
"""
classify_pic.py — 对**已经去重完**的图片目录做二次分类。

v1 规则（共 6 桶）：
  规则 1 舱外活体检测   —— YOLO 检出 person + 载具（bicycle/motorcycle）
                          [骑行 / 滑板 / 三轮上的人]
  规则 2 人体关键点     —— YOLO-pose 检出 person 且**同框无载具**，
                          关节可见数够 [步行 / 站立]
  规则 4 前备箱防夹     —— 路径含前视关键字 + person 命中即可（车内视角）
  规则 5 前机盖开关     —— 少样本 embedding 匹配（走 embed_detector）
  规则 3 动态手势       —— 少样本 embedding 匹配（走 embed_detector）
  规则 6 遮挡           —— 少样本 embedding 匹配（走 embed_detector）

输出目录结构（嵌套 = 目录组织，不代表包含关系）：

  camera/
  ├── 舱外活体检测/               ← 规则 1（互斥于规则 2）
  ├── 人体关键点/                  ← 规则 2
  │   ├── 前备箱防夹检测/          ← 规则 4
  │   └── 前机盖开关检测/          ← 规则 5
  ├── 动态手势/                    ← 规则 3（少样本 embedding）
  └── 遮挡/                        ← 规则 6（少样本 embedding，跟活体同级）

一张图命中多桶各复制一份（真 copy，覆盖同名）。
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import Callable, Iterable

# stats_db 可选; 打包时 hidden-import, 缺失不影响主流程
try:
    import stats_db as _stats_db  # type: ignore
except Exception:  # pragma: no cover
    _stats_db = None  # type: ignore


def _classify_version() -> str:
    try:
        from _version import VERSION
        return VERSION
    except Exception:
        return "dev"

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
BUCKET_GESTURE = "动态手势"            # 规则 3（少样本 embedding）
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
#  camera 目录级 lock & done marker（多机安全）
# ---------------------------------------------------------------------------
#  语义跟 dedupe_pic.py 的 _dedup.lock / _dedup_done.marker 对齐：
#  - marker_dir = markers_root / <camera 相对 in_root 的路径>
#  - 抢 _classify.lock（TTL 判断，超时自动抢占，多机共享盘安全）
#  - 跑完写 _classify_done.marker，加 --force / GUI"强制重跑"忽略

_CLASSIFY_LOCK_NAME = "_classify.lock"
_CLASSIFY_DONE_NAME = "_classify_done.marker"
_HOSTNAME = socket.gethostname()


def _classify_lock_payload() -> str:
    return f"{_HOSTNAME}|{os.getpid()}|{int(time.time())}"


def _classify_lock_is_stale(lock_path: Path, ttl_seconds: float) -> bool:
    try:
        content = lock_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return True
    parts = content.split("|")
    if len(parts) < 3:
        return True
    try:
        ts = int(parts[2])
    except Exception:
        return True
    return (time.time() - ts) > ttl_seconds


def _acquire_classify_lock(lock_path: Path, ttl_seconds: float) -> bool:
    """原子抢占 camera 级去分类锁。True=抢到、False=别人占用且未过期。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _classify_lock_payload().encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags, 0o644)
    except FileExistsError:
        if _classify_lock_is_stale(lock_path, ttl_seconds):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                return False
            try:
                fd = os.open(str(lock_path), flags, 0o644)
            except Exception:
                return False
        else:
            return False
    except Exception:
        return False
    try:
        os.write(fd, payload)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    return True


def _release_classify_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


class _NullLock:
    """占位锁，jobs=1 时 with 语句零开销。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
#  配置 & 结果
# ---------------------------------------------------------------------------


@dataclass
class ClassifyConfig:
    in_root: Path
    out_root: Path
    filter_keywords: tuple[str, ...] = ()          # 目录名包含即跳过整棵子树
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
    camera_dir_name: str = "camera"  # 分水岭目录名，精确匹配

    # ---- 多线程 + marker（v0.4.26） ----
    markers_root: Path | None = None    # marker/lock 集中存放的根（多机共享盘推荐）
    jobs: int = 1                       # camera 目录并发数（线程池）
    lock_ttl: int = 900                 # camera 锁 TTL 秒，超时可被别机抢占
    force_rerun: bool = False           # 忽略 _classify_done.marker


@dataclass
class Stats:
    scanned: int = 0
    liveness: int = 0
    keypoint: int = 0
    frunk: int = 0
    hood: int = 0
    gesture: int = 0
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
    """桶目录层级（v0.4.23 起）：
       舱外活体检测/
       舱外活体检测/人体关键点/
       舱外活体检测/人体关键点/前备箱防夹检测/
       舱外活体检测/人体关键点/动态手势/
       舱外活体检测/人体关键点/前机盖开关检测/
       遮挡/                    ← 与舱外活体检测同级
    """
    if rule == BUCKET_LIVENESS:
        return camera_out / BUCKET_LIVENESS
    if rule == BUCKET_KEYPOINT:
        return camera_out / BUCKET_LIVENESS / BUCKET_KEYPOINT
    if rule == BUCKET_FRUNK:
        return camera_out / BUCKET_LIVENESS / BUCKET_KEYPOINT / BUCKET_FRUNK
    if rule == BUCKET_HOOD:
        return camera_out / BUCKET_LIVENESS / BUCKET_KEYPOINT / BUCKET_HOOD
    if rule == BUCKET_GESTURE:
        return camera_out / BUCKET_LIVENESS / BUCKET_KEYPOINT / BUCKET_GESTURE
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


def _should_skip_dir(name: str, filter_keywords: tuple[str, ...]) -> bool:
    """目录名包含任一过滤关键字 → 跳过整棵子树。"""
    lower = name.lower()
    return any(k and k.lower() in lower for k in filter_keywords)


def _find_camera_dirs(in_root: Path, camera_name: str, filter_keywords: tuple[str, ...], log: LogFn) -> list[Path]:
    """在 in_root 下精确匹配名字 == camera_name 的所有目录（找到就不再往下钻）。"""
    hits: list[Path] = []
    for dirpath, dirs, _files in os.walk(str(in_root)):
        # 过滤子目录
        for d in list(dirs):
            if _should_skip_dir(d, filter_keywords):
                dirs.remove(d)
        if os.path.basename(dirpath) == camera_name:
            hits.append(Path(dirpath))
            dirs[:] = []   # 找到 camera 后不再深入，camera 之下由后续逻辑处理
            continue
    log(f"[扫描] 找到 {len(hits)} 个 {camera_name!r} 目录")
    return hits


def _resolve_camera_out(in_root: Path, out_root: Path, camera_dir: Path, same_root: bool) -> Path:
    """决定当前 camera 目录对应的输出根。
       - same_root=True：原地操作，输出就在 camera_dir 自己
       - same_root=False：out_root / <in_root 去盘符/UNC> / <camera 相对 in_root>
         例：in_root=Z:/切帧结果/测试/分类测试1  out_root=D:/test
             camera_dir=Z:/切帧结果/测试/分类测试1/sjbz.../camera
             → D:/test/切帧结果/测试/分类测试1/sjbz.../camera
    """
    if same_root:
        return camera_dir
    top = _in_root_top_segments(in_root)   # 切帧结果/测试/分类测试1
    try:
        rel = camera_dir.relative_to(in_root)   # sjbz.../camera
    except ValueError:
        rel = Path(camera_dir.name)
    return out_root / top / rel


def process_all(
    cfg: ClassifyConfig,
    yolo, pose, embed,
    stats: Stats,
    writer,
    log: LogFn,
    cancel: CancelFn | None,
) -> None:
    """v0.4.23 起：用 camera 目录做分水岭，只在 camera 下分类分桶。

    输出结构（相对每个 camera_out）：
      camera_out/舱外活体检测/<子目录>/*.jpg
      camera_out/舱外活体检测/人体关键点/<子目录>/*.jpg
      camera_out/舱外活体检测/人体关键点/前备箱防夹检测/<子目录>/*.jpg
      camera_out/舱外活体检测/人体关键点/动态手势/<子目录>/*.jpg
      camera_out/舱外活体检测/人体关键点/前机盖开关检测/<子目录>/*.jpg
      camera_out/遮挡/<子目录>/*.jpg
    """
    in_root = cfg.in_root
    same_root = str(cfg.in_root).strip() == str(cfg.out_root).strip()
    camera_dirs = _find_camera_dirs(in_root, cfg.camera_dir_name, cfg.filter_keywords, log)
    if not camera_dirs:
        log(f"[提示] 未在 {in_root} 下找到任何 {cfg.camera_dir_name!r} 目录")
        return

    # 计算每个 camera 的 marker_dir（markers_root 下的镜像位置）
    markers_root = cfg.markers_root
    tasks: list[tuple[Path, Path, Path | None]] = []
    for camera_dir in camera_dirs:
        camera_out = _resolve_camera_out(in_root, cfg.out_root, camera_dir, same_root)
        marker_dir: Path | None = None
        if markers_root is not None:
            try:
                rel = camera_dir.relative_to(in_root)
            except ValueError:
                rel = Path(camera_dir.name)
            marker_dir = markers_root / rel if str(rel) != "." else markers_root
        tasks.append((camera_dir, camera_out, marker_dir))

    # 过滤已完成 marker（除非 --force）
    if markers_root is not None and not cfg.force_rerun:
        pending: list[tuple[Path, Path, Path | None]] = []
        skipped_done = 0
        for camera_dir, camera_out, marker_dir in tasks:
            if marker_dir is not None and (marker_dir / _CLASSIFY_DONE_NAME).is_file():
                skipped_done += 1
                log(f"[跳过] {camera_dir} 已存在 {_CLASSIFY_DONE_NAME}（marker_dir={marker_dir}）")
                continue
            pending.append((camera_dir, camera_out, marker_dir))
        if skipped_done:
            log(f"[跳过] 合计 {skipped_done} 个 camera 已完成，如需重跑请勾选『强制重跑』或 --force")
        tasks = pending

    if not tasks:
        log("[提示] 所有 camera 目录都已有完成 marker，无需处理")
        return

    jobs = max(1, int(cfg.jobs or 1))
    ttl = max(30, int(cfg.lock_ttl or 900))
    io_lock: threading.Lock | None = threading.Lock() if jobs > 1 else None
    log_lock = threading.Lock()   # 保护 log 打印顺序，避免多线程日志交叉

    def _thread_log(msg: str) -> None:
        with log_lock:
            log(msg)

    def _run_one(task: tuple[Path, Path, Path | None]) -> None:
        camera_dir, camera_out, marker_dir = task
        if cancel and cancel():
            return
        # 落库用: 处理前 snapshot 累计计数, 结束后取 delta
        with (io_lock if io_lock is not None else _NullLock()):
            _snap = {
                "scanned": stats.scanned,
                "liveness": stats.liveness,
                "keypoint": stats.keypoint,
                "frunk": stats.frunk,
                "hood": stats.hood,
                "gesture": stats.gesture,
                "occlusion": stats.occlusion,
                "copied_bucket": stats.copied_bucket,
                "errors": stats.errors,
            }
        _t_cam_start = time.time()
        # marker/lock 抢占
        lock_path: Path | None = None
        if marker_dir is not None:
            marker_dir.mkdir(parents=True, exist_ok=True)
            lock_path = marker_dir / _CLASSIFY_LOCK_NAME
            if not _acquire_classify_lock(lock_path, ttl):
                _thread_log(f"[跳过] {camera_dir} 锁被占用且未过期（TTL={ttl}s），可能别机在跑")
                return
        _thread_log(f"[camera] {camera_dir} -> {camera_out}"
                    + (f"  marker={marker_dir}" if marker_dir is not None else ""))
        ok = False
        try:
            _process_one_camera(
                camera_dir, camera_out, cfg,
                yolo, pose, embed, stats, writer,
                _thread_log, cancel, io_lock=io_lock,
            )
            ok = True
        except Exception as e:
            _thread_log(f"[错误] {camera_dir} 处理异常: {e}")
        finally:
            if lock_path is not None:
                _release_classify_lock(lock_path)
            # 只有正常跑完 & 未被取消才写 done marker
            if ok and marker_dir is not None and not (cancel and cancel()):
                try:
                    (marker_dir / _CLASSIFY_DONE_NAME).write_text(
                        "done", encoding="utf-8"
                    )
                except Exception as e:
                    _thread_log(f"[警告] 写 {_CLASSIFY_DONE_NAME} 失败: {e}")
            # 落库 (无论成功失败都记一条, 静默失败)
            if _stats_db is not None:
                try:
                    with (io_lock if io_lock is not None else _NullLock()):
                        _bc = {
                            "活体": stats.liveness - _snap["liveness"],
                            "关节": stats.keypoint - _snap["keypoint"],
                            "前备箱": stats.frunk - _snap["frunk"],
                            "前机盖": stats.hood - _snap["hood"],
                            "手势": stats.gesture - _snap["gesture"],
                            "遮挡": stats.occlusion - _snap["occlusion"],
                        }
                        _scanned = stats.scanned - _snap["scanned"]
                        _copied = stats.copied_bucket - _snap["copied_bucket"]
                        _errs = stats.errors - _snap["errors"]
                    _stats_db.record_classify(
                        camera_dir=str(camera_dir),
                        scanned=int(_scanned),
                        copied_bucket=int(_copied),
                        bucket_counts=_bc,
                        errors=int(_errs),
                        elapsed_sec=time.time() - _t_cam_start,
                        exit_code=0 if ok else 1,
                        in_root=str(cfg.in_root),
                        out_root=str(cfg.out_root),
                        task_id=os.environ.get("PICCLEAR_TASK_ID") or None,
                        version=_classify_version(),
                    )
                except Exception:
                    pass

    if jobs == 1:
        for t in tasks:
            if cancel and cancel():
                break
            _run_one(t)
    else:
        log(f"[并发] 启动线程池 workers={jobs}，camera 目录粒度并发")
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="classify") as ex:
            futures = [ex.submit(_run_one, t) for t in tasks]
            for fut in futures:
                try:
                    fut.result()
                except Exception as e:
                    log(f"[异常] {type(e).__name__}: {e}")


def _process_one_camera(
    camera_dir: Path,
    camera_out: Path,
    cfg: ClassifyConfig,
    yolo, pose, embed,
    stats: Stats,
    writer,
    log: LogFn,
    cancel: CancelFn | None,
    io_lock: "threading.Lock | None" = None,
) -> None:
    """处理单个 camera 目录。多线程调用时传入 io_lock 保护 stats/writer。"""
    _noop_lock = _NullLock()
    lk = io_lock if io_lock is not None else _noop_lock

    in_root = camera_dir
    for dirpath, dirs, files in os.walk(in_root):
        if cancel and cancel():
            return
        # 就地修改 dirs，让 walk 跳过：
        # - 名字命中过滤关键字的子目录
        # - 输出桶目录（防止用户不小心把 out_root 放进了 in_root 循环处理）
        pruned = []
        for d in list(dirs):
            if d in BUCKET_NAMES:
                dirs.remove(d)
                continue
            if _should_skip_dir(d, cfg.filter_keywords):
                pruned.append(d)
                dirs.remove(d)
        if pruned:
            with lk:
                stats.skipped_filter += len(pruned)
            log(f"  [跳过] {Path(dirpath).relative_to(in_root)} 下过滤: {pruned}")

        # 处理这一层的图片
        for name in files:
            if cancel and cancel():
                return
            if cfg.limit:
                with lk:
                    reached = stats.scanned >= cfg.limit
                if reached:
                    return
            dot = name.rfind(".")
            if dot < 0:
                continue
            if name[dot + 1:].lower() not in {
                e.lower().lstrip(".") for e in cfg.image_extensions
            }:
                continue
            img = Path(dirpath) / name
            with lk:
                stats.scanned += 1

            rel_in = img.relative_to(in_root)

            # 分类判定（v0.4.23：不再做全量镜像复制，只按命中桶复制）
            r = classify_one(img, rel_in, yolo, pose, embed, cfg)
            with lk:
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
                if BUCKET_GESTURE in r.buckets:
                    stats.gesture += 1
                if BUCKET_OCCLUSION in r.buckets:
                    stats.occlusion += 1

            for bucket in r.buckets:
                dst = _bucket_path_for(bucket, camera_out) / rel_in
                try:
                    _copy_overwrite(img, dst)
                    with lk:
                        stats.copied_bucket += 1
                except Exception as e:
                    with lk:
                        stats.errors += 1
                    log(f"  [错误] 桶复制失败 {img} -> {dst}: {e}")

            if writer is not None:
                embed_str = ";".join(
                    f"{b}:{sim:.2f}({sample})"
                    for b, (sim, sample) in sorted(r.embed_matches.items())
                )
                with lk:
                    writer.writerow([
                        str(rel_in), "|".join(r.buckets),
                        f"{r.person_conf:.3f}", r.vehicle_hit,
                        r.kp_visible,
                        embed_str,
                        r.error,
                    ])

            with lk:
                should_log = (stats.scanned % 200 == 0)
                snapshot = (
                    stats.scanned, stats.liveness, stats.keypoint,
                    stats.frunk, stats.hood, stats.gesture, stats.occlusion,
                )
            if should_log:
                sc, lv, kp, fr, hd, gs, oc = snapshot
                log(
                    f"  [进度] {sc} 张  "
                    f"活体={lv} 关节={kp} "
                    f"前备箱={fr} 前机盖={hd} "
                    f"手势={gs} 遮挡={oc}"
                )



def _in_root_top_segments(in_root: Path) -> Path:
    """把输入根路径转成输出下的顶层子路径。
    去掉盘符/前导分隔符/UNC 主机段，保留有意义的目录名。
    例:
      Z:\\切帧结果\\测试\\分类测试1\\camera → Path("切帧结果/测试/分类测试1/camera")
      \\\\host\\share\\a\\b               → Path("a/b")
      /Users/xxx/data                → Path("Users/xxx/data")
    """
    raw = str(in_root).replace("\\", "/")
    # 去掉盘符 X:
    if len(raw) >= 2 and raw[1] == ":":
        raw = raw[2:]
    # 去掉 UNC \\host\share 或 //host/share 前两段
    parts = [p for p in raw.split("/") if p]
    if raw.startswith("//") or (len(parts) >= 2 and str(in_root).startswith("\\\\")):
        parts = parts[2:]  # 去掉 host + share
    if not parts:
        return Path(".")
    return Path(*parts)


def run(
    cfg: ClassifyConfig,
    log: LogFn = _default_log,
    cancel: CancelFn | None = None,
) -> Stats:
    in_root_str = str(cfg.in_root)
    # ---- 诊断日志（v0.4.22 新增，帮排查路径问题）----
    log(f"[诊断] 原始输入字符串 = {in_root_str!r}")
    try:
        log(f"[诊断] os.path.isdir  = {os.path.isdir(in_root_str)}")
    except Exception as _e:
        log(f"[诊断] os.path.isdir  报错: {_e}")
    try:
        log(f"[诊断] os.path.exists = {os.path.exists(in_root_str)}")
    except Exception as _e:
        log(f"[诊断] os.path.exists 报错: {_e}")
    try:
        _entries = os.listdir(in_root_str)
        log(f"[诊断] os.listdir     = {_entries[:5]}  (共 {len(_entries)} 项)")
    except Exception as _e:
        log(f"[诊断] os.listdir     报错: {_e}")
    # -----------------------------------------------
    if not os.path.isdir(in_root_str):
        if os.path.exists(in_root_str):
            log(f"[警告] isdir=False 但 exists=True，继续尝试遍历")
        else:
            raise FileNotFoundError(f"输入目录不存在: {in_root_str}")
    cfg.out_root.mkdir(parents=True, exist_ok=True)
    # 是否同目录原地操作
    same_root = str(cfg.in_root).strip() == str(cfg.out_root).strip()
    log(f"[模式] 输入==输出 : {same_root}  分水岭目录名: {cfg.camera_dir_name!r}")
    log(f"[模式] 并发数    : {cfg.jobs}  锁 TTL: {cfg.lock_ttl}s  强制重跑: {cfg.force_rerun}")
    if cfg.markers_root is not None:
        try:
            cfg.markers_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"无法创建 markers_root: {cfg.markers_root} ({e})")
        log(f"[模式] markers_root: {cfg.markers_root}")
    else:
        log("[模式] markers_root: (未设置，不写 lock / done marker)")

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

    log(f"[扫描] 从输入根开始遍历: {cfg.in_root}")

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
        process_all(cfg, yolo, pose, embed, stats, writer, log, cancel)
        if cfg.limit and stats.scanned >= cfg.limit:
            log(f"[限制] 已达 --limit={cfg.limit}")

    elapsed = time.time() - start
    log("=" * 60)
    log(f"[完成] 扫描 {stats.scanned} 张 耗时 {elapsed:.1f}s")
    log(f"  规则 1 舱外活体   : {stats.liveness}")
    log(f"  规则 2 人体关键点 : {stats.keypoint}")
    log(f"  规则 4 前备箱防夹 : {stats.frunk}")
    log(f"  规则 5 前机盖     : {stats.hood}")
    log(f"  规则 3 动态手势   : {stats.gesture}")
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
    p.add_argument("--camera-dir-name", type=str, default="camera",
                   help="分水岭目录名，精确匹配（默认 camera）")
    p.add_argument("--markers-root", type=Path, default=None,
                   help="marker/lock 集中存放目录（多机共享盘推荐）；"
                        "留空则不写 _classify.lock / _classify_done.marker")
    p.add_argument("--jobs", type=int, default=1,
                   help="camera 目录并发数（线程池），默认 1")
    p.add_argument("--lock-ttl", type=int, default=900,
                   help="_classify.lock TTL 秒，超时后可被别机抢占，默认 900")
    p.add_argument("--force", action="store_true",
                   help="忽略 _classify_done.marker，强制重跑")
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
        in_root=a.in_root,
        out_root=a.out_root,
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
        camera_dir_name=a.camera_dir_name,
        report_path=a.report,
        markers_root=a.markers_root.resolve() if a.markers_root else None,
        jobs=a.jobs,
        lock_ttl=a.lock_ttl,
        force_rerun=a.force,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
