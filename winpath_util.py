"""Windows 长路径 / 映射盘 / tkinter 混斜杠公共工具模块.

背景与硬规则详见 docs/windows_long_path.md.
这里把原本各自散落在 dedupe_pic.py / detector.py / diag_pic.py 里的
helper 抽出来集中维护, 新工具 (如 extract_frames.py) 直接 import 即可.

对外 API 一律**不带下划线前缀** (方便新代码直接 from winpath_util import ...);
老代码为了减小 diff, 在文件顶部起 _xxx 别名 (from winpath_util import xxx as _xxx).

非 Windows 平台 (Mac / Linux) 上, 所有函数**原样退回**普通实现,
零回归、零性能损失.
"""
from __future__ import annotations

import os
import shutil
import sys


# ------------------------------ 常量 -----------------------------------

# 加 \\?\ 前缀的长度阈值. 取 180 而不是 240/260 是为了给 SMB / UNC 展开留余量:
# 堡垒机 Z: -> \\filestor01.cloud-prod.seres.cn\kj-e68-datamark-100 展开后
# 会比盘符多 50+ 字符.
LONG_PATH_THRESHOLD = 180


# ---------------------- 映射盘符 -> UNC (仅诊断用) ----------------------

_MAPPED_DRIVE_CACHE: dict[str, str | None] = {}


def resolve_mapped_drive_to_unc_verbose(drive_letter: str) -> tuple[int, str | None]:
    r"""WNetGetConnectionW 原始返回值 + UNC.

    返回 (rc, unc_or_errmsg):
      -  0: 成功, unc = "\\server\share"
      - -1: 非 Windows
      - -2: 参数非 X: 形式
      - -99: ctypes 异常, 第二个字段是 repr(exception)
      -  非 0 (Windows 错误码): unc = None
    """
    if os.name != "nt":
        return (-1, None)
    key = drive_letter.upper().rstrip("\\/")
    if not (len(key) == 2 and key[1] == ":"):
        return (-2, None)
    try:
        import ctypes
        from ctypes import wintypes

        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        WNetGetConnectionW = mpr.WNetGetConnectionW
        WNetGetConnectionW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        WNetGetConnectionW.restype = wintypes.DWORD

        buf_len = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        rc = WNetGetConnectionW(key, buf, ctypes.byref(buf_len))
        return (int(rc), buf.value if rc == 0 else None)
    except Exception as e:
        return (-99, f"ctypes exception: {e!r}")


def resolve_mapped_drive_to_unc(drive_letter: str) -> str | None:
    r"""缓存版, 只返回 UNC (\\server\share) 或 None. 供诊断日志 append 使用.

    - 保留仅供诊断展示 (堡垒机实测 \\?\UNC\ 反而打不开, 不参与真实路径拼接)
    - 结果按盘符字母 (大写) 缓存到进程内, 反复调用零开销
    """
    if os.name != "nt":
        return None
    key = drive_letter.upper().rstrip("\\/")
    if not (len(key) == 2 and key[1] == ":"):
        return None
    if key in _MAPPED_DRIVE_CACHE:
        return _MAPPED_DRIVE_CACHE[key]
    rc, val = resolve_mapped_drive_to_unc_verbose(key)
    unc: str | None = None
    if rc == 0 and isinstance(val, str) and val.startswith("\\\\"):
        unc = val.strip()
    _MAPPED_DRIVE_CACHE[key] = unc
    return unc


# --------------------------- 路径归一化 --------------------------------

def normalize_windows_path(image_path) -> str:
    r"""把 tkinter/filedialog 混用的 //?/ 前缀 + 正斜杠路径统一成 Windows 惯用形式.

    背景 (v0.4.34): tkinter filedialog 在长路径下会把返回值里的 \ 全部转成 /,
    并且已经悄悄加了 \\?\ 前缀. 这样一路传到 to_long_path 时:
      - startswith("\\?\\") 判断失效 (实际是 //?/)
      - 又叠一层 \\?\ 变双重前缀 -> FileNotFoundError
    这里做的事:
      1) 正斜杠全部换成反斜杠
      2) 已带 \\?\ 前缀就保留 (不重复加)
      3) 非 Windows 原样返回
    """
    s = str(image_path)
    if os.name != "nt":
        return s
    if "/" in s:
        s = s.replace("/", "\\")
    while s.startswith("\\\\?\\\\\\?\\"):
        s = s[4:]
    return s


