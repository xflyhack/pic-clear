# pic-clear

在**离线、无 Python 环境**的 Windows（例如堡垒机里的跳板机）里，扫描目录、找出**接近相同**的图片并删除。默认会用 YOLOv8n 识别图片内容：

- 含**人**的图片 → **硬保护**，一律保留
- 含**车 / 电车 / 公交 / 卡车 / 自行车 / 摩托车**的图片 → 只在相邻帧车辆位置发生变化时保护，否则参与去重
- 可选开启**场景保护**（`--scene-protect`）：把明显的**纯色 / 渐变屏**（传感器遮挡等异常帧）识别出来强制保留

## 工具集

本仓库产出 **6 个 Windows exe**，分工不同，各自独立打包：

### 业务 exe（5 个，都需要 `license.lic`）

| exe | 作用 | 体积 |
|---|---|---|
| **`extract_frames.exe`** | 递归扫描视频目录，把 `.h265` / `.mp4` 按 1 帧/秒抽成 JPEG，输出镜像目录 | ~95 MB（含 ffmpeg）|
| **`dedupe_pic.exe`** | 对图片目录做近似去重（dHash）+ YOLO 保护（人/车）+ 前后帧车运动保护 | ~57 MB（含 yolov8n）|
| **`pipeline.exe`** | 编排层：一键跑抽帧 + 去重，后台 detach，可查状态/停/看日志 | ~10-20 MB |
| **`pipe_gui.exe`** | `pipeline.exe` 的图形前端，双击运行，托盘 + 全局快捷键 + 主窗实时进度 + 日志 tail，不习惯命令行的同事用 | ~15-25 MB |
| **`summary_stats_gui.exe`** | 图形版统计汇总工具，扫 `machine_id_*.csv`，选磁盘 + 目录树钻取 + 汇总 + 导出 CSV | ~15-25 MB |

5 个业务 exe **共用同一份 `license.lic`**，同一台机器只需申请一次授权。

### 签发工具（1 个，作者用）

| exe | 作用 | 体积 |
|---|---|---|
| **`gen_license_gui.exe`** | 图形版 license 签发工具，双击运行，内置签发私钥。⚠ 仅限作者/内部使用，不要外发。 | ~15-25 MB |

签发工具本身**不需要 license**。命令行版是 `gen_license.py`（`python gen_license.py <指纹>`）。

### 辅助 bat 脚本

`scripts_bat/` 目录下有一系列 bat，用于纯命令行流程、进程状态检测、marker 文件隐藏等。详见 [`scripts_bat/README.md`](scripts_bat/README.md)。

## 典型 pipeline

```
h265/mp4 视频目录 
    │  extract_frames.exe
    ▼
镜像目录 + 抽好的 JPEG 帧
    │  dedupe_pic.exe
    ▼
去重后仅保留有价值的关键帧
```

**推荐用 `pipeline.exe` 或 `pipe_gui.exe` 一键跑完整个流程**，不用一个个手动调。

## 你想怎么用？

按角色选入口：

- **不会命令行 → `pipe_gui.exe`**：双击打开 GUI，选盘 + 选源目录 + 选子目录 + 点『运行』，主窗直接看每个子目录的抽帧/去重实时进度，点『查看日志』可以像 `tail -f` 一样实时跟 worker 日志。详见 [`docs/pipe_gui_exe.md`](docs/pipe_gui_exe.md)。
- **会命令行 → `pipeline.exe`**：CLI 提交任务，`pipeline.exe status/logs/stop` 查看和管理。详见 [`docs/pipeline_exe.md`](docs/pipeline_exe.md)。
- **想极简（老派）→ `scripts_bat/*.bat`**：把 exe 放到 `C:\Windows\System32`，双击 bat 一键跑。详见 [`scripts_bat/README.md`](scripts_bat/README.md)。
- **想单独抽帧 / 单独去重**：直接调 `extract_frames.exe` / `dedupe_pic.exe`，见下面的分节说明。
- **想看统计 / 看每台机器删了多少张 → `summary_stats_gui.exe`**：双击打开 GUI，选磁盘 + 目录树钻取到 `data_source` 或某天的目录，点『开始汇总』看当前剩余 / 累计删除 / 按机器分。也能导出 CSV 给老板看。
- **作者要签发 license.lic**：命令行版 `python gen_license.py`（见"作者签发流程"章节），或图形版 `gen_license_gui.exe`（见 [`docs/gen_license_gui.md`](docs/gen_license_gui.md)）。

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
- **保护**：默认对每张图跑 YOLOv8n ONNX 推理，检测到 COCO 里的
  `person / bicycle / car / motorcycle / bus / train / truck` 就打上"受保护"标签
  - `person` 命中即**硬保护**（无条件保留）
  - 车类命中不硬保护，交给下面的"相邻帧车变化保护"决定
