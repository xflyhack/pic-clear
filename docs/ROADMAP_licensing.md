# 授权保护方案（PyArmor + RSA 签名 + 机器指纹）

**状态**：待实现（未来做）
**创建**：2026-07-12
**依赖**：现有的 `dedupe_pic.exe`

---

## 决策清单（已定）

| 项 | 选择 |
|---|---|
| 加密方式 | **B + C 组合**：PyArmor 字节码加密 + RSA 签名机器指纹 |
| 用户规模 | 自己 / 少数同事，**一台机一份 license.lic** |
| 授权有效期 | **永久**（`license.lic` 里 `expire_date="never"`）|
| 机器指纹来源 | **主板序列号 + 磁盘 UUID + hostname**（拼接后 SHA256 取 16 位）|
| 授权流程 | 用户跑 exe 报错 → 打印指纹 → 发我 → 手工生成 → 回给用户 |
| 私钥保管 | **本机专用密钥**（不复用 SSH），存 `~/.dedupe_pic_keys/private.pem` |

## 重要澄清：不要用 SSH 密钥签 license

用户提到本机有 `~/.ssh/id_rsa`。**不用它来签 license**：

- SSH 密钥用于登录服务器/GitHub；license 签名是另一个安全域，两者混用等于任一泄露另一个也完蛋
- SSH 私钥格式是 OpenSSH，不是标准 PEM，`cryptography` 库要额外处理
- 备份/换机策略不同

**要专门生成一对 license 密钥**（未来开工第 1 步）：

```bash
mkdir -p ~/.dedupe_pic_keys && cd ~/.dedupe_pic_keys
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -pubout -out public.pem
chmod 600 private.pem
```

- `private.pem` 只留在你 Mac 上，**永不上传**
- `public.pem` 内嵌到 exe 里（放在 repo 中，被 PyInstaller 打包）

## 架构总览

```
┌───────── 你的开发机（Mac）─────────┐
│ private.pem  (仅本机)               │
│ gen_license.py                      │
│    ↓ 输入用户机器指纹                │
│    ↓ 用 private 签名                 │
│    ↓ 输出                            │
│ license.lic (发给用户)               │
└──────────────────────────────────────┘

┌───────── 用户机器（堡垒机 Win）───┐
│ dedupe_pic.exe                       │
│    ├─ 内嵌 public.pem                │
│    ├─ PyArmor 加密的字节码            │
│    └─ 启动时：                        │
│       1. 读机器指纹（主板+磁盘+主机名）│
│       2. 读同目录 license.lic         │
│       3. 用 public 验签               │
│       4. 校验 fingerprint 匹配        │
│       5. 通过则运行，否则退出并显示指纹 │
│ license.lic                           │
└──────────────────────────────────────┘
```

## 机器指纹算法

```
raw = motherboard_serial + "|" + disk_uuid + "|" + hostname
fingerprint = sha256(raw).hexdigest()[:16].upper()
展示：XXXX-XXXX-XXXX-XXXX（每 4 位一段，方便用户敲/发消息）
```

Windows 上获取原始值（用 `subprocess` 调，不引外部库）：

```python
# 主板序列号
subprocess.check_output(["wmic", "baseboard", "get", "serialnumber"])
# 也可用：subprocess.check_output(["wmic", "csproduct", "get", "uuid"])

# 磁盘 UUID（系统盘）
subprocess.check_output(["wmic", "diskdrive", "get", "serialnumber"])

# 主机名
socket.gethostname()
```

**注意**：
- 堡垒机克隆虚机可能主板序列号相同，加 hostname 才能区分
- 有些用户可能没 wmic 权限（较新 Windows 11 默认移除了 wmic）→ 降级用 `powershell Get-CimInstance Win32_BaseBoard`
- 抓不到任一值时，用 `"UNKNOWN"` 占位，保证不闪退

## license.lic 格式

```json
{
  "fingerprint": "A1B2-C3D4-E5F6-7890",
  "issued_to": "xflyhack@team",
  "issued_at": "2026-07-12",
  "expire_date": "never",
  "note": "任意备注"
}
```

**+ 签名**：把 JSON 序列化（`sort_keys=True, separators=(',',':')`）后用 RSA-PSS-SHA256 签名，Base64 编码，追加到 JSON 末尾成为一个字段 `signature`，或另存为一个独立的 `license.sig`。

推荐**单文件 license.lic**，结构：

```
LINE 1: BASE64(JSON payload)
LINE 2: BASE64(RSA-PSS-SHA256 signature)
```

好处：一个文件搞定，用户复制粘贴不会漏。

## PyArmor 加密

**目的**：把 `dedupe_pic.py`、`detector.py`、`licensing.py` 编译成加密字节码，让逆向者看到的只有花指令，无法直接改 `if license_ok:` 判断。

**免费版够用**（Python 3.11 支持）：

