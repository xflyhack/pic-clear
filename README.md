# pic-clear

在**离线、无 Python 环境**的 Windows（例如堡垒机里的跳板机）里，扫描目录、找出**接近相同**的图片并删除。默认会用 YOLOv8n 识别图片内容：

- 含**人**的图片 → **硬保护**，一律保留
- 含**车 / 电车 / 公交 / 卡车 / 自行车 / 摩托车**的图片 → 只在相邻帧车辆位置发生变化时保护，否则参与去重
- 可选开启**场景保护**（`--scene-protect`）：把明显的**纯色 / 渐变屏**（传感器遮挡等异常帧）识别出来强制保留

## 下载 exe（推荐）

**一键拿全套 6 个 exe** —— 打开这一个页面就行：

👉 **[https://github.com/xflyhack/pic-clear/releases/latest](https://github.com/xflyhack/pic-clear/releases/latest)**

页面右侧 `Assets` 区域会列出全部 8 个 exe（`extract_frames.exe` / `dedupe_pic.exe` / `pipeline.exe` / `pipe_gui.exe` / `extract_gui.exe` / `dedupe_gui.exe` / `summary_stats_gui.exe` / `gen_license_gui.exe`），点每个 exe 后面的 ⬇ 图标就能下载。

私有仓库需要登录 GitHub 账号才能看到 Assets 区域。

> **发版流程**（作者用）：本地打 tag 并推送：
> ```bash
> git tag v0.1.3
> git push origin v0.1.3
> ```
> 6 个编译 workflow 会并行跑，各自把产物挂到同一个 Release page。约 15-30 分钟出全套。

如果 Release 页面还没有你想要的版本、又急着要产物，也可以走**逐个 workflow 下载**的老路：
仓库 `Actions` 页面 → 选对应 workflow（如 `Build Pipe GUI EXE`）→ 最新一次成功的 run → 底部 `Artifacts` 下载 zip。

## 工具集

本仓库产出 **6 个 Windows exe**，分工不同，各自独立打包：

### 业务 exe（5 个，都需要 `license.lic`）

| exe | 作用 | 体积 |
|---|---|---|
| **`extract_frames.exe`** | 递归扫描视频目录，把 `.h265` / `.mp4` 按 1 帧/秒抽成 JPEG，输出镜像目录 | ~95 MB（含 ffmpeg）|
| **`dedupe_pic.exe`** | 对图片目录做近似去重（dHash）+ YOLO 保护（人/车）+ 前后帧车运动保护 | ~57 MB（含 yolov8n）|
| **`pipeline.exe`** | 编排层：一键跑抽帧 + 去重，后台 detach，可查状态/停/看日志 | ~10-20 MB |
| **`pipe_gui.exe`** | `pipeline.exe` 的图形前端，双击运行，托盘 + 全局快捷键 + 主窗实时进度 + 日志 tail，不习惯命令行的同事用 | ~15-25 MB |
| **`extract_gui.exe`** | **抽帧专用 GUI**（只切帧不去重），选源目录 + 一级子目录多选 + fps，后台线程实时日志 + 托盘 + `Ctrl+Alt+E` | ~20-30 MB |
| **`dedupe_gui.exe`** | **去重专用 GUI**（只去重不切帧），选目标目录 + 单/一级/递归模式 + threshold/motion + 强制重跑 + 托盘 + `Ctrl+Alt+D` | ~20-30 MB |
| **`summary_stats_gui.exe`** | 图形版统计汇总工具，扫 `machine_id_*.csv`，选磁盘 + 目录树钻取 + 汇总 + 导出 CSV | ~15-25 MB |

7 个业务 exe **共用同一份 `license.lic`**，同一台机器只需申请一次授权。

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

## 并发 + 多机共享盘 + Marker 集中

**抽帧和去重都支持"单机并发 + 多机互斥"**，并且所有锁 / 完成标记都集中放到一个
`markers_root` 目录（推荐指向多机都能访问的共享盘，例如 `Z:\pic-clear-markers`）。

### Marker 集中管理

四种 marker 都不再落到视频/图片目录里，改按视频原始层级放到 `markers_root` 下：

```
Z:\pic-clear-markers\<src_name>\<sub>\<video_stem>\
  _extract.lock            ← 抽帧锁
  _done.marker             ← 抽帧完成
  _dedup.lock              ← 去重锁
  _dedup_done.marker       ← 去重完成
```

**多机部署**：10 台机器都把同一份共享盘挂成 Z 盘（或别的盘符），4 个 GUI 里都
把 `Marker 根` 指到 `Z:\pic-clear-markers`，任一视频只会被抢到锁的那台机器处理。

### 抽帧并发

- **单机并发**：`extract_frames.exe --jobs N`（默认 1）。推荐 4-8，机器强+盘快可到 16。
  GUI 对应『抽帧并发数』
- **多机互斥**：每个视频抽前在 `markers_root/<rel>/` 原子创建 `_extract.lock`；
  别的机器看到锁就跳过；崩溃/断网留下的锁 `--lock-ttl` 秒后（默认 900 = 15 分钟）
  视为过期可被抢占。GUI 对应『抽帧锁 TTL(s)』
- **中断安全**：kill 后靠锁自愈 + `_done.marker` 幂等，重跑接着来
- **日志**：`[3/240] ✓ xxx/v3.mp4 帧=32 耗时=1.4s`

### 去重并发

- **单机并发**：`dedupe_gui` / `pipe_gui` 里配『去重并发数』（默认 1）。dedupe 内部
  YOLO 会用多核，2-3 通常够；太大反而互抢 CPU
- **多机互斥**：每个视频目录去重前在对应 marker 目录原子创建 `_dedup.lock`；
  完成后写 `_dedup_done.marker`。别的机器看到 done 直接跳过、看到 lock 未过期跳过
- **断点重删**：中断后重跑，靠 `_dedup_done.marker` 幂等；`_dedup.lock` TTL 过期
  自动抢占；`dedupe_gui` 里勾『强制重跑』或加 `dedupe_pic --force` 忽略 done marker
- **日志前缀**：`[目录名] 完成 rc=0`

## 你想怎么用？

按角色选入口：

- **不会命令行 → `pipe_gui.exe`**：双击打开 GUI，选盘 + 选源目录 + 选子目录 + 点『运行』，主窗直接看每个子目录的抽帧/去重实时进度，点『查看日志』可以像 `tail -f` 一样实时跟 worker 日志。详见 [`docs/pipe_gui_exe.md`](docs/pipe_gui_exe.md)。
- **会命令行 → `pipeline.exe`**：CLI 提交任务，`pipeline.exe status/logs/stop` 查看和管理。详见 [`docs/pipeline_exe.md`](docs/pipeline_exe.md)。
- **想极简（老派）→ `scripts_bat/*.bat`**：把 exe 放到 `C:\Windows\System32`，双击 bat 一键跑。详见 [`scripts_bat/README.md`](scripts_bat/README.md)。
- **想单独抽帧 / 单独去重（GUI）**：新增 `extract_gui.exe`（只切帧）和 `dedupe_gui.exe`（只去重）双击就能用，界面里选目录 + 参数 + 点『开始』。两者与 `pipe_gui.exe` 完全独立，共用同一份 `license.lic`。
- **想单独抽帧 / 单独去重（命令行）**：直接调 `extract_frames.exe` / `dedupe_pic.exe`，见下面的分节说明。
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

## 动态口令（可选，v0.3.0+）

在 `license.lic` 之上，可以再加一层**每天首次启动必须输 6 位数字**的二次验证，
适合多人共享堡垒机的场景。特点：

- 完全离线（TOTP，参考 RFC 6238，兼容 Google / 微软 Authenticator）
- 通过后 **24 小时**内三个 GUI 都免输入（`~/.pic-clear/otp_session.json`）
- 错 3 次冷却 60 秒，容忍时钟偏差 ±90 秒
- 没有 `otp.secret` 文件时**自动跳过**，不影响老用户
- 环境变量 `PIC_CLEAR_SKIP_OTP=1` 可临时关闭
- 密钥库位置由 `PIC_CLEAR_OTP_VAULT` 环境变量控制（未设置回落 `~/.pic-clear-otp`）

### 作者签发

```bash
# 方式 A：命令行签发（旧）
/tmp/pic_venv/bin/python otp_admin.py generate <指纹> \
    --issued-to <名字> --write-secret-to /tmp/otp.secret

# 方式 B：网页签发（新，推荐）
/tmp/pic_venv/bin/python otp_web.py --host 127.0.0.1 --port 5000
# 浏览器打开 http://127.0.0.1:5000，右上角"+ 添加机器"，填三项：
#   - 机器指纹  （用户在堡垒机 exe 上打印）
#   - 机器 ID / IP （一般填机器 IP，用于识别）
#   - 颁发给     （使用人名字）
```

### 用户使用

- 把 `otp.secret` 跟 `license.lic` 一起放到 exe 同目录
- 双击 exe → 授权通过 → 弹 6 位口令对话框 → 输入即可
- 6 位口令从 Authenticator / 作者的网页面板 / 作者的 `otp_admin.py current` 命令拿

### 网页面板（毛玻璃黑色主题）

```bash
python3 otp_web.py --host 127.0.0.1 --port 5000
# 局域网共享
python3 otp_web.py --host 0.0.0.0 --port 5000
```

功能：
- 每台机器一张毛玻璃卡片，6 位大字号数字每秒刷新，30 秒环形倒计时
- 点数字复制到剪贴板
- 卡片 hover 出现小三点按钮 → 两步确认删除（防误触）
- 顶部 "+ 添加机器" 按钮，弹窗签发新机器（指纹 / 机器 ID / 颁发给 三项必填）

### Docker 部署 otp_web（持久化）

```bash
docker compose -f docker-compose.otp_web.yml up -d
# 打开 http://localhost:5000
```

- 数据落在 Docker 命名卷 `otp_vault` 里，容器重建密钥不丢
- 想直接看宿主机文件：把 compose 里 `otp_vault:/data` 改成 `./otp_vault:/data`
- 想让本地 `otp_admin.py` 也用这个库：`export PIC_CLEAR_OTP_VAULT=/srv/otp_vault`

完整文档见 `docs/otp.md`。

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
