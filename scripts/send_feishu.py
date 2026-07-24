"""发飞书群机器人卡片: 新版本发布通知.

被 GitHub Actions upload-to-us.yml 调用. 从环境变量拿参数, 不带 sys.argv,
方便 workflow 里 env: 直接列出所有输入.

必填 env:
  FEISHU_WEBHOOK       完整 webhook URL
  TAG                  版本号, 例如 v0.4.119
  FILES_TXT            一个纯文本文件, 每行: "filename  size_bytes  sha256"
                       (workflow 里跑 sha256sum + stat 生成)
  DOWNLOAD_BASE        下载中心根 URL, 例如 http://download.shuqiit.com
  GITHUB_REPO          "USER/REPO", 用来拼 GitHub Release 链接

可选 env:
  FEISHU_SECRET        签名密钥. 空则不签名.

失败策略: 打印错误, 但 sys.exit(0), 通知不成功不让整个 workflow 变红.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _sign(secret: str, ts: int) -> str:
    """飞书签名: base64(HMAC-SHA256(f'{ts}\\n{secret}', b''))."""
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256)
    return base64.b64encode(h.digest()).decode("utf-8")


def _human_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def _read_files(path: str) -> list[dict]:
    """读 FILES_TXT: 每行 'filename  size_bytes  sha256', 空行忽略."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            filename = parts[0]
            try:
                size_bytes = int(parts[1])
            except ValueError:
                continue
            sha = parts[2] if len(parts) >= 3 else ""
            out.append({
                "filename": filename,
                "size_bytes": size_bytes,
                "size_human": _human_size(size_bytes),
                "sha256": sha,
            })
    # 按大小降序, 让大文件在前面更醒目
    out.sort(key=lambda x: x["size_bytes"], reverse=True)
    return out


def _build_card(
    tag: str,
    files: list[dict],
    download_base: str,
    github_repo: str,
) -> dict:
    """飞书 interactive card JSON.
    参考: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/feishu-cards
    """
    base = download_base.rstrip("/")
    version_url = f"{base}/v/{tag}"
    gh_url = f"https://github.com/{github_repo}/releases/tag/{tag}"

    # 用 lark_md 让我们能加粗 / 用 monospace
    file_lines = []
    for f in files:
        # 每行: **filename** — size  · sha256 短
        sha_part = f"  ·  `{f['sha256'][:12]}…`" if f["sha256"] else ""
        file_lines.append(
            f"• **{f['filename']}**  ({f['size_human']}){sha_part}"
        )
    files_md = "\n".join(file_lines) if file_lines else "_(无文件)_"

    total_bytes = sum(f["size_bytes"] for f in files)
    total_human = _human_size(total_bytes)

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": f"🎉 数旗 pic-clear {tag} 已发布",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{len(files)} 个 exe** 已镜像到国内下载中心，"
                        f"总计 {total_human}。点下方按钮直接下载。"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": files_md},
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "📥 打开下载中心",
                        },
                        "type": "primary",
                        "url": version_url,
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "GitHub Release",
                        },
                        "type": "default",
                        "url": gh_url,
                    },
                ],
            },
        ],
    }
    return card


def _post(webhook: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("[feishu] FEISHU_WEBHOOK 未设置, 跳过通知", file=sys.stderr)
        return 0  # 不失败

    tag = os.environ.get("TAG", "").strip()
    files_txt = os.environ.get("FILES_TXT", "").strip()
    download_base = os.environ.get("DOWNLOAD_BASE", "").strip()
    github_repo = os.environ.get("GITHUB_REPO", "").strip()

    if not (tag and files_txt and download_base and github_repo):
        print(
            "[feishu] 缺少必填参数 (TAG/FILES_TXT/DOWNLOAD_BASE/GITHUB_REPO), 跳过",
            file=sys.stderr,
        )
        return 0

    files = _read_files(files_txt)
    card = _build_card(tag, files, download_base, github_repo)

    payload: dict = {
        "msg_type": "interactive",
        "card": card,
    }

    # 可选签名
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(secret, ts)

    status, body = _post(webhook, payload)
    print(f"[feishu] http_status={status} resp={body[:400]}")
    if status != 200:
        print("[feishu] 通知失败 (但不阻塞 workflow)", file=sys.stderr)
    return 0  # 无论成功失败都返回 0, 不让通知拖垮整个 workflow


if __name__ == "__main__":
    sys.exit(main())
