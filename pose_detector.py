# -*- coding: utf-8 -*-
"""
YOLOv8n-pose ONNX 推理（纯 onnxruntime + numpy + Pillow）。

输出：每个检测到的 person 附带 17 个 COCO keypoint (x, y, visibility)。
风格与 detector.py 保持一致，复用其 _letterbox / _nms。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# 复用 detector 里的 Windows 长路径兼容 helper, 避免 PIL 在 MAX_PATH=260
# 附近 silently 打不开图片; detector 加载失败时退化为原生 Image.open.
try:
    from detector import _pil_open  # type: ignore
except Exception:  # pragma: no cover
    def _pil_open(_p):  # type: ignore
        return Image.open(_p)

from detector import _letterbox, _nms

# COCO 17 keypoints，索引 = 顺序
KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

# 判定"完整可标"用的核心关节：肩 / 髋 / 膝，每组左右任一可见即可
CORE_JOINT_GROUPS: tuple[tuple[int, int], ...] = (
    (5, 6),    # shoulders
    (11, 12),  # hips
    (13, 14),  # knees
)


@dataclass
class PoseDetection:
    confidence: float
    box_xyxy: tuple[float, float, float, float]
    keypoints: tuple[tuple[float, float, float], ...]  # (x, y, vis) * 17

    def visible_count(self, vis_thres: float = 0.5) -> int:
        return sum(1 for _, _, v in self.keypoints if v >= vis_thres)

    def has_core_joints(self, vis_thres: float = 0.5) -> bool:
        for group in CORE_JOINT_GROUPS:
            if not any(self.keypoints[i][2] >= vis_thres for i in group):
                return False
        return True


class PoseDetector:
    def __init__(
        self,
        model_path: str | Path,
        conf_thres: float = 0.4,
        iou_thres: float = 0.5,
        input_size: int = 640,
    ) -> None:
        import onnxruntime as ort  # 延迟导入
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

    def detect(self, image_path: str | Path) -> list[PoseDetection]:
        try:
            with _pil_open(image_path) as im:
                im = im.convert("RGB")
                tensor, ratio, (pad_x, pad_y) = _letterbox(im, self.input_size)
        except Exception:
            return []

        outputs = self.session.run(
            None, {self.input_name: tensor[None, ...]}
        )
        pred = outputs[0]
        # 官方导出 (1, 56, 8400)：4 box + 1 conf + 17*3 kpt
        if pred.ndim == 3 and pred.shape[1] == 56:
            pred = pred[0].transpose(1, 0)
        elif pred.ndim == 3 and pred.shape[2] == 56:
            pred = pred[0]
        else:
            return []

        boxes_xywh = pred[:, :4]
        conf = pred[:, 4]
        kpts_raw = pred[:, 5:]  # (N, 51)

        mask = conf >= self.conf_thres
        if not mask.any():
            return []
        boxes_xywh = boxes_xywh[mask]
        conf = conf[mask]
        kpts_raw = kpts_raw[mask]

        cx, cy, w, h = (
            boxes_xywh[:, 0], boxes_xywh[:, 1],
            boxes_xywh[:, 2], boxes_xywh[:, 3],
        )
        boxes_xyxy = np.stack(
            [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1
        )

        keep = _nms(boxes_xyxy, conf, self.iou_thres)
        results: list[PoseDetection] = []
        for k in keep:
            bx = boxes_xyxy[k]
            box_src = (
                (float(bx[0]) - pad_x) / ratio,
                (float(bx[1]) - pad_y) / ratio,
                (float(bx[2]) - pad_x) / ratio,
                (float(bx[3]) - pad_y) / ratio,
            )
            kp = kpts_raw[k].reshape(17, 3)
            kp_src = tuple(
                (
                    (float(kx) - pad_x) / ratio,
                    (float(ky) - pad_y) / ratio,
                    float(kv),
                )
                for kx, ky, kv in kp
            )
            results.append(PoseDetection(
                confidence=float(conf[k]),
                box_xyxy=box_src,
                keypoints=kp_src,
            ))
        return results


def resolve_pose_model_path(user_path: str | None) -> Path | None:
    """查找 yolov8n-pose.onnx，风格同 detector.resolve_model_path。"""
    candidates: list[Path] = []
    if user_path:
        candidates.append(Path(user_path))
    try:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(exe_dir / "yolov8n-pose.onnx")
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "yolov8n-pose.onnx")
    candidates.append(Path.cwd() / "yolov8n-pose.onnx")
    for c in candidates:
        if c.is_file():
            return c
    return None
