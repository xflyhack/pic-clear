# Windows 长路径 / 映射盘 / tkinter 混合斜杠 —— 踩坑史与硬规则

> **本文档给所有以后开发新 Windows 工具（GUI 或 CLI）的开发者**。凡是要在 Windows
> 上做文件 IO（读图、扫盘、删除、写 marker 等）就必须完整看一遍。
>
> 涵盖 v0.4.30 → v0.4.35 的完整踩坑过程与最终硬规则。**这里踩过的每一个坑都真实
> 出现过在堡垒机 Windows Server 2022 SMB 挂载 Z: 上，代价是 5 个 tag、诊断反复**。
>
> **v0.4.61 更新**：把 `_normalize_windows_path` / `_to_long_path` / `_pil_open` /
> `_safe_*` 家族抽到公共模块 `winpath_util.py`，`dedupe_pic.py` / `detector.py` /
> `diag_pic.py` / `extract_frames.py` 四份代码统一。以后新工具 `from winpath_util
> import ...` 起下划线别名即可，别再复制粘贴。同时把 `extract_frames.py` (漏网之鱼)
> 全套接入，修 v0.4.60 前堡垒机上 `out_dir.mkdir` 报 `WinError 206` 的问题。

---

## 一句话总结

> **`\\?\` 只是绕开 CRT `MAX_PATH=260` 的入口；真正安全的做法是"入口归一化 +
> pathlib 每一个 IO 操作套长路径 helper + 静默 except 全删掉"**，三个动作缺一不可。

---

## 症状速查表（新工具遇到类似日志请先来这里对照）

| 症状 | 大概率是 |
|---|---|
| `[打开失败-stat] ... WinError 3 系统找不到指定的路径。` | `Path.stat()` 没走长路径兜底（本文 §5） |
| `[打开失败-hash]` / `[PIL诊断]  Image.open ERR FileNotFoundError` | PIL / detector 没走 `_to_long_path`（本文 §4） |
| `[扫描完成] 有效图片 0，打开失败 N` 但**同一张图 diag_pic 能开** | 入口路径归一化没做 —— tkinter 返回 `//?/Z:/...`（本文 §3） |
| dedupe 跑完了但 markers_root 里一个 marker 都没有 | `done_marker.write_text` 里 `except Exception: pass` 静默吞了（本文 §6） |
| 断线续跑不生效，重跑还是所有目录 | 同上 |
| `Could not find a part of the path '\\?\UNC\server\share\...'` | 别做 UNC 展开，`\\?\Z:\` 就够（本文 §2） |
| bat 里 `net use Z:` 有输出，但 `\\?\Z:\` 在 dedupe 里打不开 | 100% 是 tkinter 混斜杠或 Path.stat 漏兜底 |

---

## §1 背景：MAX_PATH=260 到底怎么回事

Windows Win32 API 的历史包袱：
- **短 API**（`fopen`, `CreateFileA` 无 `\\?\` 前缀）：路径受 `MAX_PATH=260` 限制
- **长 API**（`CreateFileW` + `\\?\` 前缀）：**直通 NT 命名空间**，理论上支持 32767 字符
- Python 的 `pathlib.Path.stat()` / `open()` / `Image.open()` 在 Windows 上**基本都是走 CRT 的短 API**，所以路径一长就挂
- 加 `\\?\` 前缀是标准解法。但 `\\?\` 有一堆坑，见下文

**堡垒机的雪上加霜**：目录被 SMB 挂载到 `Z:` 上（`Z: → \\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100`），实际 SMB 层路径比盘符看着长。业务目录动辄 200-330 字符，天生落在崩溃区。

---

## §2 弯路 1：以为映射盘要展开成 `\\?\UNC\`（v0.4.31 走错方向）

**当时的假设**：微软老文档 "Maximum Path Length Limitation" 里写着——
> If your path uses a mapped drive letter, you must convert the path to the equivalent UNC path form before prepending "\\?\"

所以 v0.4.31 加了 `WNetGetConnectionW(Z:)` 把 `Z:` 展开为 `\\filestor01...\kj-e68-datamark-100`，再套 `\\?\UNC\...`。

**堡垒机实测结果（Windows Server 2022 build 20348）**：
```
[B2] \\?\Z:\...      OK size=180283   ← 能开！
[B3] \\?\UNC\...     ERR "Could not find a part of the path"  ← 反而不能开！
```

**教训**：老文档对新 Windows 已经**不成立**。Windows 新版本上 `\\?\Z:\...` 本身直通，不用管映射不映射。**v0.4.32 立刻撤销了 UNC 展开分支**。

**硬规则**：**别做 UNC 展开**。`_to_long_path` 只做两件事：
- UNC 已经是 `\\server\share\...` → 前缀 `\\?\UNC\` 
- 其他一律 `\\?\` + 原路径（包括映射盘 `Z:\`）

---

## §3 弯路 2：tkinter filedialog 会偷偷加 `//?/` + 混合斜杠（v0.4.34 真根因）

