# -*- coding: utf-8 -*-
"""fps_rules.py — 按视频文件名规则决定抽帧 fps.

需求背景 (2026-07-23):
    抽帧默认 1fps, 但某些子集视频要用不同 fps. 例如文件名里含 DTSS 且
    末尾编号在 02~16 之间的视频用 30fps, 其余保持 1fps. 规则由用户在
    GUI 配置, 通过 --fps-rules <path> 传给 extract_frames.exe.

对外 API:
    load_fps_rules(path) -> FpsRuleSet
    resolve_fps_for_video(video_name, ruleset) -> (fps, hit_desc)
        hit_desc 形如 "dtss" (命中的规则关键字) 或 None (未命中, 用默认 fps)

JSON schema (给 CLI 的):
    {
      "default_fps": 1.0,
      "camera_regex": "camera(\\d+)",
      "rules": [
        {"keyword": "dtss",   "ids": ["02~16"],           "speed": 30.0},
        {"keyword": "cndtss", "ids": ["02","16","14~17"], "speed": 30.0}
      ]
    }

匹配语义:
    - 关键字 lower() 后 contains 匹配文件名 lower()
    - ids 支持 "02" (单值) 和 "02~16" / "camera02~camera16" (区间, 闭区间)
    - camera_regex 从文件名末尾取最后一次匹配的编号 (int)
    - ids 空列表 = 该关键字所有视频命中 (不看编号)
    - 多规则按顺序尝试, 首个命中生效
    - 关键字有命中但取不到编号且 ids 非空 -> 不命中 (保守)

模块不进 pyarmor gen (纯逻辑, 明文常量).
非 Windows 平台正常工作, 零平台特定.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CAMERA_REGEX = r"camera(\d+)"


@dataclass
class FpsRule:
    keyword: str
    ids: list[str] = field(default_factory=list)
    speed: float = 1.0


@dataclass
class FpsRuleSet:
    default_fps: float = 1.0
    camera_regex: str = DEFAULT_CAMERA_REGEX
    rules: list[FpsRule] = field(default_factory=list)

    @property
    def compiled_camera_regex(self) -> re.Pattern | None:
        if not self.camera_regex:
            return None
        try:
            return re.compile(self.camera_regex, re.IGNORECASE)
        except re.error:
            return None


# ------------------------------ 加载 -----------------------------------

def load_fps_rules(path: str | Path) -> FpsRuleSet:
    """从 JSON 文件加载规则. 解析失败抛异常, 由调用方决定 fatal / warn."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return parse_fps_rules(data)


def parse_fps_rules(data: Any) -> FpsRuleSet:
    """从 dict 结构解析. 允许两种输入:
      1) {"default_fps":..., "camera_regex":..., "rules":[...]} (CLI 内部格式)
      2) {"dtss":[...], "cndtss":[...], "speed":30}            (用户裸对象)
      3) {"camera_regex":..., "groups":[{...}, {...}]}         (导出格式)
    """
    if not isinstance(data, dict):
        raise ValueError(f"fps_rules 顶层必须是 dict, 收到 {type(data).__name__}")

    # 情况 1: 明确的 rules 列表
    if "rules" in data and isinstance(data["rules"], list):
        return _parse_flat(data)

    # 情况 3: groups 列表 (展开成 rules)
    if "groups" in data and isinstance(data["groups"], list):
        flat_rules: list[FpsRule] = []
        for g in data["groups"]:
            flat_rules.extend(_group_to_rules(g))
        return FpsRuleSet(
            default_fps=float(data.get("default_fps", 1.0)),
            camera_regex=str(data.get("camera_regex") or DEFAULT_CAMERA_REGEX),
            rules=flat_rules,
        )

    # 情况 2: 用户裸对象 (视为单 group)
    rules = _group_to_rules(data)
    return FpsRuleSet(
        default_fps=float(data.get("default_fps", 1.0)),
        camera_regex=str(data.get("camera_regex") or DEFAULT_CAMERA_REGEX),
        rules=rules,
    )


def _parse_flat(data: dict) -> FpsRuleSet:
    rules: list[FpsRule] = []
    for r in data.get("rules", []):
        if not isinstance(r, dict):
            continue
        kw = str(r.get("keyword", "")).strip()
        if not kw:
            continue
        ids_raw = r.get("ids") or []
        if not isinstance(ids_raw, list):
            raise ValueError(f"rule.ids 必须是 list, 收到 {type(ids_raw).__name__}")
        ids = [str(x).strip() for x in ids_raw if str(x).strip()]
        speed = float(r.get("speed", 1.0))
        if speed <= 0:
            raise ValueError(f"rule.speed 必须 > 0, 收到 {speed}")
        rules.append(FpsRule(keyword=kw, ids=ids, speed=speed))
    return FpsRuleSet(
        default_fps=float(data.get("default_fps", 1.0)),
        camera_regex=str(data.get("camera_regex") or DEFAULT_CAMERA_REGEX),
        rules=rules,
    )


