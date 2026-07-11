# pic-clear

在**离线、无 Python 环境**的 Windows（例如堡垒机里的跳板机）里，扫描目录、找出**接近相同**的图片并删除。**默认会用 YOLOv8n 识别图片内容，含"人 / 车 / 电车 / 公交 / 卡车 / 自行车 / 摩托车"的图片一律保留，绝不删除。**

## 场景

- 通过堡垒机登录到一台 Windows 虚机
- D 盘里有一堆图片（`.jpg` / `.jpeg` / `.png` 等），子目录嵌套很深
- 目标机**不能联网、没有 Python**
- 堡垒机**允许上传文件、不允许下载文件**

## 方案

1. Python 脚本 `dedupe_pic.py`（去重）+ `detector.py`（YOLOv8n 目标检测）
2. GitHub Actions 的 Windows runner 打包成**单文件 exe**（Python 运行时 + Pillow + onnxruntime + `yolov8n.onnx` 全部内嵌）
3. 从 Actions 下载 `dedupe_pic.exe` → 上传到 Win 机 → 命令行运行

## 算法

- **去重**：每张图算 dHash（64-bit 差分感知哈希），组内两两 Hamming 距离 ≤ threshold 视为"接近相同"，用 Union-Find 聚类
- **保护**：默认对每张图跑 YOLOv8n ONNX 推理，检测到 COCO 里的 `person / bicycle / car / motorcycle / bus / train / truck` 就打上"受保护"标签
- **同目录相邻帧车变化保护**：在同一目录内，按文件名字典序视作"帧序列"，
  逐对比较相邻两帧的车辆状态。命中任一即打上"运动保护"标签：
  - 车数变化（如 2 辆 → 3 辆，或 1 辆 → 0 辆）
  - 车框贪心匹配 IoU < 0.5（无法一一对应）
  - 任一车中心位移 > `--motion-threshold` × max(W, H) 像素
- **决策**：
  - 组内任何图触发保护（含保护类别 或 相邻帧车变化）→ **全部 KEEP**
  - 组内全部未保护 → 按 `--strategy` 挑一张 KEEP，其余 DELETE

## 使用步骤

### 1. 拿到 exe

推荐用 GitHub Actions 自动打包：

```bash
git push
```

打开仓库 Actions → 最新一次 `Build Windows EXE` 运行 → 底部 **Artifacts** → 下 `dedupe_pic-windows-exe.zip` → 解压得到 `dedupe_pic.exe`。

体积预计 100–250 MB（因为内嵌了 onnxruntime 和 yolov8n.onnx）。

### 2. 上传到堡垒机 Win 机

通过堡垒机的"文件上传"把 `dedupe_pic.exe` 传到 `D:\tools\`。**模型已经内嵌，不需要单独传 `.onnx` 文件。**

### 3. 先 dry-run（不删任何东西，只出报告）

```cmd
cd /d D:\tools
dedupe_pic.exe D:\pic-clear\actions\runs\29158386313 --threshold 3
```

在当前目录产出 `dedupe_report.csv`，字段：

| 字段 | 含义 |
|---|---|
| `group_id` | 组号 |
| `action` | `KEEP` / `DELETE` |
| `path` | 文件路径 |
| `size_bytes` | 文件大小 |
| `mtime` | 修改时间 |
| `phash_hex` | 感知哈希 |
| `is_protected` | `yes` = 检测到保护类别 |
| `detected_classes` | 命中的类别（如 `person\|car`）|
| `max_conf` | 最高置信度 |
| `motion_protected` | `yes` = 同目录相邻帧车辆变化，被保护 |
| `motion_reason` | 变化原因：`count_changed(1->2)` / `moved(80px>54px)` / `iou_low(0.3)` |

用 Excel 打开 CSV 抽查几组，确认无误。

### 4. 正式删除（推荐软删除）

```cmd
:: 软删除：把重复文件移到 D:\_dedupe_trash\，可回滚
dedupe_pic.exe D:\pic-clear\actions\runs\29158386313 --threshold 3 --apply --trash-dir D:\_dedupe_trash

