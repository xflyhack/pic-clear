# 动态口令（TOTP / OTP）二次验证

**版本**：v0.3.0+

`license.lic` 是"这台机器允许使用"，`otp.secret` + 每天 6 位口令
是"今天这个人允许使用"。两者叠加即可实现"共享堡垒机 + 每人独立准入"。

---

## 一、给谁用？

- 堡垒机是**大家共用**的 Windows 机器
- `license.lic` 已经绑定机器指纹，任何人拿到机器都能双击 exe
- 想再加一层"每天首次启动必须输 6 位动态码"，避免同事误点或非授权使用

**不需要联网、不需要短信、不需要邮件**——完全离线，参考 RFC 6238。

---

## 二、作者签发流程（Mac 上一次）

依赖：Python 3（用仓库自带 venv 就行）。

```bash
# 生成密钥并写 otp.secret（顺手写一份，给用户拷到 exe 目录）
/tmp/pic_venv/bin/python otp_admin.py generate E915-F232-792C-5B41 \
    --issued-to xflyhack \
    --write-secret-to /tmp/otp.secret

# 看当前 6 位码（用户忘了带手机时口报）
/tmp/pic_venv/bin/python otp_admin.py current E915-F232-792C-5B41

# 每秒刷新
/tmp/pic_venv/bin/python otp_admin.py current E915-F232-792C-5B41 -w

# 生成 otpauth:// URI（可以喂给 Google / 微软 Authenticator 扫码）
/tmp/pic_venv/bin/python otp_admin.py uri E915-F232-792C-5B41

# 列出所有已签发的机器
/tmp/pic_venv/bin/python otp_admin.py list
```

作者机器上的密钥库位置：`~/.pic-clear-otp/<指纹>.json`，权限 `0600`。
把 `otp.secret`（**一行 base32 字符串**）随 `license.lic` 一起发给用户。

---

## 三、多机器网页面板

想同时看很多机器的实时口令：

```bash
python3 otp_web.py                       # 默认监听 127.0.0.1:5000
python3 otp_web.py --host 0.0.0.0 --port 8080
```

浏览器打开 `http://127.0.0.1:5000`：
- 黑色渐变毛玻璃主题
- 每台机器一张卡：**指纹 / 发放对象 / 大字号 6 位口令 / 30 秒环形倒计时**
- 点数字复制到剪贴板
- 每秒自动刷新

---

## 四、用户使用（堡垒机 / Windows）

### 1. 拿到两个文件

- `license.lic`（授权）
- `otp.secret`（动态口令密钥，一行 base32）

**放在 exe 同目录**（跟 `pipe_gui.exe` / `extract_gui.exe` / `dedupe_gui.exe`
并列）。

### 2. 双击 exe

- 授权通过 → 弹出「pic-clear 动态口令」对话框
- 输入手机 Authenticator 或作者口报的 6 位数字，回车
- 满 6 位自动提交
- **今天首次输对**后 24 小时内启动都免输入（三个 GUI 共用一份 session）

### 3. 兼容与容错

- 没有 `otp.secret` → **自动跳过 OTP 验证**（向后兼容旧用户）
- 环境变量 `PIC_CLEAR_SKIP_OTP=1` → 跳过（开发调试用）
- 错 3 次 → 冷却 60 秒
- 容忍窗口 **±90 秒**，堡垒机跟真实时间小偏差不影响
- Session 文件：`%USERPROFILE%\.pic-clear\otp_session.json`
- 用户关掉对话框 → `sys.exit(4)`（跟 license 未通过的 exit 3 分开）

---

## 五、常见问题

**Q**：Session 24 小时到期后又要重新输？
**A**：对。想改天数改 `pipe_gui.py` 里 `OTP_SESSION_TTL`。

**Q**：想强制退出 session、下次启动必须输？
**A**：删掉 `%USERPROFILE%\.pic-clear\otp_session.json` 即可。

**Q**：换手机 / 换 Authenticator 怎么办？
**A**：作者跑一次 `otp_admin.py rotate <指纹>` 生成新密钥，把新的
`otp.secret` 发过去覆盖，用户重新扫码 / 重新拿 URI。

**Q**：算法是标准 TOTP 吗？
**A**：是。`SHA1` + 30 秒周期 + 6 位数字，兼容 Google Authenticator /
微软 Authenticator / Authy / 1Password。已过 RFC 6238 官方测试向量。
