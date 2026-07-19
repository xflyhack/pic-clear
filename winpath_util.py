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


def safe_is_file(p) -> bool:
    try:
        if os.path.isfile(str(p)):
            return True
    except OSError:
        pass
    long_p = to_long_path(str(p))
    if long_p == str(p):
        return False
    try:
        return os.path.isfile(long_p)
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


def safe_mkdir(p, parents: bool = True, exist_ok: bool = True) -> None:
    r"""os.makedirs 的长路径安全版. extract_frames v0.4.61 新增.

    症状: 堡垒机 Z:\节点\... 深目录 mkdir 报
    "FileNotFoundError: [WinError 206] 文件名或扩展名太长."
    """
    p_str = str(p)
    try:
        os.makedirs(p_str, exist_ok=exist_ok) if parents else os.mkdir(p_str)
        return
    except OSError as e_short:
        long_p = to_long_path(p_str)
        if long_p == p_str:
            raise
        try:
            os.makedirs(long_p, exist_ok=exist_ok) if parents else os.mkdir(long_p)
            return
        except FileExistsError:
            if exist_ok:
                return
            raise
        except OSError:
            raise e_short


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