**diag_pic 帮我们抓到的关键日志**：
```
[原始路径] //?/Z:/切帧结果/sjbz_20260715/02/...jpg
[规范路径] \\?\Z:\切帧结果\sjbz_20260715\02\...jpg   (已把 / -> \ + 去重复 \\?\)
```

**发生了什么**：
1. `filedialog.askopenfilename()` 在长路径下**返回带前缀的路径**，但**斜杠方向是正的**：`//?/Z:/...`
2. Python 代码里 `s.startswith("\\\\?\\")` 用的是反斜杠版本，**匹配不上**
3. `_to_long_path` 又叠一层前缀 → `\\?\//?/Z:/...` **双重前缀**
4. `FileNotFoundError: [Errno 2]`

**修法**：入口先归一化。任何进入 `_to_long_path` 的字符串**必须先过 `_normalize_windows_path`**：
```python
def _normalize_windows_path(image_path) -> str:
    s = str(image_path)
    if os.name != "nt":
        return s
    if "/" in s:
        s = s.replace("/", "\\")          # 正斜杠转反斜杠
    while s.startswith("\\\\?\\\\\\?\\"): # 折叠误加的双重 \\?\
        s = s[4:]
    return s
```

**硬规则**（新工具必备）：
- **所有** `_to_long_path` / `_long_path_prefix` 类 helper 的第一行必须是 `s = _normalize_windows_path(image_path)`
- 判断 `startswith("\\\\?\\")` **之前**必须归一化
- 只要 GUI 用 tkinter 就必踩，不要指望"我们没长路径不会遇到"

---

## §4 弯路 3：`\\?\` 只保护 PIL，忘了给 `pathlib.Path.stat/unlink` 套（v0.4.35 真最终根因）

**现象**：v0.4.34 修完归一化后，`diag_pic` 能开图，但 dedupe_pic 还是报：
```
[打开失败-stat] Z:\切帧结果\...jpg len=272 err=FileNotFoundError: [WinError 3]
```
一批 156 张全挂在 `stat`，PIL 那一层根本没跑到。

**根因**：`_to_long_path` 只被 `_pil_open` 调用了。`build_index` 里的 `p.stat()` 是 `pathlib.Path.stat()` 直调 CRT，走短 API，长路径 → `WinError 3`。

同理踩坑的还有：
- `p.unlink()`（删除阶段，图片可能删不掉）
- `p.exists()` / `p.is_file()`（marker 判断可能永远返回 False，导致断线续跑不生效）
- `shutil.move()`（回收站模式的兜底移动）
- `done_marker.write_text()`（marker 目录本身很深时写不进）

**修法**：`dedupe_pic.py v0.4.35` 里加了 5 个 helper —— **模板照抄即可**：