- **同目录相邻帧车变化保护**：在同一目录内，按文件名字典序视作"帧序列"，
  逐对比较相邻两帧的车辆状态。命中任一即打上"运动保护"标签：
  - 车数变化（如 2 辆 → 3 辆，或 1 辆 → 0 辆）
  - 车框贪心匹配 IoU < 0.5（无法一一对应）
  - 任一车中心位移 > `--motion-threshold` × max(W, H) 像素
- **场景保护（可选，`--scene-protect`）**：仅对 YOLO 无 `person`/车类命中的图跑，
  发现明显的"纯色 / 渐变屏"就打上"场景保护"标签强制保留，覆盖传感器遮挡等异常帧：
  - `mono_flat`  全图相邻像素平均差极小（几乎无纹理）
  - `mono_color` 高饱和 + 色相直方图峰值集中（大片同色/近同色）
  - 默认关闭，`run_all.bat` 里会问一句 YES/NO；watcher 用 `/scene` 开关
- **决策**：
  - 组内任何图触发保护（含 `person` 硬保护 / 相邻帧车变化 / 场景保护）→ **全部 KEEP**
  - 组内全部未保护 → 按 `--strategy` 挑一张 KEEP，其余 DELETE

## 授权

exe **必须搭配 license.lic 才能运行**。授权采用 **RSA-2048 签名 + 机器指纹（主板序列号+磁盘序列号+主机名）** 绑定，一台机一份。

### 首次使用流程

1. **拿到 exe**，双击运行任意命令（如 `dedupe_pic.exe --fingerprint`）
2. 程序输出**本机指纹**，形如：
   ```
   [授权] 本机指纹: A1B2-C3D4-E5F6-7890
   ```
3. **把这行指纹发给作者**（微信/邮件均可）
4. 作者用私钥签发 `license.lic`，回给你
5. 把 `license.lic` 放到 `dedupe_pic.exe` **同目录**，重新运行即可

### 单独查看指纹

```cmd
dedupe_pic.exe --fingerprint
```
只打印 16 位指纹后立刻退出，不会尝试跑任何业务逻辑，不需要 license。

### 作者签发流程（本地生成 license.lic）

**只有作者本地跑，`gen_license.py` 不会打包进 exe 分发。**

**准备**：私钥（默认在 `~/.dedupe_pic_keys/private.pem`；仓库内也有一份 `secrets/private.pem`）+ 装了 `cryptography` 的 Python 环境。

**推荐用仓库自带的 venv**（一次性安装：`python3 -m venv /tmp/pic_venv && /tmp/pic_venv/bin/pip install cryptography`）：

```bash
# 最简单：只给指纹，其他走默认（issued_to=user, expire=never, 私钥用 ~/.dedupe_pic_keys/private.pem）
/tmp/pic_venv/bin/python gen_license.py E915-F232-792C-5B41

# 常用：指定发放对象和输出文件名
/tmp/pic_venv/bin/python gen_license.py E915-F232-792C-5B41 \
    --issued-to xflyhack \
    --output /tmp/E915-F232-792C-5B41.lic

# 用仓库内的私钥（Mac 上没配 ~/.dedupe_pic_keys 时）
/tmp/pic_venv/bin/python gen_license.py E915-F232-792C-5B41 \
    --issued-to xflyhack \
    --private-key secrets/private.pem \
    --output /tmp/E915-F232-792C-5B41.lic

# 设置到期日
/tmp/pic_venv/bin/python gen_license.py E915-F232-792C-5B41 \
    --issued-to lisi --expire 2027-12-31 \
    --note "内测许可" --output lisi.lic
```