def to_long_path(image_path, threshold: int = LONG_PATH_THRESHOLD) -> str:
    r"""Windows 上超过 threshold 字符的绝对路径转成 \\?\ 前缀.

    v0.4.34 起入口先走 normalize_windows_path 修复 tkinter 的 //?/ 混斜杠坑.
    """
    s = normalize_windows_path(image_path)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\") or s.startswith("\\?\\"):
        return s
    if len(s) < threshold:
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    try:
        abs_s = os.path.abspath(s)
    except Exception:
        abs_s = s
    if abs_s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_s.lstrip("\\")
    return "\\\\?\\" + abs_s


def long_path_prefix(p: str) -> str:
    r"""对映射盘符 / 本地盘直接套 \\?\, 对 UNC 套 \\?\UNC\. 无长度阈值判断.

    diag_pic 用: 诊断工具要"强制加前缀", 不管路径多短.
    """
    if os.name != "nt":
        return p
    p = normalize_windows_path(p)
    if p.startswith("\\\\?\\") or p.startswith("\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


# ------------------------------ PIL 兜底 -------------------------------

# 首次失败最多打印几条完整诊断日志. 全进程共享一份计数, 避免多模块合起来刷屏.
PIL_DIAG_LEFT = 3


def pil_diag(msg: str) -> None:
    """把 PIL 长路径相关的诊断日志打到 stderr, 不污染 stdout 报告."""
    try:
        sys.stderr.write("[PIL诊断] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def pil_open(image_path):
    r"""PIL Image.open 的长路径安全版本. 调用方仍需自行 close / with.

    策略 (堡垒机 v0.4.32 起):
      1) 先按 to_long_path 结果调 Image.open (短路径 / \\?\Z:\... 直通)
      2) 失败再兜底: 用 Python builtin open('rb') 把整个文件读到 BytesIO 后 Image.open,
         绕开 PIL 走 CRT fopen 对 \\?\ 挑食的坑
      3) 前 N 次失败在 stderr 打一条诊断 (原路径 / 长路径 / 异常 / WNetGetConnectionW)
    """
    from PIL import Image  # 延迟 import, 避免非图片场景 (extract_frames) 拉进 PIL

    long_path = to_long_path(image_path)
    try:
        return Image.open(long_path)
    except Exception as e1:
        global PIL_DIAG_LEFT
        original = str(image_path)
        # 兜底: 二进制预读 -> BytesIO
        try:
            with open(long_path, "rb") as f:
                data = f.read()
        except Exception as e2:
            if PIL_DIAG_LEFT > 0:
                PIL_DIAG_LEFT -= 1
                pil_diag(f"path={original} len={len(original)}")
                pil_diag(f"  long_path={long_path} len={len(long_path)}")
                pil_diag(f"  Image.open ERR {type(e1).__name__}: {e1}")
                pil_diag(f"  open('rb') ERR {type(e2).__name__}: {e2}")
                if os.name == "nt" and len(original) >= 2 and original[1] == ":":
                    unc = resolve_mapped_drive_to_unc(original[:2])
                    pil_diag(f"  WNetGetConnection({original[:2]}) = {unc!r}")
            raise
        try:
            import io
            bio = io.BytesIO(data)
            img = Image.open(bio)
            if PIL_DIAG_LEFT > 0:
                PIL_DIAG_LEFT -= 1
                pil_diag(
                    f"path={original} len={len(original)} "
                    f"-> Image.open 失败, BytesIO 兜底 OK bytes={len(data)}"
                )
                pil_diag(f"  first-error Image.open {type(e1).__name__}: {e1}")
            return img
        except Exception as e3:
            if PIL_DIAG_LEFT > 0:
                PIL_DIAG_LEFT -= 1
                pil_diag(f"path={original} len={len(original)}")
                pil_diag(f"  long_path={long_path} len={len(long_path)}")
                pil_diag(f"  Image.open ERR {type(e1).__name__}: {e1}")
                pil_diag(f"  BytesIO Image.open ERR {type(e3).__name__}: {e3}")
                pil_diag(f"  file bytes read OK, size={len(data)}")
            raise


# --------------------- pathlib IO 安全版 (长路径兜底) ------------------

# 每一个 safe_* 都是: 先试原路径 (兼容非 Windows + 短路径直通),
# 挂了再走 to_long_path 兜底, 长路径也说 FileNotFoundError 才算真不存在.


def safe_stat(p) -> "os.stat_result":
    try:
        return os.stat(str(p))
    except OSError:
        long_p = to_long_path(str(p))
        if long_p == str(p):
            raise
        return os.stat(long_p)


def safe_unlink(p) -> None:
    r"""v0.4.38: FileNotFoundError 不再直接 return, 先走 \\?\ 兜底再试.

    背景: 老版本把"长路径卡在 CRT 层根本没到 SMB 就报 WinError 3"跟"真的不存在"
    混为一谈, 导致 42 张图删除计数 +1 但实际一个都没删.
    """
    try:
        os.unlink(str(p))
        return
    except OSError as e_short:
        long_p = to_long_path(str(p))
        if long_p == str(p):
            if isinstance(e_short, FileNotFoundError):
                return
            raise
        try:
            os.unlink(long_p)
            return
        except FileNotFoundError:
            return


def safe_exists(p) -> bool:
    try:
        if os.path.exists(str(p)):
            return True
    except OSError:
        pass
    long_p = to_long_path(str(p))
    if long_p == str(p):
        return False
    try:
        return os.path.exists(long_p)
    except OSError:
        return False


def safe_is_file(p, skip_samba_retry: bool = False) -> bool:
    r"""长路径安全的 isfile 判定.

    skip_samba_retry=True 用于 "删除后确认文件真没了" 这种场景:
    不需要 sleep 11 秒等 SMB 缓存 (文件本来就该 miss).
    默认 False 保留原行为.
    """
    return _safe_is_file_impl(p, skip_samba_retry=skip_samba_retry)[0]



# --------------------- Win32 FindFirstFileW 兜底 (v0.4.70) --------------------

def _find_first_file_w(long_path: str) -> tuple[str, str]:
    r"""ctypes 直调 kernel32!FindFirstFileW, 绕开 Python IO 层归一化.

    返回 (status, detail):
      - ('HIT', f"attr={n}")  文件存在, attr = dwFileAttributes 十进制
      - ('MISS', 'INVALID_HANDLE')  Win32 说找不到
      - ('MISS', f'ERR:{errno}')    其他 Win32 错误码
      - ('SKIP', 'non-nt')          非 Windows 平台
      - ('SKIP', f'exc:{repr(e)}') ctypes 调用自身炸了
    """
    if os.name != "nt":
        return ("SKIP", "non-nt")
    try:
        import ctypes
        from ctypes import wintypes

        INVALID_HANDLE_VALUE = -1

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        class WIN32_FIND_DATAW(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME),
                ("ftLastWriteTime", FILETIME),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("dwReserved0", wintypes.DWORD),
                ("dwReserved1", wintypes.DWORD),
                ("cFileName", wintypes.WCHAR * 260),
                ("cAlternateFileName", wintypes.WCHAR * 14),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        FindFirstFileW = k32.FindFirstFileW
        FindFirstFileW.argtypes = [wintypes.LPCWSTR,
                                   ctypes.POINTER(WIN32_FIND_DATAW)]
        FindFirstFileW.restype = wintypes.HANDLE
        FindClose = k32.FindClose
        FindClose.argtypes = [wintypes.HANDLE]
        FindClose.restype = wintypes.BOOL

        data = WIN32_FIND_DATAW()
        h = FindFirstFileW(long_path, ctypes.byref(data))
        if h == INVALID_HANDLE_VALUE or h == ctypes.c_void_p(-1).value:
            err = ctypes.get_last_error()
            if err == 0:
                return ("MISS", "INVALID_HANDLE")
            return ("MISS", f"ERR:{err}")
        try:
            attr = data.dwFileAttributes
            found_name = data.cFileName
        finally:
            FindClose(h)
        # 拿到的 found_name 是实际存在的文件名. 跟我们查的 basename 对一下,
        # 大小写 / 空格 / 隐形字符差异都能看出来.
        base = os.path.basename(long_path)
        return ("HIT", f"attr={attr} found={found_name!r} query={base!r}")
    except Exception as e:
        return ("SKIP", f"exc:{e!r}")


def _safe_is_file_impl(p, skip_samba_retry: bool = False) -> tuple[bool, dict]:
    r"""safe_is_file 内部实现, 顺带把短路径 / 长路径 / stat 三条查询的原始
    结果收集起来. 上层判 False 时可以把 diag 打日志, 免得再回来加断点.

    diag dict keys (全为字符串, 缺失表示未走到这条分支):
      - 'short_isfile': 'True' / 'False' / 'ERR:<type>:<msg>'
      - 'long_p'      : 长路径字符串 (若走了 to_long_path)
      - 'long_isfile' : 同 short_isfile 三种取值 (若走了长路径)
      - 'long_stat'   : 'OK size=<n>' / 'ERR:<type>:<msg>' (若长路径 isfile False)
      - 'parent_listdir': 'HIT ...' / 'MISS ...' / 'ERR:...' (v0.4.69 兜底, 若 stat 挂)
    """
    diag: dict[str, str] = {}
    p_str = str(p)
    try:
        r = os.path.isfile(p_str)
        diag["short_isfile"] = "True" if r else "False"
        if r:
            return True, diag
    except OSError as e:
        diag["short_isfile"] = f"ERR:{type(e).__name__}:{e}"
    long_p = to_long_path(p_str)
    if long_p == p_str:
        return False, diag
    diag["long_p"] = long_p
    try:
        r = os.path.isfile(long_p)
        diag["long_isfile"] = "True" if r else "False"
        if r:
            return True, diag
    except OSError as e:
        diag["long_isfile"] = f"ERR:{type(e).__name__}:{e}"
        return False, diag
    # 长路径 isfile 说 False, 再用 os.stat 兜一层, 有时 stat 能过 (SMB quirk)
    try:
        st = os.stat(long_p)
        diag["long_stat"] = f"OK size={st.st_size}"
        # stat 能过, 说明文件其实在 -> 认为存在
        return True, diag
    except OSError as e:
        diag["long_stat"] = f"ERR:{type(e).__name__}:{e}"
    # v0.4.69: isfile/stat 全挂时, 用父目录 listdir 兜底.
    # 现象: Windows Python IO 层对 \\?\UNC\ 深路径 (>260 字符) 单文件 stat 会
    # 返回 WinError 2 "找不到", 但 scandir/listdir 却能正常列出该文件.
    # diag_pic v0.4.68 已实证 (268 字符 marker 全根 rglob 短路径漏, 长路径
    # 列出 1 条, 但对单条 marker 直接 isfile 又都说 False). 按 listdir 判定为准.
    try:
        parent = os.path.dirname(long_p)
        fname  = os.path.basename(long_p)
        names  = os.listdir(parent)
        if fname in names:
            diag["parent_listdir"] = f"HIT (parent 有 {len(names)} 项, 含目标)"
            return True, diag
        diag["parent_listdir"] = f"MISS (parent 有 {len(names)} 项, 不含目标)"
    except OSError as e:
        diag["parent_listdir"] = f"ERR:{type(e).__name__}:{e}"

    # v0.4.70: listdir 也说没有时, 直调 Win32 FindFirstFileW 再兜一层.
    # 现象: Python IO 层 (os.stat / os.listdir 内部走 CRT + Win32) 对某些
    # SMB + 深路径 + 含特殊字符 (空格 / 加号 / 中文混排) 的组合会撒谎;
    # FindFirstFileW 是最贴近 Win32 kernel 的查询, 不走 CRT 归一化, 常能救回来.
    status, detail = _find_first_file_w(long_p)
    diag["find_first"] = f"{status} {detail}"
    if status == "HIT":
        return True, diag

    if skip_samba_retry:
        # v0.4.115: 调用方明确不需要 samba retry (比如 do_delete 确认文件已删)
        # 不 sleep, 直接返回 miss 结论.
        return False, diag

    # v0.4.72: Samba + Windows SMB Client 缓存假 miss 兜底.
    # 现象: Samba 服务端不发 SMB Change Notify, Windows 客户端只能靠
    # DirectoryCacheLifetime (默认 10 秒) 缓存过期. 抽帧刚写完 marker 立刻查
    # 隔壁 marker, 会读到 10 秒前的"空目录"缓存 -> 假 miss -> 视频被重抽.
    # 只在 env_probe 判定 "should_do_samba_retry" 时才 sleep, 避免本地盘 / Windows Server
    # 环境无谓浪费.
    try:
        from env_probe import should_do_samba_retry, samba_retry_wait_secs
        if should_do_samba_retry():
            wait_s = samba_retry_wait_secs()
            if wait_s > 0:
                import time as _time
                _time.sleep(wait_s)
                # 复查最有希望的三条 (isfile 短/长 + FindFirstFile), listdir 不再重跑
                # (10 秒后 SMB 缓存已过期, 短路径 isfile 大概率就 True 了)
                try:
                    if os.path.isfile(p_str):
                        diag["retry_short_isfile"] = "True"
                        return True, diag
                    diag["retry_short_isfile"] = "False"
                except OSError as e:
                    diag["retry_short_isfile"] = f"ERR:{type(e).__name__}:{e}"
                try:
                    if os.path.isfile(long_p):
                        diag["retry_long_isfile"] = "True"
                        return True, diag
                    diag["retry_long_isfile"] = "False"
                except OSError as e:
                    diag["retry_long_isfile"] = f"ERR:{type(e).__name__}:{e}"
                status2, detail2 = _find_first_file_w(long_p)
                diag["retry_find_first"] = f"{status2} {detail2}"
                if status2 == "HIT":
                    return True, diag
    except Exception as e:
        # env_probe 加载失败: 老版本兼容, 不影响原判定
        diag["retry_skipped"] = f"{type(e).__name__}:{e}"

    return False, diag


def safe_isdir(p) -> bool:
    r"""Path.is_dir 的长路径安全版. v0.4.64 新增, safe_mkdir 用它做建后验证.

    跟 safe_is_file 一个套路: 短路径先试, 挂了或 False 再走 \\?\ fallback.
    """
    try:
        if os.path.isdir(str(p)):
            return True
    except OSError:
        pass
    long_p = to_long_path(str(p))
    if long_p == str(p):
        return False
    try:
        return os.path.isdir(long_p)
    except OSError:
        return False


def safe_move(src, dst) -> None:
    r"""v0.4.38: 跟 safe_unlink 同源修法."""
    try:
        shutil.move(str(src), str(dst))
        return
    except OSError:
        pass
    long_src = to_long_path(str(src))
    long_dst = to_long_path(str(dst))
    if long_src == str(src) and long_dst == str(dst):
        raise
    shutil.move(long_src, long_dst)


def _mkdir_diag(msg: str) -> None:
    r"""v0.4.64: safe_mkdir 遇到 SMB + \\?\UNC\ 组合坑时打诊断到 stderr."""
    try:
        sys.stderr.write("[MKDIR_QUIRK] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def safe_mkdir(p, parents: bool = True, exist_ok: bool = True) -> None:
    r"""os.makedirs 的长路径安全版, 带建后验证 (v0.4.64 加固).

    历史 (v0.4.61 -> v0.4.63 遗留 bug):
      堡垒机 Z:\节点\... 或 \\filestor01\...\ 深目录 (>=260 字符) 上,
      短路径 os.makedirs 报 WinError 206, fallback 到 \\?\UNC\...\os.makedirs
      在 SMB 上有 quirk: 中间某层会抛 FileExistsError, 老代码
      "except FileExistsError: if exist_ok: return" 静默吞掉, 返回"成功",
      但实际叶节点目录没建出来. 下游 shutil.move 全挂 FileNotFoundError.

    v0.4.64 修法:
      1) 遇到 FileExistsError **不再无条件吞**, 只当叶节点已存在才 return;
         中间层的 FileExistsError 属于 SMB quirk, 打 [MKDIR_QUIRK] 日志继续.
      2) 建完做一次 safe_isdir 显式验证, 验证不通过明确 raise, 不撒谎.
      3) 长路径 fallback 也挂时 raise 的是 long_p 版实际错误 (不再 raise
         e_short), 便于人眼追问题.
    """
    p_str = str(p)
    # ---- 短路径 ----
    try:
        os.makedirs(p_str, exist_ok=exist_ok) if parents else os.mkdir(p_str)
        # 短路径 makedirs 认为成功 -> 验证一次
        if safe_isdir(p_str):
            return
        # 短路径撒谎: 报了成功但 isdir 返回 False (罕见, 但见过 SMB 缓存)
        _mkdir_diag(
            f"短路径 makedirs 返回成功但 isdir=False: {p_str}"
        )
        # 继续走长路径 fallback 再试一次
    except OSError as e_short:
        long_p = to_long_path(p_str)
        if long_p == p_str:
            # 没有长路径可换, 相信短路径的错
            raise
        # 走长路径 fallback
        try:
            os.makedirs(long_p, exist_ok=exist_ok) if parents else os.mkdir(long_p)
        except FileExistsError as e_fe:
            # v0.4.64 关键修法: 只有叶节点已存在时才认为 OK; 中间层的
            # FileExistsError (SMB + \\?\UNC\ quirk) 打日志继续验证.
            if exist_ok and safe_isdir(long_p):
                return
            _mkdir_diag(
                f"长路径 makedirs 抛 FileExistsError 但叶节点 isdir=False: "
                f"{long_p} -> {type(e_fe).__name__}: {e_fe}"
            )
            # 落到最后的验证兜底
        except OSError as e_long:
            # 长路径 fallback 也挂: raise 长路径版实际错误, 不再 raise e_short
            # (老版本 raise e_short 会遮住真实原因)
            raise e_long
    # ---- 兜底验证: 无论走的哪条路径, 最后都要保证叶节点存在 ----
    if safe_isdir(p_str):
        return
    long_p = to_long_path(p_str)
    if long_p != p_str and safe_isdir(long_p):
        return
    raise OSError(
        f"safe_mkdir 声称成功但目录未真实创建: p={p_str} long_p={long_p}"
    )


def safe_read_text(p, encoding: str = "utf-8", errors: str = "strict") -> str:
    long_p = to_long_path(str(p))
    try:
        with open(long_p, "r", encoding=encoding, errors=errors) as f:
            return f.read()
    except OSError:
        if long_p != str(p):
            raise
        with open(str(p), "r", encoding=encoding, errors=errors) as f:
            return f.read()


def safe_write_text(p, text: str, encoding: str = "utf-8") -> None:
    long_p = to_long_path(str(p))
    try:
        with open(long_p, "w", encoding=encoding) as f:
            f.write(text)
        return
    except OSError:
        if long_p == str(p):
            raise
        with open(str(p), "w", encoding=encoding) as f:
            f.write(text)


def safe_glob(dir_path, pattern: str) -> list:
    r"""Path(dir_path).glob(pattern) 的长路径安全版. 返回 list[Path] (方便统计 len).

    Windows Path.glob 内部走 scandir, MAX_PATH 一样会卡; 长路径下改走 to_long_path
    转出的 \\?\ 目录再 scandir.
    """
    from pathlib import Path

    d = Path(dir_path)
    try:
        return list(d.glob(pattern))
    except OSError:
        pass
    long_d = to_long_path(str(d))
    if long_d == str(d):
        return []
    try:
        return list(Path(long_d).glob(pattern))
    except OSError:
        return []


def safe_os_open(p, flags: int, mode: int = 0o644) -> int:
    r"""os.open 的长路径安全版, 供 O_CREAT|O_EXCL 抢锁场景."""
    p_str = str(p)
    try:
        return os.open(p_str, flags, mode)
    except OSError:
        long_p = to_long_path(p_str)
        if long_p == p_str:
            raise
        return os.open(long_p, flags, mode)
