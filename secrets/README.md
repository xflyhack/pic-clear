# ⚠️ 敏感目录 —— 私钥存放处

这里存放的 `private.pem` 是**用来签发 license.lic 的 RSA 私钥**。

## 用途
- `private.pem`：`gen_license.py` 需要它来给用户签发 license
- `public.pem`：仅作备份；实际打包时公钥已经硬编码在 `licensing.py` 里

## ⚠️ 安全提醒（务必遵守）

1. **这个仓库必须永远保持 private**。变成 public 的瞬间私钥就泄露。
2. **CI 日志里不要 `cat private.pem`**（当前 workflow 不会用到私钥，签发是本地做的，安全）
3. 如果需要**分享代码给外人看**，先把整个 `secrets/` 目录删掉再分享
4. 万一私钥泄露：
   - 生成新的密钥对（`openssl genrsa ...`）
   - 替换 `licensing.py` 里内嵌的公钥
   - 重新打包 exe
   - 通知所有用户重新申请 license

## 我为什么放到 git 里？

作者（xflyhack）自己决定：内部小工具，方便备份和多机使用，接受"仓库变 public 会导致方案失效"的风险。

## 快速签发 license

```bash
# 从任意机器 clone 后：
python gen_license.py <指纹> --issued-to <某人> \
    --private-key secrets/private.pem
```

或者干脆先把私钥拷到默认位置：

```bash
mkdir -p ~/.dedupe_pic_keys
cp secrets/private.pem ~/.dedupe_pic_keys/
chmod 600 ~/.dedupe_pic_keys/private.pem
# 之后就能直接 python gen_license.py <指纹> 不用 --private-key
```