```python
def _safe_stat(p) -> "os.stat_result":
    try:
        return os.stat(str(p))
    except OSError:
        long_p = _to_long_path(str(p))
        if long_p == str(p):
            raise
        return os.stat(long_p)


def _safe_unlink(p) -> None:
    try:
        os.unlink(str(p))
    except FileNotFoundError:
        return
    except OSError:
        long_p = _to_long_path(str(p))
        if long_p == str(p):
            raise
        os.unlink(long_p)


def _safe_exists(p) -> bool:
    try:
        if os.path.exists(str(p)):
            return True
    except OSError:
        pass
    long_p = _to_long_path(str(p))
    if long_p == str(p):
        return False
    try:
        return os.path.exists(long_p)
    except OSError:
        return False


def _safe_is_file(p) -> bool:
    try:
        if os.path.isfile(str(p)):
            return True
    except OSError:
        pass
    long_p = _to_long_path(str(p))
    if long_p == str(p):
        return False
    try:
        return os.path.isfile(long_p)
    except OSError:
        return False


def _safe_move(src, dst) -> None:
    try:
        shutil.move(str(src), str(dst))
        return
    except OSError:
        pass
    long_src = _to_long_path(str(src))
    long_dst = _to_long_path(str(dst))
    if long_src == str(src) and long_dst == str(dst):
        raise
    shutil.move(long_src, long_dst)
```

**硬规则**（新工具照做）：
- **绝对不允许**直接调 `Path.stat()` / `Path.unlink()` / `Path.exists()` / `Path.is_file()` / `shutil.move()`
- 一律走 `_safe_*` helper
- **例外**：明确知道路径 <180 字符的临时文件（如 lock 文件、config 文件）可以直调，但一定加注释说明"这里路径短、无需兜底"

---

## §5 弯路 4：`except Exception: pass` 静默吞异常，断线续跑失效无声无息（v0.4.35 顺带修）

**现象**：dedupe 跑完 100 个目录，Ctrl+C 停，再跑还是全部 100 个都跑。

**根因**：
```python
if rc == 0 and done_marker is not None:
    try:
        done_marker.write_text("done", encoding="utf-8")
    except Exception:
        pass                       # ← 罪魁祸首
```
如果 marker 目录路径太深、SMB 抖动、权限问题，写失败被吞，用户永远不知道。下次跑 marker 不存在，一切重来。

**修法**：
```python
if rc == 0 and done_marker is not None:
    try:
        try:
            done_marker.write_text("done", encoding="utf-8")
        except OSError:
            # 长路径兜底
            long_mk = _to_long_path(str(done_marker))
            if long_mk != str(done_marker):
                with open(long_mk, "w", encoding="utf-8") as _fw:
                    _fw.write("done")
            else:
                raise
    except Exception as e:
        print(f"[ERROR] 写 done marker 失败: {done_marker} -> "
              f"{type(e).__name__}: {e}", file=sys.stderr)
```

**硬规则**（新工具照做）：
- **`except Exception: pass` 永远不允许出现在**：marker / lock / 断线续跑 / 授权 / 状态持久化 相关代码
- 允许静默 `except` 只有一个场景：**已经确认副作用只是"日志好看点"的辅助分支**（例如 detector 的 `try_step`）
- 每次审 PR 见到 `except: pass` 都要问一遍："这里静默失败会不会让某个功能变成幽灵成功？"

---

## §6 诊断利器：`diag_pic.exe`（v0.4.33 新增）

**长路径问题很多年前就见过一次，然后忘了**。为了不再靠"改代码 → 出 tag → 堡垒机验证 → 反复"这个 O(30min) 循环，做了独立诊断 exe：

- 单文件 tkinter GUI
- `filedialog` 选一张真实图片
- 一次跑 **6 种打开方式**：原路径 / `\\?\Z:` / `\\?\UNC` / open+BytesIO 三分身
- 附加：`os.stat` / `WNetGetConnectionW` / `net use` / 文件头 hex dump
- 全部塞进大文本框，一键复制

