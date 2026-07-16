# -*- coding: utf-8 -*-
"""
export_onnx.py — 一次性导出 3 个离线模型到项目根目录：

  1. yolov8n.onnx           —— 通用目标检测（复用 detector.py）
  2. yolov8n-pose.onnx      —— 人体 17 关键点（pose_detector.py）
  3. mobilenetv3_embed.onnx —— 图像 embedding（少样本原型用）

依赖：ultralytics + torch + torchvision + onnx
在**有网机器**上跑一次，产物提交或拷到堡垒机。
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def export_yolo(name: str, out: Path) -> None:
    from ultralytics import YOLO
    print(f"[YOLO] 下载 & 导出 {name} ...")
    model = YOLO(name)  # 会自动下载 .pt 到本地缓存
    tmp = model.export(format="onnx", imgsz=640, opset=12, simplify=False)
    src = Path(tmp)
    if src.resolve() != out.resolve():
        out.write_bytes(src.read_bytes())
    print(f"[YOLO] 已生成 {out}  ({out.stat().st_size / 1e6:.1f} MB)")


def export_mobilenet_embed(out: Path) -> None:
    """MobileNetV3-Small 去掉分类头，输出 576 维 embedding（用于余弦相似度）。"""
    import torch
    import torch.nn as nn
    from torchvision.models import (
        mobilenet_v3_small, MobileNet_V3_Small_Weights,
    )

    print("[EMBED] 下载 MobileNetV3-Small (imagenet 预训练) ...")
    weights = MobileNet_V3_Small_Weights.IMAGENET1K_V1
    m = mobilenet_v3_small(weights=weights)
    m.classifier = nn.Identity()  # 输出 features 展平后的 576 维
    m.eval()

    dummy = torch.randn(1, 3, 224, 224)
    print(f"[EMBED] 导出 ONNX -> {out}")
    torch.onnx.export(
        m, dummy, str(out),
        input_names=["input"], output_names=["embedding"],
        dynamic_axes={"input": {0: "batch"}, "embedding": {0: "batch"}},
        opset_version=12,
        dynamo=False,   # 强制内联权重，不要 .data 外部文件
    )
    print(f"[EMBED] 已生成 {out}  ({out.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    print(f"[路径] 输出目录: {ROOT}")
    export_yolo("yolov8n.pt", ROOT / "yolov8n.onnx")
    export_yolo("yolov8n-pose.pt", ROOT / "yolov8n-pose.onnx")
    export_mobilenet_embed(ROOT / "mobilenetv3_embed.onnx")
    print()
    print("=" * 60)
    print("[完成] 3 个 onnx 已生成，可以拷到堡垒机使用")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
