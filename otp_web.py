# -*- coding: utf-8 -*-
"""
otp_web.py —— TOTP 动态口令 Web 面板（本机 localhost 用）

用法：
    python3 otp_web.py                 # 默认 http://127.0.0.1:5000
    python3 otp_web.py --port 8888
    python3 otp_web.py --host 0.0.0.0  # 局域网可访问（默认只监听 127.0.0.1）

数据源：读 ~/.pic-clear-otp/*.json（由 otp_admin.py 签发生成）

页面特性：
    - 黑色 + 毛玻璃卡片
    - 每台机器一张卡片，网格铺开
    - 6 位数字巨大 + 渐变高亮 + 环形 30 秒进度条
    - 点数字即复制，带 toast 提示
    - 每秒 fetch /api/codes 刷新（前端 SVG 环减少视觉抖动）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import otp_utils

VAULT_DIR = Path(os.path.expanduser("~")) / ".pic-clear-otp"


# ---------- 数据 ----------

def load_all_records() -> list[dict]:
    """扫描 vault 目录，返回所有机器记录（不含密钥，仅展示字段 + 当前码 + 剩余秒）。"""
    if not VAULT_DIR.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(VAULT_DIR.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        secret = rec.get("secret")
        if not secret:
            continue
        try:
            code = otp_utils.totp_at(secret)
        except Exception:
            code = "------"
        out.append({
            "fingerprint": rec.get("fingerprint", p.stem),
            "issued_to": rec.get("issued_to") or "",
            "issuer": rec.get("issuer") or "pic-clear",
            "created_at": rec.get("created_at") or "",
            "period": rec.get("period", otp_utils.DEFAULT_PERIOD),
            "digits": rec.get("digits", otp_utils.DEFAULT_DIGITS),
            "code": code,
            "seconds_left": otp_utils.seconds_to_next(
                period=rec.get("period", otp_utils.DEFAULT_PERIOD)),
        })
    return out


# ---------- HTML/CSS/JS ----------

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pic-clear · TOTP 面板</title>
<style>
:root{
  --bg:#07080b;
  --bg-grad-1:#0b0d13;
  --bg-grad-2:#151a26;
  --glass-bg:rgba(22,26,38,0.55);
  --glass-border:rgba(255,255,255,0.08);
  --text:#e7ebf5;
  --text-dim:#8a92a6;
  --hi:#7ce7ff;
  --hi-2:#a48bff;
  --warn:#ff6b6b;
  --shadow:0 20px 60px -20px rgba(0,0,0,0.65);
}
*{box-sizing:border-box}
html,body{
  margin:0;padding:0;min-height:100vh;
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  -webkit-font-smoothing:antialiased;
  overflow-x:hidden;
  background:var(--bg-grad-1);
}
/* 背景层：固定到视口，滚动时不重绘、不出现色带分层 */
body::after{
  content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:
    radial-gradient(1200px 800px at 10% -10%, #1a2140 0%, transparent 60%),
    radial-gradient(1000px 700px at 100% 100%, #2a1a4a 0%, transparent 55%),
    linear-gradient(180deg, var(--bg-grad-1), var(--bg-grad-2));
}
body::before{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(2px 2px at 20% 30%, rgba(255,255,255,0.06), transparent),
    radial-gradient(1px 1px at 80% 70%, rgba(124,231,255,0.10), transparent),
    radial-gradient(1px 1px at 40% 80%, rgba(164,139,255,0.08), transparent);
}
header{
  position:relative;z-index:1;
  padding:36px 40px 24px;
  display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:12px;
}
header h1{
  margin:0;font-size:22px;font-weight:600;letter-spacing:0.5px;
  background:linear-gradient(90deg,var(--hi),var(--hi-2));
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
header .sub{color:var(--text-dim);font-size:13px}
header .meta{color:var(--text-dim);font-size:12px}

main{
  position:relative;z-index:1;
  padding:8px 40px 60px;
  display:grid;
  grid-template-columns:repeat(auto-fill, minmax(360px, 1fr));
  gap:24px;
}

.card{
  position:relative;
  padding:28px 26px 24px;
  border-radius:20px;
  background:var(--glass-bg);
  backdrop-filter: blur(22px) saturate(140%);
  -webkit-backdrop-filter: blur(22px) saturate(140%);
  border:1px solid var(--glass-border);
  box-shadow:var(--shadow);
  overflow:hidden;
  transition:transform 0.15s ease, box-shadow 0.15s ease;
}
.card:hover{
  transform:translateY(-2px);
  box-shadow:0 30px 80px -20px rgba(0,0,0,0.75);
}
/* 删除按钮：默认极简小圆点（不刺眼），卡片 hover 时才浮现；
   两步确认：第一次点变红色"确认删除"胶囊，再点才真删 */
.card .del-btn{
  position:absolute; top:12px; right:12px;
  min-width:22px; height:22px; padding:0 8px;
  border-radius:11px;
  background:rgba(255,255,255,0.04);
  border:1px solid rgba(255,255,255,0.10);
  color:rgba(255,255,255,0.35);
  font-size:12px; line-height:20px; text-align:center;
  cursor:pointer; user-select:none; z-index:2;
  opacity:0; pointer-events:none;
  transition: opacity 0.2s ease, background 0.15s ease, color 0.15s ease,
              min-width 0.2s ease, padding 0.2s ease, border-color 0.15s ease;
}
.card:hover .del-btn{
  opacity:0.75;
  pointer-events:auto;
}
.card .del-btn:hover{
  opacity:1;
  color:#ffcccc;
  background:rgba(255,80,80,0.10);
  border-color:rgba(255,80,80,0.30);
}
.card .del-btn.armed{
  opacity:1;
  padding:0 12px;
  min-width:78px;
  color:#fff;
  background:rgba(220,50,50,0.85);
  border-color:rgba(255,120,120,0.6);
  box-shadow:0 4px 16px -4px rgba(220,50,50,0.6);
}

.card::before{
  content:"";position:absolute;inset:-1px;border-radius:20px;pointer-events:none;
  background:linear-gradient(135deg, rgba(124,231,255,0.30), rgba(164,139,255,0.10), transparent 60%);
  -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite:xor; mask-composite:exclude;
  padding:1px;opacity:0.7;
}

.card .row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.card .fp{
  font-family:'SF Mono','JetBrains Mono','Consolas',monospace;
  font-size:12px;color:var(--text-dim);letter-spacing:1px;
}
.card .who{
  font-size:14px;color:var(--text);opacity:0.85;font-weight:500;
}

.code-wrap{
  display:flex;align-items:center;justify-content:space-between;
  margin-top:18px;gap:14px;
}
.code{
  font-family:'SF Mono','JetBrains Mono','Consolas',monospace;
  font-size:56px;font-weight:700;letter-spacing:6px;
  background:linear-gradient(135deg,var(--hi) 0%, var(--hi-2) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  cursor:pointer; user-select:all;
  transition:filter 0.2s ease, transform 0.1s ease;
  text-shadow:0 0 40px rgba(124,231,255,0.15);
}
.code:hover{filter:brightness(1.15)}
.code:active{transform:scale(0.98)}
.code.warn{
  background:linear-gradient(135deg,#ff8a8a,#ff4d6d);
  -webkit-background-clip:text;background-clip:text;
}

.ring{position:relative;width:56px;height:56px;flex-shrink:0}
.ring svg{transform:rotate(-90deg)}
.ring .bg-c{stroke:rgba(255,255,255,0.06)}
.ring .fg-c{
  stroke:url(#g1);
  transition:stroke-dashoffset 0.9s linear;
}
.ring .fg-c.warn{stroke:var(--warn)}
.ring .sec{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-family:'SF Mono',monospace;font-size:14px;color:var(--text-dim);
  font-variant-numeric:tabular-nums;
}

.footer-row{
  margin-top:14px;display:flex;justify-content:space-between;
  color:var(--text-dim);font-size:11px;letter-spacing:0.5px;
}

.toast{
  position:fixed;left:50%;bottom:36px;transform:translateX(-50%) translateY(20px);
  padding:12px 22px;border-radius:12px;
  background:rgba(20,24,34,0.9);border:1px solid var(--glass-border);
  color:var(--text);font-size:13px;
  backdrop-filter:blur(12px);
  opacity:0;transition:opacity 0.2s, transform 0.2s;
  pointer-events:none;z-index:100;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.empty{
  grid-column:1/-1;text-align:center;padding:80px 20px;color:var(--text-dim);
}
.empty h2{color:var(--text);font-weight:500;margin:0 0 10px;font-size:20px}
.empty code{
  background:rgba(255,255,255,0.06);padding:2px 8px;border-radius:6px;
  font-family:'SF Mono',monospace;
}
</style>
</head>
<body>
<header>
  <div>
    <h1>pic-clear · TOTP 面板</h1>
    <div class="sub">共享密钥离线动态口令 · 每 30 秒刷新</div>
  </div>
  <div class="meta" id="meta">正在加载...</div>
</header>

<main id="grid">
  <div class="empty">加载中...</div>
</main>

<div class="toast" id="toast">已复制</div>

<svg width="0" height="0" style="position:absolute">
  <defs>
    <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#7ce7ff"/>
      <stop offset="100%" stop-color="#a48bff"/>
    </linearGradient>
  </defs>
</svg>

<script>
// 两步确认删除：第一次点 → 按钮变红胶囊"确认删除"，3 秒内再点才真删
let _armedTimer = null;
function _resetArmed(btn) {
  if (!btn) return;
  btn.classList.remove('armed');
  btn.dataset.armed = '0';
  btn.textContent = '···';
}
function armDelete(btn, fp) {
  // 已 armed → 真删
  if (btn.dataset.armed === '1') {
    doDelete(btn, fp);
    return;
  }
  // 首次点 → 进入 armed，3 秒无操作自动复位
  // 先把其它 armed 按钮复位掉
  document.querySelectorAll('.card .del-btn.armed').forEach(el => {
    if (el !== btn) _resetArmed(el);
  });
  btn.classList.add('armed');
  btn.dataset.armed = '1';
  btn.textContent = '确认删除';
  if (_armedTimer) clearTimeout(_armedTimer);
  _armedTimer = setTimeout(() => _resetArmed(btn), 3000);
}
async function doDelete(btn, fp) {
  if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }
  btn.textContent = '删除中…';
  try {
    const r = await fetch('/api/delete', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({fingerprint: fp})
    });
    const j = await r.json();
    if (!j.ok) {
      alert('删除失败：' + (j.msg || '未知错误'));
      _resetArmed(btn);
      return;
    }
    const card = document.querySelector(`.card[data-fp="${fp}"]`);
    if (card) card.remove();
  } catch(e) {
    alert('删除失败：' + e);
    _resetArmed(btn);
  }
}
// 点击卡片以外区域，取消所有 armed（避免误留）
document.addEventListener('click', (e) => {
  if (e.target.classList && e.target.classList.contains('del-btn')) return;
  document.querySelectorAll('.card .del-btn.armed').forEach(_resetArmed);
});

const RING_R = 24;
const RING_C = 2 * Math.PI * RING_R;

function cardHtml(rec) {
  const digits = rec.digits || 6;
  const period = rec.period || 30;
  const code = rec.code || "-".repeat(digits);
  const nice = code.replace(/(\d{3})(?=\d)/g, '$1 ');
  return `
    <div class="card" data-fp="${rec.fingerprint}" data-period="${period}">
      <div class="del-btn" data-armed="0" title="删除该机器授权" onclick="armDelete(this, '${rec.fingerprint}')">···</div>
      <div class="row">
        <div class="fp">${rec.fingerprint}</div>
        <div class="who">${rec.issued_to || '未署名'}</div>
      </div>
      <div class="code-wrap">
        <div class="code" data-code="${code}">${nice}</div>
        <div class="ring">
          <svg width="56" height="56">
            <circle class="bg-c" cx="28" cy="28" r="${RING_R}" fill="none" stroke-width="4"/>
            <circle class="fg-c" cx="28" cy="28" r="${RING_R}" fill="none" stroke-width="4"
                    stroke-dasharray="${RING_C}" stroke-dashoffset="0" stroke-linecap="round"/>
          </svg>
          <div class="sec">${rec.seconds_left}s</div>
        </div>
      </div>
      <div class="footer-row">
        <span>签发于 ${rec.created_at || '-'}</span>
        <span>${rec.issuer || 'pic-clear'}</span>
      </div>
    </div>
  `;
}

function renderAll(records) {
  const grid = document.getElementById('grid');
  if (!records || records.length === 0) {
    grid.innerHTML = `
      <div class="empty">
        <h2>还没有签发任何机器</h2>
        <div>用 <code>python3 otp_admin.py generate &lt;指纹&gt;</code> 签发第一台机器</div>
      </div>`;
    document.getElementById('meta').textContent = '0 台机器';
    return;
  }
  grid.innerHTML = records.map(cardHtml).join('');
  document.getElementById('meta').textContent = `${records.length} 台机器`;

  // 绑定点击复制
  grid.querySelectorAll('.code').forEach(el => {
    el.addEventListener('click', () => {
      const c = el.dataset.code;
      navigator.clipboard.writeText(c).then(() => showToast(`已复制 ${c}`));
    });
  });
}

function updateRings(records) {
  records.forEach(rec => {
    const card = document.querySelector(`.card[data-fp="${rec.fingerprint}"]`);
    if (!card) return;
    const codeEl = card.querySelector('.code');
    const secEl = card.querySelector('.sec');
    const fgC = card.querySelector('.fg-c');
    const period = parseInt(card.dataset.period, 10) || 30;

    // 数字变了就更新
    const oldCode = codeEl.dataset.code;
    if (oldCode !== rec.code) {
      codeEl.dataset.code = rec.code;
      codeEl.textContent = rec.code.replace(/(\d{3})(?=\d)/g, '$1 ');
    }
    secEl.textContent = `${rec.seconds_left}s`;

    // 环形进度
    const frac = rec.seconds_left / period;
    fgC.setAttribute('stroke-dashoffset', RING_C * (1 - frac));

    // <=5 秒警告色
    if (rec.seconds_left <= 5) {
      codeEl.classList.add('warn');
      fgC.classList.add('warn');
    } else {
      codeEl.classList.remove('warn');
      fgC.classList.remove('warn');
    }
  });
}

let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1600);
}

let lastCount = -1;

async function tick() {
  try {
    const r = await fetch('/api/codes', {cache:'no-store'});
    const data = await r.json();
    if (data.length !== lastCount) {
      renderAll(data);
      lastCount = data.length;
    } else {
      updateRings(data);
    }
  } catch (e) {
    console.error(e);
  }
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/codes":
            data = load_all_records()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "Not Found")

    def do_POST(self):  # noqa: N802
        if self.path == "/api/delete":
            self._handle_delete()
            return
        self.send_error(404, "Not Found")

    def _handle_delete(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            fp = str(data.get("fingerprint", "")).strip()
        except Exception as e:
            return self._json(400, {"ok": False, "msg": f"参数解析失败: {e}"})

        # 只允许 [A-Za-z0-9-]，防路径穿越
        import re as _re
        if not fp or not _re.fullmatch(r"[A-Za-z0-9-]{1,64}", fp):
            return self._json(400, {"ok": False, "msg": "非法指纹"})

        target = VAULT_DIR / f"{fp}.json"
        try:
            target = target.resolve()
            vault = VAULT_DIR.resolve()
            if not str(target).startswith(str(vault) + os.sep):
                return self._json(400, {"ok": False, "msg": "路径越界"})
        except Exception as e:
            return self._json(500, {"ok": False, "msg": f"路径解析失败: {e}"})

        if not target.is_file():
            return self._json(404, {"ok": False, "msg": "记录不存在"})
        try:
            target.unlink()
        except Exception as e:
            return self._json(500, {"ok": False, "msg": f"删除失败: {e}"})
        return self._json(200, {"ok": True})

    def _json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静音默认日志，避免刷屏
        return


def main() -> int:
    ap = argparse.ArgumentParser(description="pic-clear TOTP Web 面板")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认 127.0.0.1（本机）；改 0.0.0.0 让局域网可访问")
    ap.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  pic-clear TOTP 面板启动 → http://{args.host}:{args.port}")
    print(f"  数据源：{VAULT_DIR}")
    print("  Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