**使用姿势**：以后新工具在堡垒机上报"某种路径打不开"，先让用户跑 `diag_pic.exe` 选一张问题图片，贴报告过来。**5 分钟能定位到底是 6 种打开方式的哪一种能开、哪种不能开**。

**位置**：仓库根目录 `diag_pic.py`，CI workflow `.github/workflows/build-diag-pic-exe.yml`。

---

## §7 新工具接入清单（复制去打勾）

在 Windows 上做文件 IO 的新工具（GUI 或 CLI），照下面 6 步来，可以避掉这文档里的所有坑：

- [ ] **1. 复制** `_normalize_windows_path` 和 `_to_long_path` 到新代码（`dedupe_pic.py` 里现成的）
- [ ] **2. 所有 IO 入口第一行** = `s = _normalize_windows_path(image_path)`
- [ ] **3. 复制** `_safe_stat` / `_safe_unlink` / `_safe_exists` / `_safe_is_file` / `_safe_move` 家族
- [ ] **4. 全局搜** `.stat()` / `.unlink()` / `.exists()` / `.is_file()` / `shutil.move` —— **每一处**都要过 `_safe_*`
- [ ] **5. 全局搜** `except Exception: pass` / `except: pass` / `except OSError: pass` —— 涉及 marker / lock / 状态持久化的**全部改成 stderr 打日志**
- [ ] **6. tkinter GUI** 里 `filedialog.askopen*` 返回值必须 `_normalize_windows_path` 一次再往下传

**PR 检查清单**（自己写完自查）：
- 有没有直接调 pathlib 的 IO 方法？（应该全走 `_safe_*`）
- 有没有 `except: pass`？（marker 相关必须打日志）
- 有没有做 UNC 展开？（如果做了，去掉）
- 长路径判断阈值多少？（180 更保险，不要用 200 或 260）
- 长度 300 字符的路径能不能跑通？（本地模拟或上堡垒机测）

---

## §8 版本演进快速索引

