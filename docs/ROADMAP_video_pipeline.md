# 视频→抽帧→去重 一体化流水线（规划）

**状态**：待实现（未来做）
**创建**：2026-07-12
**依赖**：现有的 `dedupe_pic.exe`（dHash 去重 + YOLO 保护 + 运动保护）

---

## 目标

把当前流程从：
```
（外部）视频 → （外部）抽帧成图片 → 用户手动放 D:\pics → dedupe_pic.exe 去重
```
升级为：
```
dedupe_pic.exe --video xxx.mp4  一条命令搞定：
    自动抽帧 → 自动 dHash + YOLO + 运动过滤 → 只留有价值的关键帧
```

## 技术方案

### 抽帧：内嵌 FFmpeg（路线 A，推荐）

- 从 <https://github.com/BtbN/FFmpeg-Builds/releases> 拉 Windows 静态编译版 `ffmpeg.exe`（约 80 MB）
- 通过 PyInstaller `--add-binary` 打进 exe，运行时用 `subprocess` 调
- 兼容 mp4 / mov / avi / mkv / flv / wmv / ts 等几乎所有格式
- 抽帧速度极快（一秒几百帧）

**为什么不选 OpenCV**（路线 B）：
- `opencv-python` wheel 60+ MB，体积没省多少
- HEVC 硬编、厂商私有编码可能读不了
- 大视频 seek 慢

**代价**：exe 体积从当前 ~200 MB 涨到 **~280 MB**

### 抽帧策略（默认 M1，其他备选）

| 模式 | 命令 | 场景 |
|---|---|---|
| **M1 定时抽帧**（默认）| `--interval 1.0` 每 1 秒 1 帧 | 通用，可控数量 |
| M2 关键帧 | `--iframes-only` | 视频本身有稀疏关键帧时 |
| M3 场景变化 | `--scene-threshold 0.3` | 长视频快速摘要 |

FFmpeg 命令对应：
- M1：`ffmpeg -i in.mp4 -vf "fps=1" out_%06d.jpg`
- M2：`ffmpeg -i in.mp4 -vf "select=eq(pict_type\,I)" -vsync vfr out_%06d.jpg`
- M3：`ffmpeg -i in.mp4 -vf "select='gt(scene,0.3)'" -vsync vfr out_%06d.jpg`

### 命令行设计

```cmd
# 单视频一条龙
dedupe_pic.exe --video D:\videos\case1.mp4 --output D:\filtered\case1 --interval 1.0 --threshold 3

# 批量目录
dedupe_pic.exe --video-dir D:\videos --output D:\filtered --interval 1.0 --threshold 3

# 只抽帧，不去重（分步）
dedupe_pic.exe --extract-only D:\videos\case1.mp4 --output D:\frames\case1

# 只去重（已有的图目录，即当前功能，保持不变）
dedupe_pic.exe D:\pics --threshold 3
```

### 输出目录结构

```
D:\filtered\case1\
├── _raw_frames\              # 抽帧原始输出（可选，--clean-raw 可删）
│   ├── frame_000001.jpg
│   ├── frame_000002.jpg
│   └── ...
├── frame_000001.jpg          # 去重后保留的帧（从 _raw_frames 硬链接或拷贝）
├── frame_000007.jpg
├── ...
├── _trash\                   # 软删除的帧
└── dedupe_report.csv         # 去重报告，含每帧命运
```

### 帧命名

- **默认**：`frame_000001.jpg`（6 位定长序号，字典序 = 时间序）
- **可选**：`frame_00m01s000ms.jpg`（含时间戳，能对应回视频时间点）
  - 命令行开关：`--frame-name timestamp`

## 待用户确认的 5 个问题

1. **视频格式和分辨率**：一般是什么？mp4 1080p？摄像头 4K？时长？
2. **默认抽帧频率**：1 帧/秒 是否合适？
3. **使用形态**：单命令一条龙 / 两步走 / 独立 extract_frames.exe？
4. **帧命名**：定长序号 or 时间戳？
5. **exe 体积**：涨到 280 MB 堡垒机上传能接受吗？如果不能，让用户单独上传 `ffmpeg.exe` 放同目录，代码 auto-detect（作为降级方案）

## 实施步骤（未来做时的清单）

1. **CI**：在 `build-windows-exe.yml` 里加下载 `ffmpeg.exe` 的步骤
2. **代码**：
   - 新增 `video_extractor.py`：封装 ffmpeg 调用、帧命名、进度回调
   - `dedupe_pic.py` 新增 `--video` / `--video-dir` / `--extract-only` 分支
   - `resolve_ffmpeg_path()`：查找顺序 = `--ffmpeg PATH` → exe 同目录 → PyInstaller 内嵌 → 系统 PATH
   - 抽帧进度用现有 `ProgressReporter` 显示（帧数百分比 + ETA）
3. **PyInstaller**：`--add-binary "ffmpeg.exe;."`
4. **文档**：更新 README，新增"视频输入"章节
5. **本地验证**：
   - 短视频（10s、1080p、H.264）→ 抽帧成功 → 去重逻辑仍正确
   - 长视频（10min+）→ 进度条不断更新
   - 冷门格式（.mkv、.mov、.avi）各测一个

## 风险 & 注意事项

- **磁盘空间**：1 小时 1080p 视频 × 1 fps × 200 KB/帧 ≈ 720 MB 抽帧数据；用户要留够空间。可加 `--check-disk-space` 预警
- **子进程编码**：ffmpeg stderr 可能带非 UTF-8 输出，读取时用 `errors="replace"`
- **中断恢复**：抽帧中途 Ctrl+C，`_raw_frames` 里可能是半截 JPEG；重跑时先删残留或跳过已存在
- **音频**：默认丢弃，只要视频轨（`-an`）

## 相关代码位置

当前实现（未来对接点）：
- `dedupe_pic.py` main() 参数解析
- `dedupe_pic.py` build_index() 扫描入口
- `.github/workflows/build-windows-exe.yml` 构建脚本
