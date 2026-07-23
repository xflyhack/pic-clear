# 差异化抽帧率规则 (`--fps-rules` / GUI "差异化抽帧规则")

> **一句话**：老需求 —— 所有视频统一按 `--fps` 抽帧；
> **新需求 (v0.4.102)** —— 按视频文件名匹配"关键字 + 编号"，命中就用另一套 fps。

## 为什么加这个

抽帧默认 1fps 够大多数场景用，但少数子集 (`DTSS` / `CnDTSS` / `CWDTSS` 之类)
需要 30fps 精细抽帧。之前只能拆多次任务分批跑，容易混、容易漏。现在一次任务
里就能按规则自动分流。

## 数据模型

规则集 (`FpsRuleSet`) 三要素：

- `default_fps` (float)：未命中任何规则时的兜底 fps。**GUI 默认取 UI 上"抽帧率"输入框的值**。
- `camera_regex` (str)：从视频文件名里取编号的正则。默认 `camera(\d+)`。
  留空表示"不看编号，只看关键字"。
- `rules` (list)：一条条子规则，从上到下按顺序尝试，**首个命中生效**。

每条 rule：

- `keyword` (str)：文件名里要包含的关键字，大小写不敏感。**按 word boundary 匹配**
  (前后必须是非字母数字或字符串端点)，所以 `dtss` 只会命中 `_DTSS_` /
  `.DTSS.` 之类，不会误命中 `cndtss` 里的 `dtss`。
- `ids` (list[str])：编号规则。每个元素可以是：
    - 单值：`"02"` → 精确等于 2
    - 区间：`"02~16"` → 闭区间 [2, 16]
    - 带前缀：`"camera02~camera16"` → 等价于 `"02~16"`（数字前缀会被忽略比较）
    - 空列表 `[]` → 只要关键字命中就算命中，不看编号
- `speed` (float)：命中时使用的 fps。

## CLI schema (`--fps-rules <path>`)

```json
{
  "default_fps": 1.0,
  "camera_regex": "camera(\\d+)",
  "rules": [
    {"keyword": "dtss",   "ids": ["02~16"],           "speed": 30.0},
    {"keyword": "cndtss", "ids": ["02","16","14~17"], "speed": 30.0},
    {"keyword": "cwdtss", "ids": ["06~11"],           "speed": 30.0}
  ]
}
```

命令行：`sqFrameGrab.exe <src> <dst> --markers-root Z:\... --fps 1.0 --fps-rules rules.json`

- `--fps` 依然存在，作为兜底默认值
- `--fps-rules` 可选，不给就是老行为（全部按 `--fps`）
- 若 `--fps-rules` JSON 里显式写了 `default_fps`，会被 CLI **覆写成 `--fps` 的值**
  （统一以命令行为准，避免 GUI 用户改了 UI fps 但忘了同步 JSON 里的 default_fps）

## GUI 导入 / 导出 schema

GUI 导出为按 speed 聚合的 groups 格式（更贴近用户手写风格）：

```json
{
  "camera_regex": "camera(\\d+)",
  "groups": [
    {
      "dtss":   ["02~16"],
      "cndtss": ["02", "16", "14~17"],
      "cwdtss": ["06~11"],
      "speed":  30
    }
  ]
}
```

GUI 导入时**两种格式都吃**：`{rules: [...]}` 也行，`{groups: [...]}` 也行，
甚至用户直接手写的裸对象 `{"dtss": ["02~16"], "speed": 30}` 也能被识别成
单 group。

## 匹配算法

```
lower = video_name.lower()
cam_id = 最后一次 camera_regex.finditer(lower) 匹配到的 int (取不到=None)
for r in rules:
    if r.keyword.lower() word-boundary contains lower:
        if r.ids 为空 OR cam_id 在 r.ids 里:
            return (r.speed, r.keyword)
return (default_fps, None)
```

**边界**：

- 关键字有命中但取不到编号 (`cam_id=None`)
  - `r.ids == []` → 命中
  - `r.ids != []` → **不命中**（保守，避免误抽）
- `_leading_or_trailing_int("camera02")` 与 `_leading_or_trailing_int("02")` 都 = 2
- 规则之间**用户可拖拽调整顺序**，顺序即优先级
- 关键字用 word boundary，不会有子串误命中问题

## 落库 (stats_db)

抽帧完往 `extract_stats` / `extract_final` 各写一条，v0.4.102 新增两列：

- `fps_rule_hit` (TEXT)：命中的关键字（如 `dtss`），未命中为 NULL
- `fps_source` (TEXT)：`rule` 或 `default`

`fps` 列本身就是**该视频实际用的 fps**（不是 `--fps` 参数值），所以
`sqStatsViewerGui` 里看到的"抽帧 fps + 帧数"就是真实值，能一眼算出
`fps * duration_sec ≈ frames`。

老库自动迁移：`stats_db._ensure_columns` 检测缺列会 `ALTER TABLE ADD COLUMN`，
无需手工操作。

## 示例：用户举的经典场景

```
cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera01  -> fps=1  (default)
cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera02  -> fps=30 (hit=dtss)
cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera15  -> fps=30 (hit=dtss)
cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera17  -> fps=1  (default, 17 不在 02~16)
cyc_24_nan_qt_2000lx_CnDTSS_260722_144229_camera16 -> fps=30 (hit=cndtss)
cyc_24_nan_qt_2000lx_other_camera03                -> fps=1  (default, 无关键字)
```

## 硬规则

- `fps_rules.py` **可以**走 pyarmor gen（纯逻辑，无明文常量泄漏问题）
- workflow 里给 `sqFrameGrab.exe` 和 `sqFrameGrabGui.exe` 都要加
  `--hidden-import fps_rules`，GUI 还要 `--hidden-import extract_gui_fps_rules`
- 每个 `pyarmor gen` 只传 1 个文件（trial 版配额）