:: 或直接永久删除
dedupe_pic.exe D:\pic-clear\actions\runs\29158386313 --threshold 3 --apply --hard-delete
```

### 5. 关掉检测让速度飞起（不推荐，除非你确认没有需要保护的图）

```cmd
dedupe_pic.exe D:\pics --no-protect --threshold 3
```

## 所有参数

```
dedupe_pic.exe <根目录> [选项]

必填：
  <根目录>                  要扫描的目录，递归处理，如 D:\pics

常用选项：
  --threshold N            相似度阈值（Hamming 距离），0=完全一样，3=严格（推荐起步），
                           5=默认，10=宽松（可能误伤）
  --strategy S             组内非保护图的保留策略：largest / oldest / shortest-path，默认 largest
  --ext EXT                扫描扩展名，默认 jpg,jpeg,png,bmp,gif,webp，all=不过滤

目标检测保护（默认启用）：
  --no-protect             关闭检测（速度大幅提升，但不再保护含人/车的图）
  --model PATH             yolov8n.onnx 路径，默认自动查找 exe 同目录 / 内嵌
  --protect LIST           保护类别（逗号分隔）
                           默认: person,bicycle,car,motorcycle,bus,train,truck
                           可选 COCO 80 类中任意组合，如追加 airplane,boat 等
  --conf FLOAT             置信度阈值，默认 0.35（越大越严格，漏检风险变高）
  --motion-threshold F     同目录相邻帧车运动阈值（占 max(W,H) 的比例），默认 0.05
                           越小越灵敏（车抖动一下就当变化），越大越钝
                           推荐：0.02~0.05 车辆序列 / 0.10 电影胶片式大幅移动

删除相关（不加 --apply 一律 dry-run）：
  --apply                  真正执行删除
  --trash-dir DIR          软删除到该目录（推荐）
  --hard-delete            强制永久删除

报告输出：
  --report PATH            默认 ./dedupe_report.csv
  --failed-report PATH     无法解码的文件清单
```

## 阈值 & 置信度选择

**Hamming threshold**：
| 值 | 效果 |
|---|---|
| 0 | 只删完全一样 |
| 3 | 几乎肉眼一样（推荐首次使用）|
| 5 | 轻微压缩 / 裁边算相同（默认）|
| 10 | 构图相似即合并（**可能误杀**）|

**检测 conf**：
| 值 | 效果 |
|---|---|
| 0.25 | 灵敏，模糊/远处的人也保护，可能过度保护 |
| 0.35 | 默认，兼顾准确和召回 |
| 0.5 | 严格，只保护清晰目标（漏检风险，可能误删含人图片）|

## 性能预期

- dHash：每张几毫秒
- YOLOv8n 推理：CPU 上大约 **50–200 ms/张**（取决于 CPU）
- 聚类：O(N²)，N=1 万约几秒，N=10 万约几分钟

1000 张图约 1–3 分钟；1 万张图约 15–30 分钟。**主要瓶颈在检测，`--no-protect` 可加速 20 倍以上。**

## 拿报告出堡垒机的小技巧

由于机器不能下载文件：
- 直接在 Win 机用 Excel / 记事本打开 CSV
- `type dedupe_report.csv | more` 分屏看
- 大多数堡垒机允许**剪贴板文本传出**：`clip < dedupe_report.csv` 可把 CSV 内容复制到剪贴板带出来
- 实在不行截图

## 风险 & 建议

1. **必须先 dry-run + 抽查 CSV**，特别关注 `is_protected=no` 的 DELETE 行
2. **首次跑真实数据用 `--threshold 3`**，不要用默认 5
3. **优先 `--trash-dir` 软删除**，验证一段时间再永久删
4. 检测模型是 YOLOv8n（最小档），偶尔会漏检**过小 / 过暗 / 遮挡严重**的目标；如果这一点对你很关键，告诉我，可以换 YOLOv8s（约大 3 倍）
5. YOLO 训练在 COCO 数据集上：**电车 / 有轨电车归到 `train` 类**；如果你的图里有别的交通工具（如公交、卡车、面包车），都在默认保护列表里，不用管
