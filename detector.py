# -*- coding: utf-8 -*-
"""
YOLOv8 ONNX 目标检测（纯 onnxruntime + numpy + Pillow 实现）
用于识别"含有需要保护类别（人 / 车 / 电车 等）"的图片，防止被去重误删。

模型：YOLOv8n，输入 (1, 3, 640, 640)，输出 (1, 84, 8400)，
     其中 84 = 4 (cx,cy,w,h) + 80 (COCO 类别分)。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# ----- Z:/映射盘符 → \\server\share 缓存 (Windows only) -----
_MAPPED_DRIVE_CACHE: dict[str, str | None] = {}


def _resolve_mapped_drive_to_unc(drive_letter: str) -> str | None:
    r"""把 'Z:' 这样的映射盘符解析为底层 UNC (如 \\server\share).

    - 只在 Windows 上有效, 其他平台返回 None
    - 结果按盘符字母 (大写) 缓存到进程内, 反复调用零开销
    - 非映射盘 / 本地盘 / 解析失败 -> 返回 None (调用方按原盘符处理)

    背景: Win32 的 \\?\ 前缀要求路径是"已归一化的 NT 命名空间", 而映射盘符
          在超过 MAX_PATH 时会绕过 DOS 设备解析层, 导致 \\?\Z:\... 直通失败.
          正确写法是 \\?\UNC\server\share\..., 因此先展开一次.
    """
    if os.name != "nt":
        return None
    key = drive_letter.upper().rstrip("\\/")
    if not (len(key) == 2 and key[1] == ":"):
        return None
    if key in _MAPPED_DRIVE_CACHE:
        return _MAPPED_DRIVE_CACHE[key]
    unc: str | None = None
    try:
        import ctypes
        from ctypes import wintypes

        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        WNetGetConnectionW = mpr.WNetGetConnectionW
        WNetGetConnectionW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        WNetGetConnectionW.restype = wintypes.DWORD

        buf_len = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        rc = WNetGetConnectionW(key, buf, ctypes.byref(buf_len))
        if rc == 0:
            val = buf.value.strip()
            if val.startswith("\\\\"):
                unc = val
    except Exception:
        unc = None
    _MAPPED_DRIVE_CACHE[key] = unc
    return unc


def _to_long_path(image_path) -> str:
    r"""Windows 上 >= 200 字符的绝对路径转成 \\?\ 前缀, 绕开 MAX_PATH=260 限制.

    - 非 Windows 原样返回, 保证 mac / Linux / 本地虚拟机零回归
    - 已带 \\?\ 或 \?\ 前缀原样返回, 不重复加
    - 短于 200 字符原样返回 (PIL 观察在 200 附近就会开始翻车,
      比 MAX_PATH=260 更保守, 换取更少的黑箱失败)
    - UNC 路径 (\\server\share\...) 转成 \\?\UNC\server\share\...
    - 映射盘符 (Z:\...) 先展开成 \\server\share\... 再套 \\?\UNC\ 前缀,
      纯本地盘 (D:\...) 保持 \\?\D:\... 不变.
    - 相对路径先用 os.path.abspath 转绝对路径再套前缀
    """
    s = str(image_path)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\") or s.startswith("\\?\\"):
        return s
    if len(s) < 200:
        return s
    # UNC 已经是 \\server\share\... 就直接套前缀, 不走 abspath
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    try:
        abs_s = os.path.abspath(s)
    except Exception:
        abs_s = s
    if abs_s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_s.lstrip("\\")
    # 到这里 abs_s 形如 'X:\\...', 若 X: 是映射盘则展开成 UNC
    if len(abs_s) >= 2 and abs_s[1] == ":":
        unc_root = _resolve_mapped_drive_to_unc(abs_s[:2])
        if unc_root:
            rest = abs_s[2:].lstrip("\\")
            unc_full = unc_root.rstrip("\\") + "\\" + rest
            return "\\\\?\\UNC\\" + unc_full.lstrip("\\")
    return "\\\\?\\" + abs_s


def _pil_open(image_path):
    """PIL Image.open 的长路径安全版本, 与 dedupe_pic._pil_open 语义一致."""
    return Image.open(_to_long_path(image_path))

# COCO 80 类
COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

DEFAULT_PROTECT_CLASSES = frozenset({
    "person", "bicycle", "car", "motorcycle", "bus", "train", "truck",
})

# 用于"前后帧车变化"判定：认为下面这些类别属于"车辆"
VEHICLE_CLASSES = frozenset({
    "bicycle", "car", "motorcycle", "bus", "train", "truck",
})


# =============================================================================
#  场景异常检测（不依赖 YOLO / onnxruntime）
#
#  背景：切帧图偶尔出现"YOLO 什么都识别不到但明显不该删"的异常帧，例如：
#    - 传感器故障 / 大面积遮挡产生的纯色或渐变屏（整张几乎一片同色）
#
#  这些图纯 dHash 会把它们聚成一组只留一张，其余被删。业务希望"宁多留
#  勿多删"，因此提供一个廉价的场景分析：只对 YOLO 空手回来的图调用，
#  命中即保护，不影响正常带人/车的图。
#
#  目前仅覆盖"纯色屏 / 渐变屏"(mono)。其他类型（引擎盖打开、复杂遮挡）
#  样本不足，暂不判定，避免误伤正常无主体帧（空路面、纯天空、树影特写等）。
# =============================================================================


@dataclass
class SceneFlags:
    is_anomaly: bool
    reason: str
    metrics: dict  # 便于调参/排查；CSV 里只落 reason


def analyze_scene(
    image_path: str | Path,
    *,
    edge_flat: float = 3.0,
    sat_high: float = 0.5,
    hue_top_high: float = 0.6,
    resize_to: int = 128,
) -> SceneFlags:
    """判定单张图是否属于"纯色/渐变屏"（应保护、不删）。

    命中任一即视为异常：
      A) mono_flat  全图平均边缘 edge_mean < edge_flat  （几乎无纹理）
      B) mono_color 饱和度均值 > sat_high 且 色相直方峰值占比 > hue_top_high
                    （高饱和且色相集中——大片同色/近同色）

    调用方约定：只对 YOLO 无 person/vehicle 命中的图调用，避免误伤正常场景。
    """
    try:
        with _pil_open(image_path) as im:
            im = im.convert("RGB")
            w0, h0 = im.size
            if w0 <= 0 or h0 <= 0:
                return SceneFlags(False, "", {})
            r = min(resize_to / w0, resize_to / h0, 1.0)
            if r < 1.0:
                nw = max(1, int(round(w0 * r)))
                nh = max(1, int(round(h0 * r)))
                im = im.resize((nw, nh), Image.BILINEAR)
            arr = np.asarray(im, dtype=np.float32)   # (H, W, 3)
            hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
    except Exception:
        return SceneFlags(False, "", {})

    h, w = arr.shape[:2]
    if h < 2 or w < 2:
        return SceneFlags(False, "", {})

    r_ch, g_ch, b_ch = arr[..., 0], arr[..., 1], arr[..., 2]
    gray = 0.299 * r_ch + 0.587 * g_ch + 0.114 * b_ch
    edge_mean = float(
        (np.abs(np.diff(gray, axis=0)).mean()
         + np.abs(np.diff(gray, axis=1)).mean()) / 2.0
    )

    mx = arr.max(-1)
    mn = arr.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    sat_mean = float(sat.mean())

    hue = hsv[..., 0]
    hist, _ = np.histogram(hue, bins=18, range=(0, 256))
    hue_top_ratio = float(hist.max() / max(hist.sum(), 1))

    metrics = {
        "edge_mean": edge_mean,
        "sat_mean": sat_mean,
        "hue_top_ratio": hue_top_ratio,
        "size": (w, h),
    }

    if edge_mean < edge_flat:
        return SceneFlags(True, "mono_flat", metrics)
    if sat_mean > sat_high and hue_top_ratio > hue_top_high:
        return SceneFlags(True, "mono_color", metrics)
    return SceneFlags(False, "", metrics)


def _iou(box_a: tuple[float, float, float, float],
         box_b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def vehicle_changed(
    prev_boxes: list[tuple[float, float, float, float]],
    curr_boxes: list[tuple[float, float, float, float]],
    image_size: tuple[int, int],
    motion_threshold: float = 0.05,
    iou_threshold: float = 0.5,
) -> tuple[bool, str]:
    """
    判定两帧之间车辆是否发生"变化"，命中任一即视为变化：
      1) 车数不同（含 0 vs N）
      2) 车数相同，但贪心 IoU 匹配后：
         - 有车无法匹配到 IoU >= iou_threshold 的对应车 → 变化
         - 或某辆车中心位移 > motion_threshold * max(W, H) → 变化
    返回 (是否变化, 原因描述)。image_size = (W, H)，来自 curr 帧。
    """
    n_prev, n_curr = len(prev_boxes), len(curr_boxes)
    if n_prev != n_curr:
        return True, f"count_changed({n_prev}->{n_curr})"
    if n_curr == 0:
        return False, "no_vehicle"

    w, h = image_size
    move_limit = motion_threshold * max(w, h)

    # 贪心 IoU 匹配：对每个 curr，从 prev 里挑 IoU 最大的未占用者
    used = [False] * n_prev
    for cb in curr_boxes:
        best_iou = -1.0
        best_j = -1
        for j, pb in enumerate(prev_boxes):
            if used[j]:
                continue
            iou = _iou(cb, pb)
            if iou > best_iou:
                best_iou = iou
                best_j = j
        if best_j < 0 or best_iou < iou_threshold:
            return True, f"iou_low({best_iou:.2f})"
        used[best_j] = True
        cx, cy = _center(cb)
        px, py = _center(prev_boxes[best_j])
        dist = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        if dist > move_limit:
            return True, f"moved({dist:.1f}px>{move_limit:.1f}px)"

    return False, "same"



@dataclass
class Detection:
    class_id: int
    class_name: str
    confidence: float
    box_xyxy: tuple[float, float, float, float]


def _letterbox(
    img: Image.Image, new_size: int = 640
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """把任意尺寸图片等比缩放并 padding 到 (new_size, new_size)，
    返回归一化 float32 张量 (3, H, W)、缩放比、pad(左, 上)。"""
    w0, h0 = img.size
    r = min(new_size / w0, new_size / h0)
    new_w = int(round(w0 * r))
    new_h = int(round(h0 * r))
    if (new_w, new_h) != (w0, h0):
        img = img.resize((new_w, new_h), Image.BILINEAR)
    canvas = Image.new("RGB", (new_size, new_size), (114, 114, 114))
    pad_x = (new_size - new_w) // 2
    pad_y = (new_size - new_h) // 2
    canvas.paste(img, (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)  # HWC -> CHW
    return arr, r, (pad_x, pad_y)


def _nms(
    boxes: np.ndarray, scores: np.ndarray, iou_thres: float
) -> list[int]:
    """经典 NMS，返回保留下来的下标列表。boxes: (N,4) xyxy。"""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = (x2 - x1).clip(min=0) * (y2 - y1).clip(min=0)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = (xx2 - xx1).clip(min=0) * (yy2 - yy1).clip(min=0)
        union = areas[i] + areas[rest] - inter
        iou = inter / np.where(union > 0, union, 1)
        order = rest[iou <= iou_thres]
    return keep


class YoloDetector:
    def __init__(
        self,
        model_path: str | Path,
        conf_thres: float = 0.35,
        iou_thres: float = 0.5,
        input_size: int = 640,
    ) -> None:
        import onnxruntime as ort  # 延迟导入，未启用检测时不用装

        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.input_size = input_size
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, image_path: str | Path) -> list[Detection]:
        try:
            with _pil_open(image_path) as im:
                im = im.convert("RGB")
                tensor, ratio, (pad_x, pad_y) = _letterbox(im, self.input_size)
        except Exception:
            return []

        blob = tensor[None, ...]  # (1, 3, H, W)
        outputs = self.session.run(None, {self.input_name: blob})
        # 输出 shape: (1, 84, 8400) —— YOLOv8 官方 export
        pred = outputs[0]
        if pred.ndim == 3 and pred.shape[1] == 84:
            pred = pred[0].transpose(1, 0)  # -> (8400, 84)
        elif pred.ndim == 3 and pred.shape[2] == 84:
            pred = pred[0]  # already (8400, 84)
        else:
            return []

        boxes_xywh = pred[:, :4]
        class_scores = pred[:, 4:]  # (8400, 80)
        class_ids = class_scores.argmax(axis=1)
        confidences = class_scores.max(axis=1)

        mask = confidences >= self.conf_thres
        if not mask.any():
            return []
        boxes_xywh = boxes_xywh[mask]
        class_ids = class_ids[mask]
        confidences = confidences[mask]

        # xywh -> xyxy
        cx, cy, w, h = (
            boxes_xywh[:, 0], boxes_xywh[:, 1],
            boxes_xywh[:, 2], boxes_xywh[:, 3],
        )
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

        # 逐类别 NMS
        results: list[Detection] = []
        for cls_id in np.unique(class_ids):
            idx = np.where(class_ids == cls_id)[0]
            keep = _nms(boxes_xyxy[idx], confidences[idx], self.iou_thres)
            for k in keep:
                gi = idx[k]
                bx = boxes_xyxy[gi]
                # 反 letterbox：减 pad，除 ratio
                bx_src = [
                    (bx[0] - pad_x) / ratio,
                    (bx[1] - pad_y) / ratio,
                    (bx[2] - pad_x) / ratio,
                    (bx[3] - pad_y) / ratio,
                ]
                results.append(
                    Detection(
                        class_id=int(cls_id),
                        class_name=COCO_NAMES[int(cls_id)],
                        confidence=float(confidences[gi]),
                        box_xyxy=tuple(bx_src),  # type: ignore[arg-type]
                    )
                )
        return results

    def has_protected(
        self, image_path: str | Path, protect: set[str]
    ) -> tuple[bool, list[Detection]]:
        dets = self.detect(image_path)
        hits = [d for d in dets if d.class_name in protect]
        return (len(hits) > 0, hits)

    def detect_full(
        self, image_path: str | Path, protect: set[str]
    ) -> tuple[bool, list[Detection], list[Detection], tuple[int, int] | None]:
        """
        一次调用返回全部信息，避免主脚本对同一张图重复推理：
          (是否含保护类别, 保护命中列表, 车辆命中列表, 图像尺寸 (W,H))
        """
        try:
            with _pil_open(image_path) as im:
                size = im.size
        except Exception:
            size = None
        dets = self.detect(image_path)
        hits = [d for d in dets if d.class_name in protect]
        vehicles = [d for d in dets if d.class_name in VEHICLE_CLASSES]
        return (len(hits) > 0, hits, vehicles, size)


def resolve_model_path(user_path: str | None) -> Path | None:
    """
    定位 yolov8n.onnx 的路径。查找顺序：
      1. --model 参数
      2. exe/脚本所在目录同级的 yolov8n.onnx
      3. PyInstaller onefile 展开的临时目录 sys._MEIPASS/yolov8n.onnx
      4. 当前工作目录 ./yolov8n.onnx
    """
    candidates: list[Path] = []
    if user_path:
        candidates.append(Path(user_path))

    try:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(exe_dir / "yolov8n.onnx")
    except Exception:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "yolov8n.onnx")

    candidates.append(Path.cwd() / "yolov8n.onnx")

    for c in candidates:
        if c.is_file():
            return c
    return None