**参数速查**：

| 参数 | 说明 | 默认 |
|---|---|---|
| `fingerprint`（位置） | 目标机器指纹 `XXXX-XXXX-XXXX-XXXX` | 必填 |
| `--issued-to` | 发放给谁（记录用） | `user` |
| `--expire` | `YYYY-MM-DD` 或 `never` | `never` |
| `--note` | 备注 | 空 |
| `--private-key` | 私钥路径 | `~/.dedupe_pic_keys/private.pem` |
| `--output` | 输出文件名 | `license.lic` |

**已签发指纹**在 `AGENTS.md` 里维护。

**图形版签发工具（`gen_license_gui.exe`）**：不想敲命令行、想直接双击？CI 会自动构建 Windows 版：

- artifact 名：`gen-license-gui-windows-exe`
- 双击运行，表单里填指纹 / 发放对象 / 到期日 / 备注 / 输出路径，点『生成 license.lic』
- **私钥内置**（用 CI 打包时把 `secrets/private.pem` 一起打进去），无需外部依赖
- ⚠️ 此 exe 具备签发能力，**仅限内部使用，不要外发**（详见 `docs/gen_license_gui.md`）

---

### license 位置查找顺序

1. 环境变量 `DEDUPE_LICENSE=D:\path	o\license.lic`（最高优先级）
2. exe 同目录 `license.lic`
3. 找不到则报错

---

## `extract_frames.exe` 使用（视频抽帧）

递归扫描一个视频目录，把每个 `.h265` 视频按 fps 抽成 JPEG，**输出目录结构完全镜像输入目录**，视频文件本身变成一个"同名子目录"存放抽出的帧。

### 硬规则

- 任何名叫 **`VLM`** 的目录（含所有子孙）**整棵子树跳过**（大小写不敏感）
- 只处理 `.h265`（可用 `--ext` 加其他扩展名如 `h265,hevc,mp4`）

### 示例

假设有：
```
D:\videos\
├── 1a\2a\3a\4a\6a\video1.h265
├── 1a\2a\3a\4a\7a\VLM\skip_me.h265   ← VLM 下，会被跳过
└── 1\2\3\4\5\video2.h265
```

运行：
```cmd
extract_frames.exe D:\videos D:\frames --fps 1
```

得到：
```
D:\frames\
├── 1a\2a\3a\4a\6a\video1\frame_000001.jpg
│                              \frame_000002.jpg
│                              ...
└── 1\2\3\4\5\video2\frame_000001.jpg
                            ...
```

### 常用参数

```
extract_frames.exe <SRC_ROOT> <DST_ROOT> [选项]

  --fps FLOAT              每秒抽多少帧，默认 1.0
  --ext EXT                扫描扩展名（逗号分隔），默认 h265
  --skip-dir NAME          要跳过的目录名（逗号分隔），默认 VLM
  --quality INT            JPEG 质量 1-100，默认 90
  --no-skip-existing       不跳过已抽好帧的视频（默认会跳过，重跑友好）
  --dry-run                只列出计划，不真抽
  --ffmpeg PATH            ffmpeg.exe 路径（默认自动查找 exe 同目录）
  --fingerprint            打印本机指纹后退出（申请授权用）
```

### 跟 dedupe_pic.exe 串起来

```cmd
:: 1) 抽帧
extract_frames.exe D:\videos D:\frames --fps 1

:: 2) 去重（每个视频子目录独立聚类，跨视频也会全局去重）
dedupe_pic.exe D:\frames --threshold 3
```

---

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
