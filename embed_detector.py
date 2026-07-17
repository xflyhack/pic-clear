# -*- coding: utf-8 -*-
"""
embed_detector.py — 少样本图像 embedding 匹配 + 磁盘缓存（自动增量）。

用法：
  rules/
    舱外活体检测/*.jpg
    人体关键点/*.jpg
    前机盖开关检测/*.jpg
    遮挡/*.jpg
    ...

启动时对样例算 embedding 并缓存到 rules/.embed_cache.json：
  - 缓存 key = (相对路径, size, mtime_ns) —— 文件不变就复用
  - 新增文件 → 只算新的
  - 删除文件 → 从缓存剔除
  - 强制重算：手动删 .embed_cache.json 即可（GUI 会加按钮）

模型：MobileNetV3-Small 去分类头，输出 576 维（export_onnx.py 生成）
"""

from __future__ import annotations

import base64
import json
import sys
import time
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


IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
SUPPORTED_EXT = {"jpg", "jpeg", "png", "bmp", "webp"}
CACHE_FILENAME = ".embed_cache.json"
CACHE_VERSION = 1


def _preprocess(image_path: str | Path, size: int = 224) -> np.ndarray | None:
    try:
        with _pil_open(image_path) as im:
            im = im.convert("RGB").resize((size, size), Image.BILINEAR)
            arr = np.asarray(im, dtype=np.float32) / 255.0
    except Exception:
        return None
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    return arr.transpose(2, 0, 1)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _encode_emb(arr: np.ndarray) -> str:
    return base64.b64encode(arr.astype(np.float32).tobytes()).decode("ascii")


def _decode_emb(s: str, dim: int) -> np.ndarray:
    raw = base64.b64decode(s.encode("ascii"))
    return np.frombuffer(raw, dtype=np.float32).reshape(dim)