| 版本 | 改动 | 结果 |
|---|---|---|
| v0.4.28 | PIL Image.open 加 `\\?\` helper | 修本地 D: 长路径, 堡垒机 Z: 还挂 |
| v0.4.31 | 加 `WNetGetConnectionW` 展开 UNC | ❌ 弯路, `\\?\UNC\` 反而打不开 |
| v0.4.32 | 撤 UNC 展开 + BytesIO 兜底 + PIL 诊断 | 还挂, 但日志更详细 |
| v0.4.33 | 新增 `diag_pic.exe` + 失败日志分类 | 诊断利器就位, 日志能看到"stat 挂了" |
| v0.4.34 | `_normalize_windows_path` 修 `//?/` 混斜杠 | diag_pic 能开图, dedupe 还挂 stat |
| **v0.4.35** | `_safe_stat/unlink/exists/is_file/move` + marker 写失败打日志 | ✅ **通了** |
| **v0.4.61** | 抽公共模块 `winpath_util.py` + `extract_frames.py` 接入 `safe_mkdir/glob/read_text/write_text/os_open` | ✅ **抽帧长路径通了** |
| **v0.4.62** | ffmpeg image2 muxer + `\\?\` 输出 pattern 的新坑 → 走本地 temp 中转 + 完整 stderr 日志 | ✅ **抽帧真正通了** |
| v0.4.63 | *(不是长路径坑, 顺带记一下)* ffmpeg 7.x mjpeg encoder 拒 full-range YUV → 加 `-strict unofficial` + `-pix_fmt yuvj420p`; 主日志摘要改成 `_pick_key_error_line` | ✅ DMS/OMS 视频通了 |
| **v0.4.64** | `safe_mkdir` 加建后验证 + 新增 `safe_isdir` + `[MKDIR_QUIRK]` 日志; `extract_frames` 搬迁前再 mkdir 一次 safety net | ✅ **修 SMB `\\?\UNC\` makedirs 撒谎导致 [MOVE_FAIL] 138/138 全挂** |

**关键教训**：改一个 helper 影响面看似小，实际打包成 exe 分发要 20 分钟 CI + 5 分钟堡垒机验证。**能提前用 `diag_pic.exe` 摊事实的场景，永远不要靠 tag 迭代猜方向**。

---

## §9 白话版：为什么要给路径加 `\\?\`（新人必读）

### UNC 是什么？

**UNC = 共享盘的原始地址**，长这样：

```
\\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100\切帧结果\...
```

开头两个反斜杠 + 服务器名 + 共享名。文件管理器地址栏能直接输入打开。

### Z: 是什么？

Z 盘就是给上面那串 UNC **起个短名字**。你在系统里点了"映射网络驱动器"以后，Windows 记住：

> 以后 `Z:\` = `\\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100\`

两者指的**是同一个东西**，就像手机联系人里"老王" = "138xxxx1234"。

### 为什么加 `\\?\` 就能过长路径？

Windows 有两套读文件的 API：

| API | 路径长度上限 | 用法 |
|---|---|---|
| 老的 (短 API) | 260 字符 | 直接写 `Z:\a\b.jpg` |
| 新的 (长 API) | 32000 字符 | 前面加 `\\?\`，写 `\\?\Z:\a\b.jpg` |

`\\?\` 相当于告诉 Windows："**这个路径走新 API**，别用 260 限制卡我"。

### 为什么曾经想展开成 UNC，反而挂了（v0.4.31 弯路）

想过把 `Z:\` 反查回 UNC，写成长路径版：

```
\\?\UNC\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100\切帧结果\...
```

结果堡垒机的 `WNetGetConnectionW` 返回 `1783 ERROR_INVALID_UNICODE`，
拿到的服务器/共享名有问题，拼出来的路径 Windows 认不出。**156 张图全打不开**。

**结论**：保留 `Z:\` 直接加 `\\?\Z:\...` 就够，别多此一举反查 UNC。

### 本地 D 盘要不要也加 `\\?\`？

**要，但只在路径长的时候加。** 规则：

- `D:\短路径\a.jpg` (< 240 字符) → 不加，直接用
- `D:\很深...\a.jpg` (≥ 240 字符) → 必须加 `\\?\`
- `Z:\` 映射盘 → 只要用了就建议全加（保险）

### 为什么不无脑给所有路径都加？

1. `\\?\` 前缀会**跳过路径规范化**：
   - `/` 不会自动转 `\`
   - `.` `..` 不会解析
   - `C:\foo\..\bar` 不会变 `C:\bar` —— 传什么就是什么
2. 有些老 API / 第三方库不认 `\\?\`
3. 相对路径根本不能加（`\\?\` 只支持绝对路径）

### 标准做法（代码里就这么做的）

```python
def _to_long_path(p: str) -> str:
    if not sys.platform.startswith("win"): return p
    if p.startswith("\\\\?\\"): return p       # 已加过就不动
    p = os.path.normpath(p)                    # 先规范化 / -> \
    if not os.path.isabs(p): return p          # 相对路径不加
    if len(p) < 240: return p                  # 短路径不加, 留 20 字符余量
    if p.startswith("\\\\"):                   # 已经是 UNC 就走 UNC 长路径
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p                       # 普通盘符
```

**一句话**：本地 D 盘和堡垒机 Z 盘走**同一套代码**，
短路径原样、长路径自动加前缀，两边都能跑。

---

## §10 弯路 5：ffmpeg image2 muxer 不吃 `\\?\` 输出 pattern（v0.4.62 真最终根因）

**现象 (v0.4.61 挂)**：mkdir 长路径 WinError 206 修好后, 抽帧到 ffmpeg 阶段
又炸, stderr 一堆:

```
[vost#0:0/mjpeg @ ...] Error submitting a packet to the muxer: No such file or directory
[out#0/image2 @ ...] Error muxing a packet
[out#0/image2 @ ...] Task finished with error code: -2 (No such file or directory)
ffmpeg rc = 4294967294  (uint32 表示的 -2, 即 ENOENT)
```

**根因**: v0.4.61 我给 ffmpeg 的 output pattern 也过了 `_to_long_path`, 得到
`\\?\Z:\...\camera12_%04d.jpg` (334 字符). ffmpeg 的 **image2 muxer** 内部走
`av_get_frame_filename2()` 展开 `%04d` 序号时对 `\\?\` 前缀处理有 quirk:
- 输入侧 `-i` 走 `avio_open2()` URL 层, 兼容 `\\?\` (所以输入是 OK 的)
- 输出侧 image2 muxer 每帧展开 filename 后再 `avio_open2()`, 路径归一化把
  `\\?\` 前缀里的 `?` / `\` 视为普通字符, 展开出的路径根本不是我们预期的

**修法 (v0.4.62)**: **ffmpeg 只写本地 temp 目录, 短路径**. 跑完再用 `_safe_move`
把生成的 `.jpg` 逐个搬到真正的 `out_dir` (SMB 长路径, `_safe_move` 自带 `\\?\` 兜底).

关键代码 (`extract_frames.py::_do_extract`):
```python
src_arg = _to_long_path(src_str)          # 输入侧仍走 \\?\
tmp_out = Path(tempfile.mkdtemp(prefix="pic-clear-ext-"))
tmp_out_pattern = str(tmp_out / ffmpeg_name)   # 本地短路径, 不加 \\?\
cmd = [str(ffmpeg), ..., "-i", src_arg, ..., tmp_out_pattern]
# ffmpeg 完成后:
for src_frame in tmp_out.glob(glob_pattern):
    _safe_move(src_frame, out_dir / src_frame.name)
```

**代价**: 多一次 IO (temp -> SMB), 但换来彻底绕开 image2 muxer 的坑,
而且 temp 本地写通常比 SMB 直写还快, 净收益基本正.

**顺带增强 (v0.4.62)**: 所有 ffmpeg 失败分支改用 `[FFMPEG_FAIL]` 多行日志,
完整贴 stderr (不截断) + 上下文 (rel / src / src_arg / long_prefix / tmp_pat
/ 各自长度), 排查长路径类问题不用再 mental math 字符长度.

**硬规则 (新工具要用 ffmpeg 或类似写文件的外部 CLI 时)**:
- 输入侧参数 (`-i`) 可以走 `_to_long_path` 加 `\\?\`
- **输出侧参数一律不加 `\\?\`**, 改用"本地 temp 目录 + 搬迁" 或 "已开
  LongPathsEnabled + manifest longPathAware=true 的自定义 build 二进制"
- 失败时日志**完整贴 stderr**, 不要 `err[:200]` 截断

### 为什么 bat 能删、Python 不能删（读者常问）

bat 里的 `del` / `rmdir` 是原生 Windows 命令，内部就是长路径实现，走系统级 IO。

Python 的 `pathlib.Path.stat()` / `open()` 走的是 Python 自己包一层的 C API，
**长路径必须显式加 `\\?\` 前缀才吃得下**。这不是 Z 盘特殊，是 "长路径 + Python IO" 这对组合特殊。

---

## §11 弯路 6：`safe_mkdir` 在 SMB + `\\?\UNC\` 上会"撒谎"（v0.4.64 修）

**现象 (v0.4.62-v0.4.63 遗留)**: ffmpeg 明明成功抽出 138 帧到本地 temp,
搬迁阶段 `_safe_move` 全挂:

```
[MOVE_FAIL] 138/138 帧搬迁失败
  ...camera09_0001.jpg -> FileNotFoundError: [Errno 2] No such file or directory:
    '\\?\UNC\filestor01...\camera09\camera09_0001.jpg'
```

`out_dir` 明明在 `_extract_one_impl` 顶部就已经 `_safe_mkdir(..., exist_ok=True)`,
按理不该 not found. 但反查发现 138 帧、137 帧、136 帧… 每次都刚好整批全挂,
说明 dst 父目录**根本不存在**.

**根因**: `safe_mkdir` 在 SMB + `\\?\UNC\` 深路径 (>=260 字符) 上碰到 quirk:

- 短路径 `os.makedirs()` 抛 `FileNotFoundError [WinError 206]` 太长
- 走长路径 fallback `os.makedirs(r'\\?\UNC\filestor01\...')`
- **Windows 在中间某一层抛 `FileExistsError`**（不是叶节点, SMB 上普遍见到）
- 老代码 `except FileExistsError: if exist_ok: return` **静默吞掉**返回"成功"
- 但叶节点**其实没建出来**, 下游 `shutil.move` 全挂

**核心教训**: `exist_ok=True` 语义是"目标目录已存在也算成功", 前提是**目标真的存在**.
SMB 上 `FileExistsError` 可能来自中间层 (奇怪但真实发生), 无脑吞会撒谎.

**修法 (v0.4.64)**:

1. **新增 `safe_isdir(p)`**: `Path.is_dir` 的长路径安全版, 跟 `safe_is_file`
   / `safe_stat` 一个套路 (短路径先试, 挂了或 False 再走 `\\?\` fallback).
2. **重写 `safe_mkdir`**:
   - 遇到 `FileExistsError`, **只有 `safe_isdir(long_p) == True` 才 return**;
     中间层的 quirk 打 `[MKDIR_QUIRK]` 日志继续走验证兜底
   - 短路径 `makedirs` 返回成功后也强制 `safe_isdir` 验证一次 (罕见 SMB 缓存)
   - 长路径 fallback 挂 `OSError` 时 raise `e_long` (不再 raise `e_short`,
     避免遮住真实原因)
   - **兜底验证**: 无论走哪条路径, 最后 `safe_isdir(p_str) or safe_isdir(long_p)`
     不通过就 raise, 不再撒谎
3. **`extract_frames._do_extract` safety net (方案 B)**: 搬迁前对 `out_dir` 再
   `_safe_mkdir` 一次 (belt + suspenders), 兜底老 exe 或将来类似坑.
   mkdir 幂等, 已存在近零开销.

**新日志样例** (堡垒机遇 SMB quirk 时会看到):

```
[MKDIR_QUIRK] 长路径 makedirs 抛 FileExistsError 但叶节点 isdir=False:
    \\?\UNC\filestor01\...\camera09 -> FileExistsError: [WinError 183] ...
```

看到就是 SMB 中间层 quirk 触发了, `safe_mkdir` 已经继续验证 + 兜底, 通常不会
raise 到 `_do_extract` 里. 如果 `_do_extract` 还是打了 `[MOVE_FAIL]`,
说明这个目录**真的**建不出来 (权限 / 磁盘满 / 网络断), 拿具体 `[MKDIR_QUIRK]` /
`[MOVE_FAIL]` 段截图定位.

**验证**: 本地 Mac 冒烟 3 次幂等 mkdir + 深路径 mkdir + `_do_extract` 完整流程
全绿 (`STAGE=ok N=2`, marker 写出, 帧文件到位).

**硬规则 (以后写 `safe_*` helper 通用铁律)**:

- `exist_ok`, `errors='ignore'`, `except X: pass` 这类**看似简化用户体验**的分支,
  必须**建后/删后/写后再验证一次**. Windows 上尤其如此.
- **`except FileExistsError: pass`** / **`except FileNotFoundError: pass`** 是
  远程文件系统上最容易撒谎的两个写法, 见到必须审计.
- 挂了 raise 时**优先 raise 走最长路径 (fallback) 的实际错**, 而不是 raise
  短路径的错 (老代码 `raise e_short` 在长路径 fallback 挂时会遮住真实原因,
  排查时人眼看不见 `\\?\UNC\` 那条链).
