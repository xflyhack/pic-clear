# child_exe_ver —— GUI 启动日志显示子 exe 版本

**引入版本**: v0.4.76
**位置**: `child_exe_ver.py`（明文，**不进 pyarmor gen**，跟 `env_probe.py` 同规格）

---

## 为什么要做

pic-clear 里 GUI 和干活 exe 是**独立打包**的：

| GUI                | 调用的干活 exe            |
|--------------------|--------------------------|
| `extract_gui.exe`  | `extract_frames.exe`     |
| `dedupe_gui.exe`   | `dedupe_pic.exe`         |
| `classify_gui.exe` | **内嵌 classify_pic**, 不走子 exe |

**踩过的坑（v0.4.75 事件）**：
用户把 `extract_gui.exe` 升级到 v0.4.75（含 v0.4.70 的 `FindFirstFileW` 兜底 +
v0.4.72 的 Samba 假 miss retry），但 **忘了同步升级 `extract_frames.exe`**，
`extract_frames.exe` 还是 v0.4.60 系列的老货。跑切帧还在打 `[MARKER_MISS]
parent_listdir=0 项`，用户以为修复没生效，反复找我改代码，实际根因是
**GUI 是新版，干活 exe 是旧版**。这种坑肉眼看不出来（都是 exe 双击运行），
只有点开 GUI 的"关于"tab 才知道，但用户不常点关于 tab。

**解决办法**：GUI 启动时**在日志开头**打一段 `[CORE]`，把子 exe 路径、
版本、跟 GUI 版本的一致性直接甩出来，用户一眼能看出来。

---

## 日志样例

**一致**（正常）：

```
====================================================================
[CORE] extract_frames.exe 版本探测
====================================================================
[CORE]   路径: D:\qzgj\extract_frames.exe
[CORE]   版本: extract_frames v0.4.76
[CORE]   一致性: ✓ (跟 GUI v0.4.76 一致)
====================================================================
```

**不一致**（子 exe 落后）：

```
====================================================================
[CORE] extract_frames.exe 版本探测
====================================================================
[CORE]   路径: D:\qzgj\extract_frames.exe
[CORE]   版本: extract_frames v0.4.70
[CORE]   一致性: ⚠ GUI 版本 v0.4.76 vs 子 exe 版本 v0.4.70, **子 exe 落后, 请更新!**
====================================================================
```

**缺失**：

```
====================================================================
[CORE] extract_frames.exe 版本探测
====================================================================
[CORE]   路径: (未找到 extract_frames.exe)
[CORE]   版本: N/A
[CORE]   ✘ 缺失内核 exe, 请把它放到 GUI 同目录 / System32 / PATH 后重启
====================================================================
```

---

## API

```python
from child_exe_ver import probe_and_log

probe_and_log(
    logger,                          # callable(str) / logging.Logger / None
    exe_finder=_find_extract_exe,    # 无参函数, 返回 exe 路径 str/None
    exe_name="extract_frames.exe",   # 打日志用的逻辑名
    gui_version=APP_VERSION,         # GUI 自己的版本, 用于一致性对比
)
```

内部流程：

1. 调 `exe_finder()` 拿 exe 路径
2. 跑 `<exe> --version`（超时 15s，`CREATE_NO_WINDOW` 不弹黑框）
3. 拿 stdout 第一行当版本
4. 从 GUI 版本 + 子 exe 版本各正则抽 `vX.Y.Z`，对比是否一致
5. 打 `[CORE]` 多行到日志

---

## 新 GUI 接入清单

如果新做的 GUI 走"子 exe 干活"模式（非 import 模式），必须做以下 5 件事：

1. **py 侧**：`_build_ui` 之后、`self.root.after(300, ...)` 之前加：
   ```python
   try:
       from child_exe_ver import probe_and_log as _core_probe
       def _run_core_probe():
           _core_probe(
               self._log,
               exe_finder=_find_xxx_exe,
               exe_name="xxx.exe",
               gui_version=APP_VERSION,
           )
       self.root.after(200, _run_core_probe)   # 在 env_probe(100) 之后
   except Exception as _e:
       self._log(f"[CORE] 探测失败: {type(_e).__name__}: {_e}")
   ```

2. **workflow 侧**（`build-xxx-gui-exe.yml`）：
   - `Prepare build folder` 加 `copy child_exe_ver.py build_xxx\`
   - pyinstaller 命令加 `--hidden-import child_exe_ver ^`

3. **不要**把 `child_exe_ver.py` 加进 `pyarmor gen` 列表（跟 `env_probe.py` 同硬规则）

4. **不要**改子 exe 侧（子 exe 只要有 `--version` 就行，已经有了）

5. **不要**改 classify_gui（它是 `import classify_pic`，不是子 exe 模式，
   直接走 `render_static_version_frame` 即可）

---

## 常见问题

**Q: 为什么日志里同时有 `[ENV]` 和 `[CORE]` 两段？**
A: `[ENV]` 是 env_probe 打的**运行环境画像**（磁盘/SMB/长路径开关），
`[CORE]` 是本模块打的**子 exe 版本**，两个是并列的启动 hook，
`env_probe` 200ms 前触发，`child_exe_ver` 200ms 后触发，不会互相阻塞 UI。

**Q: 子 exe 卡住了怎么办？**
A: `probe_child_exe` 有 15s 超时保护，超时打 `error=timeout_15s`，
不会卡住 GUI，最多让日志开头晚 15s 出现完整画像。

**Q: dev 版本怎么显示？**
A: 本地开发时 `_version.py` 是 `VERSION = "dev"`，`_extract_ver` 抽不出
`vX.Y.Z`，一致性判定放行（`matches_gui=True`），只做展示不告警。

---

## 相关文件

- `child_exe_ver.py` —— 本模块
- `env_probe.py` —— 同规格的环境探测模块（参考实现）
- `pipe_gui.py::render_core_version_frame` —— "关于" tab 里的图形化探测（同逻辑，tkinter 版）
- `docs/env_probe.md` —— 姊妹文档
- `docs/windows_long_path.md` —— 长路径/SMB 踩坑史（诠释为什么子 exe 版本对齐这么重要）