```bash
pip install pyarmor
pyarmor gen \
    --output build_obf \
    --enable-jit \
    --restrict \
    dedupe_pic.py detector.py licensing.py
# 产物：build_obf/dedupe_pic.py 等（已加密）+ build_obf/pyarmor_runtime_XXX/
```

然后用加密后的目录喂给 PyInstaller。

## 代码骨架（未来开工时写）

### `licensing.py`（新增文件）

```python
# -*- coding: utf-8 -*-
"""授权校验：机器指纹 + RSA 签名。"""
import base64, json, socket, subprocess, sys, hashlib
from pathlib import Path

# 打包时会被 PyInstaller 内嵌
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
...（构建时注入）...
-----END PUBLIC KEY-----"""


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _motherboard_serial() -> str:
    out = _run(["wmic", "baseboard", "get", "serialnumber"])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-1] if len(lines) > 1 else "UNKNOWN"


def _disk_uuid() -> str:
    out = _run(["wmic", "diskdrive", "get", "serialnumber"])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-1] if len(lines) > 1 else "UNKNOWN"


def get_fingerprint() -> str:
    raw = f"{_motherboard_serial()}|{_disk_uuid()}|{socket.gethostname()}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()
    return "-".join(h[i:i+4] for i in range(0, 16, 4))


def verify_license(license_path: Path) -> tuple[bool, str]:
    """返回 (是否有效, 原因)"""
    if not license_path.is_file():
        return False, "license.lic 不存在"

    try:
        lines = license_path.read_text().strip().splitlines()
        payload_b64, sig_b64 = lines[0], lines[1]
        payload = base64.b64decode(payload_b64)
        sig = base64.b64decode(sig_b64)
    except Exception as e:
        return False, f"license.lic 格式错误: {e}"

    # RSA 验签
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        pub = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)
        pub.verify(
            sig, payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except Exception:
        return False, "签名验证失败（license 被篡改或不是官方签发）"

    data = json.loads(payload)
    if data.get("fingerprint") != get_fingerprint():
        return False, f"授权与本机不匹配（本机指纹: {get_fingerprint()}）"

    # 到期检查（当前策略：expire_date="never" 表示永久）
    exp = data.get("expire_date", "never")
    if exp != "never":
        import datetime
        if datetime.date.today().isoformat() > exp:
            return False, f"授权已于 {exp} 到期"

    return True, f"授权有效（发放给: {data.get('issued_to', 'unknown')}）"
```

### `dedupe_pic.py` 开头集成

```python
def main() -> int:
    _force_utf8_stdio()

    # ==== 授权校验（在任何业务逻辑之前）====
    from licensing import get_fingerprint, verify_license
    exe_dir = Path(sys.argv[0]).resolve().parent
    ok, msg = verify_license(exe_dir / "license.lic")
    if not ok:
        print("=" * 60)
        print("[授权] 程序未授权，无法运行。")
        print(f"[授权] 原因: {msg}")
        print(f"[授权] 本机指纹: {get_fingerprint()}")
        print("[授权] 请把上面这一行指纹发给作者，获取 license.lic")
        print("[授权] 拿到后，把 license.lic 放到 dedupe_pic.exe 同目录再运行。")
        print("=" * 60)
        return 3
    print(f"[授权] {msg}")

    # ==== 原有业务逻辑 ====
    args = parse_args()
    ...
```

### `gen_license.py`（开发端工具，只在你 Mac 跑）

```python
#!/usr/bin/env python3
"""生成 license.lic 的工具。仅在开发机（Mac）运行。"""
import argparse, base64, json, sys
from datetime import date
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fingerprint", help="用户提供的机器指纹 XXXX-XXXX-XXXX-XXXX")
    ap.add_argument("--issued-to", default="user", help="授权对象姓名/工号")
    ap.add_argument("--expire", default="never", help="到期日期 YYYY-MM-DD 或 never")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--private-key", default=str(Path.home() / ".dedupe_pic_keys/private.pem"))
    ap.add_argument("--output", default="license.lic")
    args = ap.parse_args()

    payload = json.dumps({
        "fingerprint": args.fingerprint.upper(),
        "issued_to": args.issued_to,
        "issued_at": date.today().isoformat(),
        "expire_date": args.expire,
        "note": args.note,
    }, sort_keys=True, separators=(",", ":")).encode()

    priv = serialization.load_pem_private_key(
        Path(args.private_key).read_bytes(), password=None
    )
    sig = priv.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )

    Path(args.output).write_text(
        base64.b64encode(payload).decode() + "\n" +
        base64.b64encode(sig).decode() + "\n"
    )
    print(f"已生成 {args.output}")
    print("payload:", payload.decode())

if __name__ == "__main__":
    main()
```

用法：
```bash
python gen_license.py A1B2-C3D4-E5F6-7890 --issued-to xflyhack
# 得到 license.lic，通过任意渠道发给用户
```

## CI 集成（`.github/workflows/build-windows-exe.yml`）