def _group_to_rules(group: dict) -> list[FpsRule]:
    """把 {"dtss":[...], "cndtss":[...], "speed":30} 展成多条 FpsRule."""
    if not isinstance(group, dict):
        return []
    speed = float(group.get("speed", 1.0))
    if speed <= 0:
        raise ValueError(f"group.speed 必须 > 0, 收到 {speed}")
    reserved = {"speed", "camera_regex", "default_fps"}
    out: list[FpsRule] = []
    for k, v in group.items():
        if k in reserved:
            continue
        if not isinstance(v, list):
            continue
        ids = [str(x).strip() for x in v if str(x).strip()]
        out.append(FpsRule(keyword=str(k).strip(), ids=ids, speed=speed))
    return out


def dump_fps_rules(ruleset: FpsRuleSet) -> dict:
    """给 CLI 生成的 flat schema (给 GUI 存临时文件用)."""
    return {
        "default_fps": float(ruleset.default_fps),
        "camera_regex": ruleset.camera_regex or DEFAULT_CAMERA_REGEX,
        "rules": [
            {"keyword": r.keyword, "ids": list(r.ids), "speed": float(r.speed)}
            for r in ruleset.rules
        ],
    }


# ------------------------------ 匹配 -----------------------------------

def _keyword_hit(name_lower: str, kw_lower: str) -> bool:
    """按 word boundary 匹配关键字, 避免 cndtss 里的 dtss 被误命中.

    边界定义: 关键字前后必须是"非字母数字"或字符串端点.
    也就是说 _DTSS_ / DTSS. / -DTSS- / ^DTSS 都算命中,
    但 CnDTSS / predtsspost 不算.
    """
    if not kw_lower:
        return False
    pat = re.compile(
        rf"(?<![a-z0-9]){re.escape(kw_lower)}(?![a-z0-9])"
    )
    return pat.search(name_lower) is not None


_INT_RE = re.compile(r"(\d+)")


def _extract_int(token: str) -> int | None:
    """从 '02' / 'camera02' / 'cam-02' 里取整数, 找不到返回 None."""
    if not token:
        return None
    m = _INT_RE.search(token)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_camera_id(name: str, pattern: re.Pattern | None) -> int | None:
    """从视频名里取最后一次匹配的编号. 默认正则 camera(\\d+) 取末尾 cameraNN."""
    if pattern is None:
        return None
    matches = list(pattern.finditer(name))
    if not matches:
        return None
    try:
        return int(matches[-1].group(1))
    except (ValueError, IndexError):
        return None


def _match_ids(cam_id: int | None, specs: list[str]) -> bool:
    """判断编号是否命中 spec 列表. specs 空 = True (不看编号)."""
    if not specs:
        return True
    if cam_id is None:
        return False  # 有 spec 但取不到编号 -> 保守判不命中
    for s in specs:
        s = s.strip().lower()
        if not s:
            continue
        if "~" in s:
            a, b = s.split("~", 1)
            na, nb = _extract_int(a), _extract_int(b)
            if na is None or nb is None or na > nb:
                continue
            if na <= cam_id <= nb:
                return True
        else:
            m = _extract_int(s)
            if m is not None and cam_id == m:
                return True
    return False


def resolve_fps_for_video(
    video_name: str,
    ruleset: FpsRuleSet | None,
) -> tuple[float, str | None]:
    """按规则集给单个视频算 fps. 返回 (fps, hit_keyword_or_None).

    hit_keyword_or_None:
        None       -> 用了 default_fps
        "dtss"     -> 命中了关键字 dtss 的规则
    """
    if ruleset is None or not ruleset.rules:
        default = float(ruleset.default_fps) if ruleset else 1.0
        return default, None
    lower = (video_name or "").lower()
    cam_pat = ruleset.compiled_camera_regex
    cam_id = _extract_camera_id(lower, cam_pat)
    for r in ruleset.rules:
        kw = r.keyword.lower()
        if not kw:
            continue
        if not _keyword_hit(lower, kw):
            continue
        if _match_ids(cam_id, r.ids):
            return float(r.speed), r.keyword
    return float(ruleset.default_fps), None


# ------------------------------ 校验 (给 GUI 用) -----------------------------

def validate_ids_spec(spec: str) -> tuple[bool, str]:
    """校验一条 ids 字符串 (逗号分隔), 用于 GUI 编辑框实时反馈.

    返回 (ok, err_msg). ok=True 时 err_msg="".
    """
    if not spec or not spec.strip():
        return True, ""  # 空 = 关键字命中就算命中
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return True, ""
    for p in parts:
        if "~" in p:
            a, b = p.split("~", 1)
            na, nb = _extract_int(a), _extract_int(b)
            if na is None or nb is None:
                return False, f"区间 {p!r} 两端必须包含数字"
            if na > nb:
                return False, f"区间 {p!r} 起始 {na} > 结束 {nb}"
        else:
            if _extract_int(p) is None:
                return False, f"{p!r} 里找不到数字"
    return True, ""


def parse_ids_spec(spec: str) -> list[str]:
    """GUI 编辑框 -> 存到 rule.ids 的字符串数组 (逗号分隔转 list)."""
    if not spec or not spec.strip():
        return []
    return [p.strip() for p in spec.split(",") if p.strip()]


def format_ids_spec(ids: list[str]) -> str:
    """rule.ids -> GUI 编辑框显示 (逗号+空格分隔)."""
    return ", ".join(ids or [])