class EmbedMatcher:
    """按桶（rules/<桶名>/）加载样例，缓存到 rules/.embed_cache.json。"""

    def __init__(self, model_path: str | Path, rules_root: Path) -> None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.rules_root = Path(rules_root)
        # bucket_name -> list[(sample_name, embedding)]
        self.prototypes: dict[str, list[tuple[str, np.ndarray]]] = {}
        self._emb_dim = 0
        self.cache_path = self.rules_root / CACHE_FILENAME

    # -------------------------------------------------- 单图 embedding
    def embed_one(self, image_path: str | Path) -> np.ndarray | None:
        pre = _preprocess(image_path)
        if pre is None:
            return None
        blob = pre[None, ...]
        out = self.session.run(None, {self.input_name: blob})[0][0].astype(np.float32)
        if self._emb_dim == 0:
            self._emb_dim = int(out.shape[0])
        return out

    # -------------------------------------------------- 缓存 IO
    def _load_cache(self) -> dict:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != CACHE_VERSION:
                return {}
            return data.get("items", {})
        except Exception:
            return {}

    def _save_cache(self, items: dict) -> None:
        payload = {
            "version": CACHE_VERSION,
            "dim": self._emb_dim,
            "items": items,
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            tmp.replace(self.cache_path)
        except Exception as e:
            # 缓存不是关键路径，写失败只警告
            print(f"[embed] 缓存写入失败: {e}", flush=True)

    # -------------------------------------------------- 载入 / 增量
    def load_prototypes(self, log=print, force_rebuild: bool = False) -> dict:
        """扫描 rules_root 下每个桶，加载样例的 embedding。

        返回统计信息：{bucket: {"total": n, "reused": r, "new": k, "removed": d}}
        """
        self.prototypes = {}
        stats: dict[str, dict[str, int]] = {}
        if not self.rules_root.is_dir():
            log(f"[embed] rules 目录不存在: {self.rules_root}")
            return stats

        old_cache = {} if force_rebuild else self._load_cache()
        new_cache: dict[str, dict] = {}
        t0 = time.time()

        for bucket_dir in sorted(p for p in self.rules_root.iterdir() if p.is_dir()):
            bucket_name = bucket_dir.name
            samples: list[tuple[str, np.ndarray]] = []
            reused = new = 0

            for f in sorted(bucket_dir.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower().lstrip(".") not in SUPPORTED_EXT:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                rel_key = f"{bucket_name}/{f.name}"
                cache_key = f"{rel_key}::{st.st_size}::{st.st_mtime_ns}"

                cached = old_cache.get(cache_key)
                if cached and "emb" in cached and "dim" in cached:
                    try:
                        emb = _decode_emb(cached["emb"], int(cached["dim"]))
                        samples.append((f.name, emb))
                        new_cache[cache_key] = cached
                        if self._emb_dim == 0:
                            self._emb_dim = int(cached["dim"])
                        reused += 1
                        continue
                    except Exception:
                        pass

                emb = self.embed_one(f)
                if emb is None:
                    log(f"[embed] 跳过样例 {f}（读图失败）")
                    continue
                samples.append((f.name, emb))
                new_cache[cache_key] = {
                    "emb": _encode_emb(emb),
                    "dim": int(emb.shape[0]),
                }
                new += 1

            if samples:
                self.prototypes[bucket_name] = samples
                stats[bucket_name] = {
                    "total": len(samples), "reused": reused, "new": new,
                    "removed": 0,
                }
            else:
                stats[bucket_name] = {
                    "total": 0, "reused": 0, "new": 0, "removed": 0,
                }

        # 计算删除数量（老缓存里有、新扫描没保留的）
        removed_total = len(old_cache) - sum(
            1 for k in new_cache if k in old_cache
        )
        if removed_total > 0:
            log(f"[embed] 从缓存剔除 {removed_total} 个已删除样例")

        self._save_cache(new_cache)

        elapsed = time.time() - t0
        for name, s in stats.items():
            if s["total"] == 0:
                log(f"[embed] 桶 [{name}] 无样例，跳过")
            else:
                log(
                    f"[embed] 桶 [{name}] {s['total']} 张 "
                    f"(复用 {s['reused']} / 新算 {s['new']})"
                )
        log(f"[embed] 样例加载完成，耗时 {elapsed:.1f}s")
        return stats

    # -------------------------------------------------- 匹配
    def best_match(
        self, image_path: str | Path, bucket_name: str,
        precomputed_emb: np.ndarray | None = None,
    ) -> tuple[float, str]:
        samples = self.prototypes.get(bucket_name)
        if not samples:
            return 0.0, ""
        emb = precomputed_emb if precomputed_emb is not None else self.embed_one(image_path)
        if emb is None:
            return 0.0, ""
        best_sim = -1.0
        best_name = ""
        for name, proto in samples:
            sim = _cosine(emb, proto)
            if sim > best_sim:
                best_sim = sim
                best_name = name
        return max(best_sim, 0.0), best_name

    def all_matches(
        self, image_path: str | Path,
        precomputed_emb: np.ndarray | None = None,
    ) -> dict[str, tuple[float, str]]:
        """对所有加载了样例的桶都算一次 best_match，返回 {bucket: (sim, sample_name)}。"""
        if not self.prototypes:
            return {}
        emb = precomputed_emb if precomputed_emb is not None else self.embed_one(image_path)
        if emb is None:
            return {}
        out: dict[str, tuple[float, str]] = {}
        for bucket_name, samples in self.prototypes.items():
            best_sim = -1.0
            best_name = ""
            for name, proto in samples:
                sim = _cosine(emb, proto)
                if sim > best_sim:
                    best_sim = sim
                    best_name = name
            out[bucket_name] = (max(best_sim, 0.0), best_name)
        return out

    def loaded_buckets(self) -> list[str]:
        return sorted(self.prototypes.keys())

    def bucket_sample_count(self, bucket_name: str) -> int:
        return len(self.prototypes.get(bucket_name, []))


def resolve_embed_model_path(user_path: str | None) -> Path | None:
    candidates: list[Path] = []
    if user_path:
        candidates.append(Path(user_path))
    try:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(exe_dir / "mobilenetv3_embed.onnx")
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "mobilenetv3_embed.onnx")
    candidates.append(Path.cwd() / "mobilenetv3_embed.onnx")
    for c in candidates:
        if c.is_file():
            return c
    return None
