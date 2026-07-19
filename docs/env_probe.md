# env_probe.py —— 运行环境自动探测（新 GUI 开发者必读）

> **一句话**：上游反复改磁盘挂载方式（映射盘 → UNC 直连 → 网络位置 → Samba），
> 每换一次我们下游软件就翻车一次。与其一次次打补丁，不如让代码**运行时探测环境**，
> 自动适配。本模块就是这个"感知层"。

---

## 为什么要做这件事（历史血泪）

pic-clear 从 v0.4.28 到 v0.4.71 打过 40+ 个 tag，其中**至少 15 个**是在跟"上游又改挂载了"这件事搏斗：

| 时期 | 上游动作 | 我们改了什么 |
|---|---|---|
| v0.4.28 | 数据路径 >200 字符 | 加 `\\?\` 前缀 |
| v0.4.31→v0.4.32 | Z: 映射盘拔了，改 UNC 直连 | 反转 `\\?\UNC\` 展开策略 |
| v0.4.34 | tkinter 混斜杠副作用 | 归一化 `//?/` → `\\?\` |
| v0.4.35 | 长路径修复漏了 `stat/unlink` | 抽 `_safe_*` 全套 helper |
| v0.4.62 | ffmpeg 不吃 `\\?\` 输出 | 走 tmp + 事后 move |
| v0.4.64 | Samba `\\?\UNC\` mkdir 撒谎 | 建后验证 |
| v0.4.65 | Samba stat 撒谎 | 长路径 stat 兜底 |
| v0.4.69 | 单文件 stat 撒谎但 listdir 稳 | 父目录 listdir 兜底 |
| v0.4.70 | 深路径 listdir 也撒谎 | Win32 FindFirstFileW 兜底 |
| v0.4.71 | 上游又换 Samba 服务端，缓存假 miss | 加环境体检 tab |
| **v0.4.72** | **决定：不再逐个坑修，统一做环境探测** | **本模块** |

**根源**：上游堡垒机磁盘一定是外挂（本地存不下几十 TB 视频），运维不懂 Windows/Samba 交互，随手换挂载方式：

- 有时候 `Z:` 盘符映射（走 SMB Redirector）
- 有时候 `\\filestor01...\share` UNC 直连（走 explorer 网络位置）
- 服务端有时 Windows Server（老式）
- 服务端有时 Linux Samba（现在）
- Windows 长路径开关 `LongPathsEnabled` 有时开有时不开

**每种组合下 Python IO 行为略有不同**，最狠的坑是 Samba：
- **不发 SMB Change Notify** → Windows 客户端只能靠 `DirectoryCacheLifetime`（默认 10 秒）过期后重查
- **`os.stat` 撒谎、`os.listdir` 撒谎**（同一文件 5 种 API 给 5 种结果）
- **对空格、`+`、中文文件名的处理跟 NTFS 微妙不同**

---

## 本模块解决什么

**建立一个"感知层"，运行时探测环境，让下游代码按画像挑策略。**

比如：
- 判 marker 不存在（4 层 API 全说 False）时，如果**运行环境是 Samba**，就 sleep 11 秒（`DirectoryCache + 1`）再复查 —— 大概率是缓存假 miss，救回来就不重抽视频
- 如果环境是**本地 NTFS**，不 sleep，直接认定真 miss —— 本地无缓存问题
- 如果 `LongPathsEnabled=1`，`\\?\` 前缀就没必要（本文档里没实装，但方向如此）

---

## API 速查

```python
from env_probe import probe_and_log, get_env, should_do_samba_retry

# 1) GUI 启动阶段: 打一行画像到日志
probe_and_log(logger=self._log)
# -> [ENV] platform=nt mount=unc_direct samba=yes long_prefix=need dir_cache=10s probe=234ms

# 2) 拿画像结构体
env = get_env()
env.server_is_samba  # bool
env.smb_dir_cache_secs  # int (默认 10)
env.long_paths_enabled  # bool
env.mount_kind  # 'unc_direct' / 'mapped_drive' / 'local'

# 3) 决策辅助函数
if should_do_samba_retry():
    time.sleep(samba_retry_wait_secs())
