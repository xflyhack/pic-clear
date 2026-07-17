# classify_gui —— 二次分类工具（规划 / v1）

**状态**：规划中
**创建**：2026-07-16
**定位**：独立新程序，不动 `extract_frames.exe` / `dedupe_pic.exe` / `pipe_gui.exe`
**输入**：已跑完去重的图片根目录
**输出**：另一根目录（不污染源目录），按业务规则分类归档

---

## 处理范围（v1）

- 遍历输入根，找每个 `camera/` 目录（目录名可配置）
- **输入根到 `camera/` 之间的完整层级全部镜像到输出**
  例：`C:/01/.../clip_xxx/camera/11/x.jpg` → `Z:/01/.../clip_xxx/camera/11/x.jpg`
- `camera/` 里未被过滤的子目录原样复制；分类桶创建在**每个** `camera/` 下
- `camera/` 下的每个子目录（如 `11`、`12`、`abc`）：
  - 名字**包含**任一"过滤关键字"→ 跳过整个子目录（不复制、不分类）
  - 否则先原样复制到输出，再逐图判定并复制到分类桶

## 分类桶（v1，共 6 大类）

在**每个** `camera/` 下建：

```
camera/
├── 舱外活体检测/                 ← 规则 1
│   ├── 人体关键点/                ← 规则 2
│   │   ├── 前备箱防夹检测/         ← 规则 4
│   │   │   └── <原子目录名>/*.jpg
│   │   ├── 前机盖开关检测/         ← 规则 5（少样本 embedding）
│   │   ├── 动态手势/               ← 规则 3（少样本 embedding）
│   │   └── <原子目录名>/*.jpg
│   └── <原子目录名>/*.jpg
├── 遮挡/                         ← 规则 6（少样本 embedding，跟活体同级）
└── <原子目录名>/*.jpg            ← 未处理，原样复制
```

- 一张图命中多条规则 → 每个桶各复制一份（真 copy）
- 输出已存在同名文件 → **覆盖**

## 规则判定（v1）

| 规则 | 手段 | 依赖 |
|---|---|---|
| 1 舱外活体检测 | YOLOv8n 检出 `person` | 复用 `yolov8n.onnx` |
| 2 人体关键点 | YOLOv8n-pose 判定"完整可标" | 新增 `yolov8n-pose.onnx` |
| 4 前备箱防夹 | 路径含"前视/前周视/前环视" + 人体贴下沿且贴左右边缘 | 几何规则，无新模型 |
| 5 前机盖开关 | 少样本 embedding | `mobilenetv3_embed.onnx` + `rules/前机盖开关检测/` |
| 3 动态手势   | 少样本 embedding | `mobilenetv3_embed.onnx` + `rules/动态手势/` |
| 6 遮挡       | 少样本 embedding | `mobilenetv3_embed.onnx` + `rules/遮挡/` |

## GUI（最简）

- 输入目录（浏览）
- 输出目录（浏览）
- camera 目录名（默认 `camera`）
- 过滤关键字（逗号分隔，子目录名包含即跳过）
- 前视相机白名单（逗号分隔，规则 4 用）
- 开始 / 停止 / 日志 / 进度

## 文件划分

- `classify_pic.py` —— 核心逻辑 + CLI（纯函数，可被 GUI import）
- `classify_gui.py` —— tkinter GUI 薄壳，调 `classify_pic` 里的函数
- 复用 `detector.py::YoloDetector`
- 新增 `pose_detector.py`

## 未来整合

- 核心逻辑在 `classify_pic.py`，GUI 是薄壳
- 未来接入 `pipe_gui.exe`：直接 import 或 subprocess 调 exe，两种都行
- 授权走 `licensing.py` 现有那套，共享 `license.lic` + `otp.secret`