**关键点**：`public.pem` 需要在 repo 里；`private.pem` 绝对不能进 repo/CI。

新增步骤（在 PyInstaller 之前）：

```yaml
- name: Inject public key into licensing.py
  run: |
    python -c "content = open('licensing.py').read().replace('# PLACEHOLDER_PUBLIC_KEY', open('public.pem').read()); open('licensing.py', 'w').write(content)"

- name: Obfuscate with PyArmor
  run: |
    pip install pyarmor
    pyarmor gen --output build_obf --enable-jit dedupe_pic.py detector.py licensing.py
    # 用 build_obf 里的加密版本替换原文件
    cp build_obf/dedupe_pic.py dedupe_pic.py
    cp build_obf/detector.py detector.py
    cp build_obf/licensing.py licensing.py

- name: Build EXE
  run: |
    pyinstaller --onefile ...
    # 额外要打包 pyarmor_runtime
```

## 用户操作流程

**第一次拿到 exe**：
```
1. 双击 dedupe_pic.exe 或 cmd 敲 dedupe_pic.exe
2. 报错：
   ============================================================
   [授权] 程序未授权，无法运行。
   [授权] 原因: license.lic 不存在
   [授权] 本机指纹: A1B2-C3D4-E5F6-7890
   [授权] 请把上面这一行指纹发给作者，获取 license.lic
   [授权] 拿到后，把 license.lic 放到 dedupe_pic.exe 同目录再运行。
   ============================================================
3. 把指纹 "A1B2-C3D4-E5F6-7890" 发给你（微信/邮件都行）
```

**你（作者）操作**：
```bash
python gen_license.py A1B2-C3D4-E5F6-7890 --issued-to "李四@某某公司"
# 得到 license.lic
# 发给用户（可 base64 编码后放在邮件正文/消息里）
```

**用户拿到 license.lic**：
```
1. 把 license.lic 放到 dedupe_pic.exe 同目录
2. 再运行 dedupe_pic.exe → 输出：
   [授权] 授权有效（发放给: 李四@某某公司）
   ============================================================
   扫描根目录: ...
```

## 破解成本估计

| 攻击者水平 | 破解难度 |
|---|---|
| 普通用户 | ❌ 拿到 exe 也跑不起（指纹不对）|
| 会用工具的初级逆向 | 想尝试 `IDA/Ghidra` 看代码 → 看到 PyArmor 花指令 → 大概率放弃 |
| 中级逆向 | 会脱 PyArmor（有开源工具），能看到解密后的 `.pyc`，进一步反编译。但要构造伪造的 license.lic 仍需**你的 RSA 私钥**，绕过要改二进制里的验签调用 → 有难度 |
| 高级逆向 | 不可挡。但值得为你这个工具花这么多精力的人极少 |

**结论**：对内部/朋友级传播的防护足够。**不要指望它防"专业黑产"**。

## 潜在风险 & 缓解

1. **克隆虚机指纹相同**：主板 UUID 一致时，加 hostname 区分；如果 hostname 也一样，只能人工分辨（比如给不同用户不同过期时间/备注）
2. **wmic 在新 Win 上被移除**：写降级路径到 `powershell Get-CimInstance`；再降级用 `UNKNOWN` 但把 hostname + IP 也拼进去
3. **PyArmor 免费版可能被 Defender 误报**：如果发生，考虑白名单化 exe hash，或改用付费 PyArmor Pro
4. **私钥泄露 = 灾难**：一旦泄露，所有已发放 license 依然可用，但你要**重新生成密钥对 → 换 public.pem → 重新打包 exe → 所有用户重新申请 license**。所以私钥要严格保管：
   - `chmod 600 ~/.dedupe_pic_keys/private.pem`
   - 别放 iCloud / Dropbox
   - 一份加密备份到 U 盘 / 硬盘

## 实施步骤清单（未来开工用）

- [ ] 本地生成 RSA 密钥对到 `~/.dedupe_pic_keys/`
- [ ] 把 `public.pem` 提交到 repo（`resources/public.pem`）
- [ ] 写 `licensing.py`（get_fingerprint + verify_license）
- [ ] 写 `gen_license.py`（本地工具）
- [ ] 在 `dedupe_pic.py` main() 开头集成授权校验
- [ ] CI workflow 增加 PyArmor 加密步骤
- [ ] 本地跑一次冒烟：
  - [ ] 无 license.lic → 打印指纹并退出
  - [ ] 错误 license.lic → 打印原因并退出
  - [ ] 手工签发 license → 正常运行
  - [ ] 指纹改一个字符 → 打印"不匹配"退出
- [ ] 更新 README 说明授权流程

## 未来可加的增强（不阻塞当前）

- 支持"多机 license"：payload 里放一个 `fingerprints: [...]` 数组
- 授权吊销名单（若不便联网，也可通过定期发放新版 exe 更新黑名单）
- License 用量统计：每次启动写一条本地日志，用户复核时可看到用了多少次