```

---

## 新 GUI / CLI 接入清单（复制去打勾）

任何一个**新增的 GUI 或 CLI 工具**，都要按以下步骤接入 env_probe，防止上游下次改挂载时再翻车：

- [ ] **`__init__`（GUI）或 `main`（CLI）里加一行 `probe_and_log`**
  ```python
  # GUI (在 self._build_ui() 之后):
  try:
      from env_probe import probe_and_log
      self.root.after(100, lambda: probe_and_log(self._log))
  except Exception as _e:
      self._log(f"[ENV] probe_and_log 失败: {type(_e).__name__}: {_e}")

  # CLI (在 argparse 解析完之后, 大干之前):
  try:
      from env_probe import probe_and_log
      probe_and_log(lambda s: print(s, flush=True))
  except Exception as _e:
      print(f"[ENV] probe_and_log 失败: {type(_e).__name__}: {_e}", flush=True)
  ```

- [ ] **打包 workflow（.github/workflows/build-XXX.yml）加 hidden-import 和 copy**
  ```yaml
  # copy 步骤:
  copy env_probe.py build_XXX\
  # pyinstaller 命令加参数:
  --hidden-import env_probe ^
  ```

- [ ] **不要在业务逻辑里硬编码"是 Samba 就 sleep"**，改成 `if should_do_samba_retry(): ...`

- [ ] **不要给 env_probe.py 走 pyarmor 加密**（是环境探测，加密没意义）

---

## Env 字段说明

```python
@dataclass
class Env:
    platform: str                        # 'nt' / 'posix'
    mount_kind: str                      # 'unc_direct' / 'mapped_drive' / 'local'
    server_is_samba: bool                # 启发式判 Samba
    long_paths_enabled: bool             # Win10+ LongPathsEnabled 注册表
    smb_dir_cache_secs: int              # DirectoryCacheLifetime (默认 10)
    smb_file_notfound_cache_secs: int    # FileNotFoundCacheLifetime (默认 5)
    smb_file_info_cache_secs: int        # FileInfoCacheLifetime (默认 10)
    long_prefix_needed: bool             # True 时 >=180 字符路径需要 \\?\ 前缀
    probe_time_ms: int                   # 探测耗时
```

### 各字段怎么探测出来的

| 字段 | 数据源 | 备注 |
|---|---|---|
| `mount_kind` | `net use` 输出 + `%APPDATA%\...\Network Shortcuts\` 列表 | 启发式 |
| `server_is_samba` | Network Shortcuts 名字里有 "Samba" | 保守启发（漏报比误报好） |
| `long_paths_enabled` | `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` | REG_DWORD |
| `smb_*_cache_secs` | `HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters\*` | 读不到就用 Windows 默认 |

**所有注册表 / 命令都是只读**，不会改堡垒机任何配置。

---

## Samba retry 层怎么工作（v0.4.72 关键设计）

`winpath_util._safe_is_file_impl` 的判定瀑布，v0.4.72 加了第 6 层：

```
1) short_isfile  → True 就 return True
2) long_isfile   → True 就 return True
3) long_stat     → OK 就 return True
4) parent_listdir → HIT 就 return True
5) FindFirstFileW → HIT 就 return True
6) [v0.4.72 新增] should_do_samba_retry() 为真时:
   - sleep(dir_cache_secs + 1)  # 等 SMB Directory Cache 过期
   - 重跑 short_isfile / long_isfile / FindFirstFileW
   - 任一 True 就 return True
   - 全 False 才认定真 miss
```

**只在 Samba / UNC 直连时才 sleep**，本地盘 / Windows Server 环境不 sleep（省时间）。

代价：Samba 环境下真 miss 会多等 11 秒才开始重抽。可接受（抽帧本身几十秒，且真 miss 罕见）。

---

## 常见问题

**Q: 我加了一个新 GUI，忘了调 `probe_and_log`，会怎样？**
A: 功能上没影响（`winpath_util` 里的 retry 层是全局生效的），只是日志开头没有环境画像，出问题排查会累。

**Q: `env_probe` 探测慢不慢？**
A: 首次调用 ~200-500ms（跑 net use / 读注册表），结果缓存到进程单例，之后调用 0 开销。GUI 里用 `root.after(100, ...)` 异步跑，不阻塞界面。

**Q: 上游又换挂载方式了，比如 NFS / SFTP mount，怎么办？**
A: 在 `env_probe.py::_detect_mount_kind()` 加对应分支即可，**下游代码不用改**。这是本模块的核心价值 —— 隔离"探测"和"决策"。

**Q: 探测挂了怎么办（比如某个注册表键权限不够）？**
A: 所有 `_reg_read_*` / `_run` 都吞异常返回默认值，探测失败**不影响主流程**，最坏结果是画像不准（比如 samba=no 但实际是 yes），此时假 miss 会重抽视频 —— 就是"回到 v0.4.71 的行为"，不会更糟。

---

## 版本演进

| Tag | 改动 |
|---|---|
| v0.4.71 | diag_pic 加"环境体检"tab，用户手动跑一次看环境画像 |
| **v0.4.72** | **抽出 env_probe 模块，3 个 GUI + extract_frames 启动即探测**；`_safe_is_file_impl` 加 Samba retry 第 6 层 |

---

## 相关文档

- `docs/windows_long_path.md` §13 —— 长路径踩坑史（本模块的前身）
- `AGENTS.md` "Windows 文件 IO 必读" 章节 —— 硬规则清单
- `winpath_util.py` `_safe_is_file_impl` —— 判定瀑布实现
