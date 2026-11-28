"""Automated real Windows Chrome full-window collector (UIA native shell + CDP web + paired hard negatives)."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.parse
import urllib.request
import zipfile

import ctypes
from ctypes import wintypes
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import win32gui
import win32process
import win32ui

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from m3_train_colab import SYSTEM_PROMPT, check_action
from roi_utils import ChromeWindowInfo, enable_per_monitor_dpi_awareness, inspect_chrome_window

DEFAULT_CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

PROFILES = [
    {
        "name": "win1280_s125_light",
        "window_size": (1280, 800),
        "scale": 1.25,
        "dark_mode": False,
        "show_bookmarks": False,
        "bg_tabs": [("tech_weekly", "技术周刊第 42 期"), ("milestones", "项目里程碑看板")],
    },
    {
        "name": "win1440_s100_dark_bm",
        "window_size": (1440, 900),
        "scale": 1.0,
        "dark_mode": True,
        "show_bookmarks": True,
        "bg_tabs": [
            ("todo", "今日待办清单"),
            ("notes", "架构设计笔记"),
            ("api_ref", "接口调试手册"),
        ],
    },
    {
        "name": "win1120_s150_light_bm",
        "window_size": (1120, 740),
        "scale": 1.5,
        "dark_mode": False,
        "show_bookmarks": True,
        "bg_tabs": [("travel", "城市交通时刻表")],
    },
]

PUBLIC_ZH_SITES = [
    {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/", "label": "Python 中文文档"},
    {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/tutorial/index.html", "label": "Python 教程"},
    {"domain": "developer.mozilla.org", "url": "https://developer.mozilla.org/zh-CN/", "label": "MDN Web 文档"},
    {"domain": "www.baidu.com", "url": "https://www.baidu.com/", "label": "百度一下"},
    {"domain": "cn.bing.com", "url": "https://cn.bing.com/", "label": "必应中文"},
    {"domain": "www.runoob.com", "url": "https://www.runoob.com/", "label": "菜鸟教程"},
    {"domain": "git-scm.com", "url": "https://git-scm.com/book/zh/v2", "label": "Pro Git 中文版"},
    {"domain": "cn.vuejs.org", "url": "https://cn.vuejs.org/", "label": "Vue.js 中文文档"},
    {"domain": "element-plus.org", "url": "https://element-plus.org/zh-CN/", "label": "Element Plus"},
]

ABSENT_TAB_POOL = [
    "季度财务审计报告",
    "Chrome 诊断报告",
    "生产环境告警控制台",
    "员工排班与考勤表",
    "年度OKR复盘纪要",
    "跨境物流运单追踪",
    "设备固件升级日志",
]

ABSENT_EXTENSION_POOL = [
    "项目录制器",
    "JSON 格式化助手",
    "网页取色器",
    "广告拦截插件",
    "划词翻译扩展",
    "密码管理助手",
]

ABSENT_WEB_CONTROL_POOL = [
    "导出为 PDF",
    "批量删除",
    "退出登录",
    "确认付款",
    "发布文章",
    "新建文件夹",
    "清空购物车",
    "联系人工客服",
    "查看历史订单",
    "提交工单",
    "下载桌面客户端",
    "恢复出厂设置",
    "撤销审批",
    "冻结账户",
]

CHINESE_QUERY_POOL = [
    "异步协程调度",
    "显卡性能测试",
    "自动化脚本",
    "前端路由配置",
    "神经网络微调",
    "表格导出设置",
    "机械键盘轴体",
    "接口鉴权说明",
]

UIA_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiHelper {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
"@
[void][DpiHelper]::SetProcessDpiAwarenessContext([IntPtr](-4))
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq 'QUIT') { break }
    if (-not $line) { continue }
    try {
        $parts = $line.Split(',')
        $hwndVal = [Int64]$parts[0]
        $maxY = [Double]$parts[1]
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwndVal)
        $cond = [System.Windows.Automation.Condition]::TrueCondition
        $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
        $items = New-Object System.Collections.Generic.List[object]
        for ($i = 0; $i -lt $all.Count; $i++) {
            $c = $all.Item($i).Current
            if ($c.IsOffscreen) { continue }
            $ct = $c.ControlType.ProgrammaticName
            if ($ct -notmatch 'TabItem|Button|Edit|MenuItem') { continue }
            $br = $c.BoundingRectangle
            if ($br.IsEmpty -or $br.Width -le 1 -or $br.Height -le 1) { continue }
            if ($br.Bottom -gt $maxY) { continue }
            $name = if ($null -ne $c.Name) { $c.Name.Trim() } else { '' }
            if (-not $name) { continue }
            $items.Add(@{
                type = $ct.Replace('ControlType.', '')
                name = $name
                left = [int][Math]::Round($br.Left)
                top = [int][Math]::Round($br.Top)
                right = [int][Math]::Round($br.Right)
                bottom = [int][Math]::Round($br.Bottom)
            })
        }
        $json = ConvertTo-Json -InputObject $items -Compress -Depth 4
        [Console]::Out.WriteLine($json)
    } catch {
        [Console]::Out.WriteLine(('{"error":' + (ConvertTo-Json $_.Exception.Message -Compress) + '}'))
    }
}
"""

JS_INSPECT_PAGE = r"""
(() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const dpr = window.devicePixelRatio || 1;
  const title = (document.title || '').trim();
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const bodyTrim = bodyText.trim();
  const topText = bodyTrim.slice(0, 800).toLowerCase();
  const titleLow = title.toLowerCase();

  // Read-only anti-bot / login wall / error detection (never bypass!)
  const blockSignals = [
    '请完成安全验证', '安全验证', '人机验证', '滑动验证', '点击完成验证',
    'just a moment', 'checking your browser', 'access denied', '403 forbidden',
    '404 not found', 'verify you are human'
  ];
  let blockedReason = null;
  for (const sig of blockSignals) {
    if (titleLow.includes(sig) || topText.includes(sig)) {
      blockedReason = 'signal:' + sig;
      break;
    }
  }
  if (!blockedReason) {
    const frames = document.querySelectorAll('iframe');
    for (const f of frames) {
      const src = (f.getAttribute('src') || '').toLowerCase();
      if (src.includes('captcha') || src.includes('recaptcha') || src.includes('turnstile') || src.includes('hcaptcha')) {
        const r = f.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < vh) {
          blockedReason = 'captcha_iframe';
          break;
        }
      }
    }
  }
  if (!blockedReason && bodyTrim.length < 30) {
    blockedReason = 'empty_body';
  }

  function isVisibleStyle(el) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') <= 0.05) return false;
    return true;
  }

  function cleanText(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  // Collect all visible viewport text to strictly verify not_found negatives
  const visChunks = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const val = cleanText(node.nodeValue);
      if (!val || !node.parentElement) continue;
      if (!isVisibleStyle(node.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width > 1 && r.height > 1 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
        visChunks.push(val);
      }
    }
  }
  // Also include input placeholders & button values visible on screen
  for (const inp of document.querySelectorAll('input, textarea, button')) {
    if (!isVisibleStyle(inp)) continue;
    const r = inp.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
      const extra = cleanText(inp.getAttribute('placeholder') || inp.value || inp.getAttribute('aria-label') || '');
      if (extra) visChunks.push(extra);
    }
  }
  const visibleTextJoined = visChunks.join('\n').toLowerCase();

  function getRowInfo(el) {
    const row = el.closest('tr, [data-row-label]');
    if (!row) return null;
    const explicit = row.getAttribute('data-row-index');
    const explicitLabel = row.getAttribute('data-row-label');
    if (explicit) return { row_index: parseInt(explicit, 10), row_label: cleanText(explicitLabel || '') };
    const tbody = row.parentElement;
    if (!tbody) return null;
    const rows = [...tbody.children].filter(c => c.tagName === 'TR');
    const idx = rows.indexOf(row);
    if (idx < 0) return null;
    const firstCell = row.querySelector('td');
    const hasHeader = row.querySelector('th') && !firstCell;
    if (hasHeader) return null;
    const dataRows = rows.filter(r => r.querySelector('td'));
    const dataIdx = dataRows.indexOf(row) + 1;
    if (dataIdx <= 0) return null;
    return { row_index: dataIdx, row_label: cleanText(firstCell ? firstCell.innerText : '') };
  }

  function isUnoccluded(el, rect) {
    const cx = (rect.left + rect.right) / 2;
    const cy = (rect.top + rect.bottom) / 2;
    const hit = document.elementFromPoint(cx, cy);
    return !!(hit && (hit === el || el.contains(hit)));
  }

  const clickables = [];
  const offscreenLabels = [];
  const seenOffscreen = new Set();

  const clickSel = 'button, a, input[type="submit"], input[type="button"], [role="button"], [role="tab"], [role="menuitem"]';
  for (const el of document.querySelectorAll(clickSel)) {
    if (!isVisibleStyle(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;

    let label = cleanText(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
    if (!label || label.length < 2 || label.length > 18) continue;
    if (/^[\d\s.,:%+\-×/]+$/.test(label)) continue;

    if (r.top > vh + 240 && r.top < vh + 3000) {
      if (label.length >= 2 && label.length <= 12 && !seenOffscreen.has(label) && !visibleTextJoined.includes(label.toLowerCase())) {
        seenOffscreen.add(label);
        offscreenLabels.push(label);
      }
      continue;
    }

    if (r.left < 8 || r.top < 8 || r.right > vw - 8 || r.bottom > vh - 8) continue;
    if (r.width * dpr < 14 || r.height * dpr < 14) continue;
    if (r.width > vw * 0.72 || r.height > vh * 0.35) continue;
    if (!isUnoccluded(el, r)) continue;

    const rowInfo = getRowInfo(el);
    clickables.push({
      label,
      rect: [r.left, r.top, r.right, r.bottom],
      tag: el.tagName.toLowerCase(),
      row_index: rowInfo ? rowInfo.row_index : null,
      row_label: rowInfo ? rowInfo.row_label : null,
    });
  }

  // Deduplicate plain labels unless row-disambiguated
  const labelCounts = new Map();
  for (const c of clickables) {
    if (c.row_index == null) {
      labelCounts.set(c.label, (labelCounts.get(c.label) || 0) + 1);
    }
  }
  const uniqueClickables = clickables.filter(c => {
    if (c.row_index != null) return true;
    return labelCounts.get(c.label) === 1;
  });

  // Text inputs
  const inputs = [];
  for (const el of document.querySelectorAll('input, textarea')) {
    if (!isVisibleStyle(el)) continue;
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (el.tagName.toLowerCase() === 'input' && !['text', 'search', 'url', 'email', ''].includes(t)) continue;
    if (el.disabled || el.readOnly) continue;
    const r = el.getBoundingClientRect();
    if (r.left < 8 || r.top < 8 || r.right > vw - 8 || r.bottom > vh - 8) continue;
    if (r.width * dpr < 80 || r.height * dpr < 22) continue;
    if (!isUnoccluded(el, r)) continue;

    let label = cleanText(el.getAttribute('aria-label') || el.getAttribute('placeholder') || '');
    if (!label && el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) label = cleanText(lbl.innerText);
    }
    if (!label) {
      const parentLbl = el.closest('label');
      if (parentLbl) label = cleanText(parentLbl.innerText);
    }
    if (!label) label = '搜索框';
    inputs.push({ label: label.slice(0, 24), rect: [r.left, r.top, r.right, r.bottom] });
  }

  // Scroll containers
  const scrolls = [];
  for (const el of document.querySelectorAll('[data-scroll-topic], div, section, main')) {
    if (el === document.body || el === document.documentElement) continue;
    if (!isVisibleStyle(el)) continue;
    const st = getComputedStyle(el);
    if (!['auto', 'scroll'].includes(st.overflowY)) continue;
    if (el.scrollHeight <= el.clientHeight + 80) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 200 || r.height < 140) continue;
    if (r.left < 0 || r.top < 0 || r.right > vw || r.bottom > vh) continue;
    const topic = cleanText(el.getAttribute('data-scroll-topic') || el.getAttribute('aria-label') || title.split(/[｜|\-]/)[0] || '列表区域');
    const dir = el.scrollTop > 30 ? 'up' : (el.scrollTop + el.clientHeight < el.scrollHeight - 20 ? 'down' : null);
    if (dir) {
      scrolls.push({ topic: topic.slice(0, 20), rect: [r.left, r.top, r.right, r.bottom], direction: dir });
    }
  }
  const se = document.scrollingElement || document.documentElement;
  if (se && se.scrollHeight > vh + 150) {
    const dir = (window.scrollY > 40) ? 'up' : 'down';
    scrolls.push({
      topic: cleanText(title.split(/[｜|\-]/)[0] || '当前页面').slice(0, 20),
      rect: [Math.round(vw * 0.15), Math.round(vh * 0.25), Math.round(vw * 0.85), Math.round(vh * 0.88)],
      direction: dir,
      is_page: true,
    });
  }

  return {
    url: location.href,
    title,
    dpr,
    viewport: [vw, vh],
    blockedReason,
    visibleTextJoined,
    clickables: uniqueClickables,
    inputs,
    scrolls,
    offscreenLabels: offscreenLabels.slice(0, 20),
  };
})()
"""


class CDPClient:
    """Minimal RFC 6455 loopback WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        parsed = urllib.parse.urlsplit(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake closed prematurely")
            buf += chunk
        header = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
        if "101" not in header.splitlines()[0]:
            raise ConnectionError(f"WebSocket upgrade failed: {header.splitlines()[0]}")
        self._extra = buf.split(b"\r\n\r\n", 1)[1]
        self._seq = 0

    def _recv_exact(self, n: int) -> bytes:
        chunks: list[bytes] = []
        remaining = n
        if self._extra:
            take = self._extra[:remaining]
            chunks.append(take)
            self._extra = self._extra[len(take) :]
            remaining -= len(take)
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ConnectionError("CDP socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_message(self) -> str:
        fragments: list[bytes] = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._recv_exact(8))
            mask_key = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("CDP peer sent close frame")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            fragments.append(data)
            if fin:
                return b"".join(fragments).decode("utf-8")

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        self._seq += 1
        req_id = self._seq
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}).encode("utf-8")
        deadline = time.monotonic() + timeout
        self.sock.settimeout(timeout)
        self._send_frame(0x1, payload)
        while True:
            remaining = max(0.05, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            raw = self._read_message()
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expression: str, timeout: float = 15.0) -> Any:
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class UIAHelper:
    """Persistent DPI-aware PowerShell UIAutomation worker."""

    _QUERY_TIMEOUT_S = 20.0

    def __init__(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(UIA_PS_SCRIPT + "\n")
        self.proc.stdin.flush()
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump_stdout, args=(self.proc, self._lines), daemon=True).start()

    @staticmethod
    def _pump_stdout(proc: subprocess.Popen, lines: "queue.Queue[str]") -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    def query(self, hwnd: int, max_screen_y: int) -> list[dict[str, Any]]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        try:
            self.proc.stdin.write(f"{hwnd},{max_screen_y}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            line = ""
        else:
            try:
                line = self._lines.get(timeout=self._QUERY_TIMEOUT_S)
            except queue.Empty:
                line = ""
        if not line:
            err = ""
            try:
                self.proc.kill()
            except OSError:
                pass
            if self.proc.stderr:
                try:
                    err = self.proc.stderr.read() or ""
                except Exception:
                    pass
            self._spawn()
            raise RuntimeError(f"UIA helper unresponsive (restarted worker): {err[:300]}")
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            if "error" in parsed:
                raise RuntimeError(f"UIA query failed: {parsed['error']}")
            return [parsed]
        return parsed

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def rich_local_pages() -> list[dict[str, str]]:
    """Return 4 high-diversity Chinese web apps visually distinct from the M3 60-page fixtures."""
    common_dark = """
      :root { --bg:#f4f6f9; --card:#ffffff; --ink:#192230; --muted:#5c6b7e; --line:#dfe5ee; --accent:#2358d8; --accent-soft:#eaf0ff; }
      @media (prefers-color-scheme: dark) {
        :root { --bg:#121720; --card:#1b2330; --ink:#e8eef7; --muted:#97a7bc; --line:#2d394b; --accent:#5a8eff; --accent-soft:#1e2f4f; }
      }
      * { box-sizing:border-box; }
      body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 'Microsoft YaHei UI','Microsoft YaHei',sans-serif; }
      button, input, textarea { font:inherit; color:inherit; }
      button { cursor:pointer; }
    """
    admin_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>工单与资源调度台｜后台管理</title>
<style>{common_dark}
  .shell {{ display:grid; grid-template-columns:210px 1fr; min-height:100vh; }}
  .nav {{ background:var(--card); border-right:1px solid var(--line); padding:18px 14px; display:flex; flex-direction:column; gap:8px; }}
  .nav h2 {{ margin:0 0 12px; font-size:16px; color:var(--accent); }}
  .nav a {{ padding:8px 10px; border-radius:6px; text-decoration:none; color:var(--ink); display:block; }}
  .nav a.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .main {{ padding:20px 24px; }}
  .toolbar {{ display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }}
  .toolbar input {{ flex:1; min-width:220px; height:36px; border:1px solid var(--line); border-radius:6px; padding:0 12px; background:var(--card); }}
  .btn {{ padding:7px 13px; border:1px solid var(--line); border-radius:6px; background:var(--card); }}
  .btn.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:11px 14px; text-align:left; border-bottom:1px solid var(--line); }}
  th {{ font-size:12px; color:var(--muted); }}
  .row-actions {{ display:flex; gap:6px; }}
  .mini {{ padding:4px 9px; font-size:12px; border:1px solid var(--line); border-radius:5px; background:var(--bg); }}
</style>
<body><div class="shell">
  <aside class="nav"><h2>云枢调度平台</h2>
    <a class="active" href="#orders">工单列表</a><a href="#nodes">算力节点</a><a href="#logs">操作日志</a><a href="#alerts">异常告警</a>
  </aside>
  <main class="main">
    <div class="toolbar">
      <input id="q" type="search" placeholder="输入工单号或负责人姓名搜索">
      <button class="btn" type="button">筛选状态</button>
      <button class="btn" type="button">导出报表</button>
      <button class="btn primary" type="button">新建工单</button>
    </div>
    <div class="card"><table>
      <thead><tr><th>工单编号</th><th>任务主题</th><th>负责人</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr data-row-index="1" data-row-label="ORD-202611-01"><td>ORD-202611-01</td><td>华东节点GPU扩容</td><td>陈思远</td><td>待处理</td><td class="row-actions"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>
        <tr data-row-index="2" data-row-label="ORD-202611-02"><td>ORD-202611-02</td><td>模型镜像同步校验</td><td>林知夏</td><td>进行中</td><td class="row-actions"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>
        <tr data-row-index="3" data-row-label="ORD-202611-03"><td>ORD-202611-03</td><td>夜间离线批测队列</td><td>周明宇</td><td>待审核</td><td class="row-actions"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>
        <tr data-row-index="4" data-row-label="ORD-202611-04"><td>ORD-202611-04</td><td>对象存储权限巡检</td><td>许安然</td><td>已完成</td><td class="row-actions"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>
        <tr data-row-index="5" data-row-label="ORD-202611-05"><td>ORD-202611-05</td><td>网关证书自动续期</td><td>顾清河</td><td>待处理</td><td class="row-actions"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>
      </tbody>
    </table></div>
  </main>
</div></body></html>"""

    shop_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>极物严选 · 数码好物馆</title>
<style>{common_dark}
  header {{ background:var(--card); border-bottom:1px solid var(--line); padding:12px 24px; display:flex; align-items:center; gap:14px; }}
  .brand {{ font-size:18px; font-weight:700; color:var(--accent); }}
  .search-wrap {{ flex:1; display:flex; gap:8px; max-width:540px; }}
  .search-wrap input {{ flex:1; height:36px; border:1px solid var(--line); border-radius:18px; padding:0 14px; background:var(--bg); }}
  .search-wrap button {{ height:36px; padding:0 18px; border-radius:18px; border:none; background:var(--accent); color:#fff; }}
  .container {{ max-width:1180px; margin:0 auto; padding:16px 24px; }}
  .notice {{ background:var(--accent-soft); border:1px solid var(--line); border-radius:8px; padding:10px 14px; display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }}
  .pills {{ display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }}
  .pill {{ padding:6px 14px; border-radius:16px; border:1px solid var(--line); background:var(--card); }}
  .pill.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .layout {{ display:grid; grid-template-columns:1fr 310px; gap:16px; }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
  .item {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
  .thumb {{ height:88px; border-radius:6px; background:linear-gradient(135deg, var(--accent-soft), var(--line)); margin-bottom:10px; }}
  .price {{ color:#e05246; font-weight:700; font-size:16px; margin:6px 0; }}
  .review-box {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; height:360px; overflow-y:auto; }}
  .rev {{ padding:10px 0; border-bottom:1px solid var(--line); }}
</style>
<body>
  <header>
    <div class="brand">极物严选</div>
    <div class="search-wrap"><input type="search" placeholder="搜索机械键盘、显示器或耳机"><button type="button">搜索商品</button></div>
    <button class="pill" type="button">我的购物车</button>
    <button class="pill" type="button">订单查询</button>
  </header>
  <div class="container">
    <div class="notice"><span>双十一返场优惠：数码外设满 500 减 50，限时生效中</span><button class="pill" type="button">我知道了</button></div>
    <div class="pills">
      <button class="pill active" type="button">全部商品</button><button class="pill" type="button">机械键盘</button>
      <button class="pill" type="button">高刷显示器</button><button class="pill" type="button">降噪耳机</button><button class="pill" type="button">人体工学椅</button>
    </div>
    <div class="layout">
      <div class="grid">
        <div class="item"><div class="thumb"></div><div><b>星辰87三模机械键盘</b></div><div class="price">¥399</div><button class="pill" type="button">加入收藏</button></div>
        <div class="item"><div class="thumb"></div><div><b>4K HDR 设计显示器</b></div><div class="price">¥1899</div><button class="pill" type="button">查看参数</button></div>
        <div class="item"><div class="thumb"></div><div><b>轻羽无线降噪耳机</b></div><div class="price">¥699</div><button class="pill" type="button">立即选购</button></div>
        <div class="item"><div class="thumb"></div><div><b>双臂显示器支架</b></div><div class="price">¥249</div><button class="pill" type="button">对比规格</button></div>
        <div class="item"><div class="thumb"></div><div><b>氮化镓140W快充头</b></div><div class="price">¥179</div><button class="pill" type="button">领券购买</button></div>
        <div class="item"><div class="thumb"></div><div><b>桌面拾音条形音箱</b></div><div class="price">¥329</div><button class="pill" type="button">了解详情</button></div>
      </div>
      <aside class="review-box" data-scroll-topic="商品评价列表">
        <h3 style="margin-top:0">精选买家评价</h3>
        {"".join(f'<div class="rev"><b>用户_{i:02d}</b>：手感扎实，无线连接稳定，桌面清爽很多。</div>' for i in range(1, 16))}
      </aside>
    </div>
  </div>
</body></html>"""

    kb_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>云文档协同空间｜开发规范</title>
<style>{common_dark}
  .topbar {{ height:48px; background:var(--card); border-bottom:1px solid var(--line); padding:0 18px; display:flex; align-items:center; justify-content:space-between; }}
  .actions {{ display:flex; gap:8px; }}
  .icon-btn {{ padding:4px 10px; font-size:12px; border:1px solid var(--line); border-radius:5px; background:var(--bg); }}
  .workspace {{ display:grid; grid-template-columns:220px 1fr 290px; height:calc(100vh - 48px); }}
  .tree {{ background:var(--card); border-right:1px solid var(--line); padding:14px; display:flex; flex-direction:column; gap:6px; }}
  .tree a {{ padding:7px 9px; border-radius:6px; text-decoration:none; color:var(--ink); }}
  .tree a.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .doc {{ padding:24px 32px; overflow-y:auto; }}
  .comments {{ background:var(--card); border-left:1px solid var(--line); padding:14px; display:flex; flex-direction:column; gap:10px; }}
  .comments textarea {{ width:100%; height:92px; border:1px solid var(--line); border-radius:6px; padding:8px; background:var(--bg); resize:none; }}
</style>
<body>
  <div class="topbar">
    <div><b>端侧视觉模型协同规范 v2.4</b></div>
    <div class="actions">
      <button class="icon-btn" type="button">收藏文档</button>
      <button class="icon-btn" type="button">历史版本</button>
      <button class="icon-btn" type="button">共享协作</button>
      <button class="icon-btn" type="button">页面设置</button>
    </div>
  </div>
  <div class="workspace">
    <nav class="tree">
      <a class="active" href="#dpi">DPI 与物理像素</a><a href="#coord">归一化坐标映射</a>
      <a href="#schema">闭集动作协议</a><a href="#safety">步前窗口校验</a><a href="#release">上线验收标准</a>
    </nav>
    <article class="doc" data-scroll-topic="文档正文">
      <h1>1. Windows DPI 与物理像素对齐规范</h1>
      {"".join(f"<p>第 {i} 节：所有窗口截图必须在 Per-Monitor DPI Aware v2 上下文中通过 DWMWA_EXTENDED_FRAME_BOUNDS 获取可见边框，并严格核对视口像素对齐。</p>" for i in range(1, 18))}
    </article>
    <aside class="comments">
      <h3 style="margin:0">评审意见 (3)</h3>
      <div style="font-size:13px;color:var(--muted)">张工：建议补充 150% 缩放边界测试。</div>
      <textarea placeholder="输入您的评审意见或修改建议"></textarea>
      <button class="icon-btn" style="background:var(--accent);color:#fff;border-color:var(--accent);padding:8px" type="button">发表评论</button>
    </aside>
  </div>
</body></html>"""

    feed_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>极客资讯 · 每日精选</title>
<style>{common_dark}
  .hdr {{ background:var(--card); border-bottom:1px solid var(--line); padding:12px 24px; display:flex; align-items:center; gap:16px; }}
  .tabs {{ display:flex; gap:10px; }}
  .tab {{ padding:6px 12px; border-radius:6px; border:none; background:transparent; }}
  .tab.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .search {{ margin-left:auto; display:flex; gap:8px; }}
  .search input {{ width:230px; height:34px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:var(--bg); }}
  .wrap {{ max-width:1160px; margin:16px auto; padding:0 24px; display:grid; grid-template-columns:1fr 300px; gap:18px; }}
  .feed-list {{ display:flex; flex-direction:column; gap:12px; height:520px; overflow-y:auto; padding-right:4px; }}
  .post {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .hot {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; height:fit-content; }}
  .hot-hdr {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }}
</style>
<body>
  <div class="hdr">
    <b>极客资讯</b>
    <div class="tabs">
      <button class="tab active" type="button">推荐阅读</button><button class="tab" type="button">人工智能</button>
      <button class="tab" type="button">前端工程</button><button class="tab" type="button">硬件评测</button><button class="tab" type="button">开源周报</button>
    </div>
    <div class="search"><input type="search" placeholder="搜索技术文章或专栏"><button class="tab" style="background:var(--accent);color:#fff" type="button">站内检索</button></div>
  </div>
  <div class="wrap">
    <section class="feed-list" data-scroll-topic="技术资讯信息流">
      {"".join(f'<article class="post"><h3 style="margin:0 0 6px">端侧多模态模型推理加速实践 #{i}</h3><p style="margin:0 0 8px;color:var(--muted)">对比 Q4_K_M 与 Q8_0 在 8GB 显存笔记本上的视觉 token 耗时与精度差异。</p><a href="#p{i}">阅读全文 #{i}</a></article>' for i in range(1, 12))}
    </section>
    <aside class="hot">
      <div class="hot-hdr"><b>今日热榜</b><button class="tab" style="border:1px solid var(--line)" type="button">换一批</button></div>
      <ol style="padding-left:18px;margin:0;display:grid;gap:8px">
        <li><a href="#h1">Windows UIA 自动化实践</a></li>
        <li><a href="#h2">浏览器视觉定位基准分析</a></li>
        <li><a href="#h3">轻量 VLM 本地量化部署</a></li>
        <li><a href="#h4">高 DPI 坐标对齐避坑指南</a></li>
      </ol>
    </aside>
  </div>
</body></html>"""

    return [
        {"slug": "admin_table", "domain": "local_admin_table", "title": "工单与资源调度台｜后台管理", "html": admin_html},
        {"slug": "ecommerce", "domain": "local_ecommerce", "title": "极物严选 · 数码好物馆", "html": shop_html},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "title": "云文档协同空间｜开发规范", "html": kb_html},
        {"slug": "news_feed", "domain": "local_news_feed", "title": "极客资讯 · 每日精选", "html": feed_html},
    ]


def start_local_fixture_server(pages: list[dict[str, str]], bg_tabs: list[tuple[str, str]]) -> tuple[ThreadingHTTPServer, str]:
    routes: dict[str, str] = {}
    for page in pages:
        routes[f"/{page['slug']}"] = page["html"]
    for slug, title in bg_tabs:
        routes[f"/bg/{slug}"] = (
            f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{title}</title>"
            f"<body style='font-family:sans-serif;padding:40px'><h2>{title}</h2><p>后台参考标签页。</p></body></html>"
        )

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path in routes:
                body = routes[path].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request: Any, client_address: Any) -> None:
            return

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


def prepare_chrome_profile(profile_dir: Path, show_bookmarks: bool) -> None:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs = {"bookmark_bar": {"show_on_all_tabs": show_bookmarks}}
    (default_dir / "Preferences").write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    if show_bookmarks:
        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "children": [
                        {"date_added": "13300000000000000", "guid": "11111111-1111-4111-8111-111111111111", "id": "1", "name": "Python 中文文档", "type": "url", "url": "https://docs.python.org/zh-cn/3/"},
                        {"date_added": "13300000000000000", "guid": "11111111-1111-4111-8111-111111111112", "id": "2", "name": "MDN 参考手册", "type": "url", "url": "https://developer.mozilla.org/zh-CN/"},
                        {"date_added": "13300000000000000", "guid": "11111111-1111-4111-8111-111111111113", "id": "3", "name": "菜鸟教程", "type": "url", "url": "https://www.runoob.com/"},
                    ],
                    "date_added": "13300000000000000",
                    "date_modified": "0",
                    "guid": "00000000-0000-4000-a000-000000000001",
                    "id": "0",
                    "name": "书签栏",
                    "type": "folder",
                },
                "other": {"children": [], "date_added": "13300000000000000", "date_modified": "0", "guid": "00000000-0000-4000-a000-000000000002", "id": "4", "name": "其他书签", "type": "folder"},
                "synced": {"children": [], "date_added": "13300000000000000", "date_modified": "0", "guid": "00000000-0000-4000-a000-000000000003", "id": "5", "name": "移动设备书签", "type": "folder"},
            },
            "version": 1,
        }
        (default_dir / "Bookmarks").write_text(json.dumps(bookmarks, ensure_ascii=False), encoding="utf-8")


def print_window_dwm(info: ChromeWindowInfo) -> Image.Image:
    l, t, r, b = info.window_rect.to_tuple()
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(info.hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    dst = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        dst.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(info.hwnd, dst.GetSafeHdc(), 2)
        if not ok:
            raise RuntimeError("PrintWindow returned 0")
        img = Image.frombytes("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        dst.DeleteDC()
        if bmp.GetHandle():
            win32gui.DeleteObject(bmp.GetHandle())
        win32gui.ReleaseDC(info.hwnd, hdc)
    dl, dt, dr, db = info.dwm_rect.to_tuple()
    return img.crop((dl - l, dt - t, dr - l, db - t))


def px_box_to_norm(box_px: list[int], width: int, height: int) -> list[int]:
    x1 = min(max(round(box_px[0] / max(1, width - 1) * 1000.0), 0), 1000)
    y1 = min(max(round(box_px[1] / max(1, height - 1) * 1000.0), 0), 1000)
    x2 = min(max(round(box_px[2] / max(1, width - 1) * 1000.0), 0), 1000)
    y2 = min(max(round(box_px[3] / max(1, height - 1) * 1000.0), 0), 1000)
    return [x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)]


def center_norm(bbox_norm: list[int]) -> list[int]:
    return [(bbox_norm[0] + bbox_norm[2]) // 2, (bbox_norm[1] + bbox_norm[3]) // 2]


def classify_uia_controls(
    raw_items: list[dict[str, Any]],
    info: ChromeWindowInfo,
    vp_top: int,
    allowed_bookmarks: set[str] | None = None,
) -> dict[str, Any]:
    dwm = info.dwm_rect
    tabs: list[dict[str, Any]] = []
    controls: dict[str, dict[str, Any]] = {}
    bookmarks: list[dict[str, Any]] = []
    addr_top = 0
    addr_bottom = 0

    # First pass to find address bar top/bottom (to separate top tab strip vs bookmark bar / translate bubble)
    for item in raw_items:
        if item["type"] == "Edit" and ("地址" in item["name"] or "address" in item["name"].lower()):
            rel_box = [
                item["left"] - dwm.left,
                item["top"] - dwm.top,
                item["right"] - dwm.left,
                item["bottom"] - dwm.top,
            ]
            addr_top = rel_box[1]
            addr_bottom = rel_box[3]
            controls["address_bar"] = {"label": "地址栏", "uia_name": item["name"], "box_px": rel_box}

    for item in raw_items:
        rel = [
            item["left"] - dwm.left,
            item["top"] - dwm.top,
            item["right"] - dwm.left,
            item["bottom"] - dwm.top,
        ]
        if rel[0] < 0 or rel[1] < 0 or rel[2] > dwm.width or rel[3] > vp_top + 8:
            continue
        if rel[2] - rel[0] < 8 or rel[3] - rel[1] < 8:
            continue

        t = item["type"]
        name = item["name"]
        low = name.lower()
        if t == "TabItem" and (addr_top == 0 or rel[3] <= addr_top + 12):
            clean_tab = name.split(" - 内存用量")[0].strip()
            tabs.append({"title": clean_tab, "box_px": rel})
        elif t == "Button":
            if name in {"返回", "Back"} and "back" not in controls:
                controls["back"] = {"label": "后退按钮", "uia_name": name, "box_px": rel}
            elif name in {"重新加载", "Reload"} and "reload" not in controls:
                controls["reload"] = {"label": "刷新按钮", "uia_name": name, "box_px": rel}
            elif name in {"新标签页", "New Tab"} and "new_tab" not in controls:
                controls["new_tab"] = {"label": "新建标签页加号按钮", "uia_name": name, "box_px": rel}
            elif name in {"标签页搜索", "Search tabs"} and "tab_search" not in controls:
                controls["tab_search"] = {"label": "标签页搜索按钮", "uia_name": name, "box_px": rel}
            elif ("添加书签" in name or "bookmark this tab" in low) and "bookmark_star" not in controls:
                controls["bookmark_star"] = {"label": "地址栏右侧星形书签按钮", "uia_name": name, "box_px": rel}
            elif name == "Chrome" and rel[0] > dwm.width * 0.7 and "chrome_menu" not in controls:
                controls["chrome_menu"] = {"label": "右上角三点菜单", "uia_name": name, "box_px": rel}
            elif (
                addr_bottom
                and rel[1] >= addr_bottom - 2
                and rel[3] <= vp_top + 4
                and len(name) >= 2
                and name not in {"标签页分组", "Tab groups", "所有书签", "All Bookmarks", "其他书签", "Other bookmarks", "英语", "中文（简体）", "关闭", "Close"}
                and rel[0] >= 45
                and (allowed_bookmarks is None or name in allowed_bookmarks)
            ):
                bookmarks.append({"title": name, "box_px": rel})

    return {"controls": controls, "tabs": tabs, "bookmarks": bookmarks}


def make_instruction(rng: random.Random, kind: str, **kw: Any) -> str:
    if kind == "native_click":
        target = kw["target"]
        return rng.choice(
            [
                f"点击 Chrome 顶部的「{target}」。",
                f"请帮我点一下浏览器{target}。",
                f"定位并左键点击{target}。",
            ]
        )
    if kind == "tab_click":
        title = kw["title"]
        return rng.choice(
            [
                f"切换到标题为「{title}」的标签页。",
                f"点击顶部标签栏里的「{title}」标签页。",
                f"点一下「{title}」这个浏览器标签页。",
            ]
        )
    if kind == "bookmark_click":
        title = kw["title"]
        return rng.choice(
            [
                f"点击书签栏里的「{title}」。",
                f"打开书签栏中的「{title}」书签。",
            ]
        )
    if kind == "web_click":
        label = kw["label"]
        if kw.get("row_index") is not None:
            r_idx = kw["row_index"]
            r_lbl = kw.get("row_label") or ""
            choices = [
                f"点击表格第 {r_idx} 行的「{label}」按钮。",
                f"请在表格第 {r_idx} 行点一下「{label}」。",
            ]
            if r_lbl:
                choices.append(f"点击「{r_lbl}」这一行的「{label}」按钮。")
            return rng.choice(choices)
        return rng.choice(
            [
                f"点击页面上的「{label}」。",
                f"请帮我点一下「{label}」。",
                f"定位并左键点击「{label}」控件。",
                f"在网页中选择「{label}」。",
            ]
        )
    if kind == "web_type":
        label = kw["label"]
        text = kw["text"]
        return rng.choice(
            [
                f"在「{label}」输入框里填入「{text}」，不要提交。",
                f"点击「{label}」并输入文字「{text}」。",
                f"帮我在「{label}」中输入「{text}」。",
            ]
        )
    if kind == "web_scroll":
        topic = kw["topic"]
        dir_zh = "向下" if kw["direction"] == "down" else "向上"
        amt = kw["amount"]
        return rng.choice(
            [
                f"在「{topic}」区域{dir_zh}滚动 {amt} 格。",
                f"把「{topic}」内容{dir_zh}滑动 {amt} 格。",
            ]
        )
    if kind == "not_found_tab":
        title = kw["title"]
        return rng.choice(
            [
                f"点击标题为「{title}」的标签页。",
                f"切换到顶部名为「{title}」的浏览器标签页。",
            ]
        )
    if kind == "not_found_ext":
        ext = kw["ext"]
        return rng.choice(
            [
                f"点击工具栏中的「{ext}」扩展按钮。",
                f"打开右上角扩展栏里的「{ext}」图标。",
            ]
        )
    if kind == "not_found_web":
        label = kw["label"]
        return rng.choice(
            [
                f"点击当前画面中的「{label}」按钮。",
                f"在当前页面找到并点一下「{label}」。",
                f"请帮我点击页面里的「{label}」。",
            ]
        )
    raise ValueError(kind)


def generate_frame_samples(
    frame_id: str,
    rel_image: str,
    image_sha256: str,
    domain: str,
    source_type: str,
    profile_name: str,
    img_size: tuple[int, int],
    uia_info: dict[str, Any],
    web_info: dict[str, Any],
    rng: random.Random,
) -> list[dict[str, Any]]:
    w, h = img_size
    samples: list[dict[str, Any]] = []
    idx = 0

    def add_sample(bucket: str, instruction: str, expected: dict[str, Any], bbox_px: list[int] | None, target_desc: str) -> None:
        nonlocal idx
        check_action(expected)
        idx += 1
        bbox_norm = px_box_to_norm(bbox_px, w, h) if bbox_px is not None else None
        if bbox_norm is not None and "coordinate" in expected:
            expected["coordinate"] = center_norm(bbox_norm)
        samples.append(
            {
                "sample_id": f"{frame_id}_{idx:02d}_{bucket}",
                "image": rel_image,
                "image_sha256": image_sha256,
                "domain": domain,
                "source_type": source_type,
                "profile": profile_name,
                "bucket": bucket,
                "target_desc": target_desc,
                "instruction": instruction,
                "expected": expected,
                "bbox": bbox_norm,
                "bbox_px": bbox_px,
            }
        )

    # 1. Native shell positives (2-3 items)
    native_candidates: list[tuple[str, str, list[int]]] = []
    for key in ("tab_search", "bookmark_star", "new_tab", "chrome_menu", "reload", "address_bar"):
        if key in uia_info["controls"]:
            c = uia_info["controls"][key]
            native_candidates.append(("native_click", c["label"], c["box_px"]))
    for tab in uia_info["tabs"]:
        short_title = tab["title"].split(" - Google Chrome")[0].strip()
        if len(short_title) > 26:
            short_title = short_title.split(" - ")[0].split("｜")[0].strip()[:26].strip()
        if len(short_title) >= 2:
            native_candidates.append(("tab_click", short_title, tab["box_px"]))
    for bm in uia_info["bookmarks"]:
        native_candidates.append(("bookmark_click", bm["title"][:20], bm["box_px"]))

    rng.shuffle(native_candidates)
    # Prefer at least one small native icon + one tab/address bar
    picked_native: list[tuple[str, str, list[int]]] = []
    small_keys = [x for x in native_candidates if x[0] == "native_click" and x[1] != "地址栏"]
    other_native = [x for x in native_candidates if x not in small_keys]
    if small_keys:
        picked_native.append(small_keys[0])
    if other_native:
        picked_native.append(other_native[0])
    for item in native_candidates:
        if len(picked_native) >= 3:
            break
        if item not in picked_native:
            picked_native.append(item)

    for kind, label, box_px in picked_native:
        inst = (
            make_instruction(rng, "tab_click", title=label)
            if kind == "tab_click"
            else make_instruction(rng, "bookmark_click", title=label)
            if kind == "bookmark_click"
            else make_instruction(rng, "native_click", target=label)
        )
        add_sample("native_shell_click", inst, {"action": "left_click", "coordinate": [0, 0]}, box_px, f"native:{label}")

    # 2. Native shell hard negatives (1-2 items)
    all_uia_text = " ".join(
        [t["title"] for t in uia_info["tabs"]]
        + [b["title"] for b in uia_info["bookmarks"]]
        + [c["uia_name"] for c in uia_info["controls"].values()]
    ).lower()
    vis_all_low = (all_uia_text + "\n" + web_info["visibleTextJoined"]).lower()

    absent_tabs = [t for t in ABSENT_TAB_POOL if t.lower() not in vis_all_low]
    if absent_tabs:
        chosen_tab = rng.choice(absent_tabs)
        add_sample(
            "native_not_found",
            make_instruction(rng, "not_found_tab", title=chosen_tab),
            {"action": "not_found", "reason": f"未在标签栏中找到「{chosen_tab}」标签页"},
            None,
            f"absent_tab:{chosen_tab}",
        )
    absent_exts = [e for e in ABSENT_EXTENSION_POOL if e.lower() not in vis_all_low]
    if absent_exts and rng.random() < 0.7:
        chosen_ext = rng.choice(absent_exts)
        add_sample(
            "native_not_found",
            make_instruction(rng, "not_found_ext", ext=chosen_ext),
            {"action": "not_found", "reason": f"工具栏中未出现「{chosen_ext}」扩展图标"},
            None,
            f"absent_ext:{chosen_ext}",
        )

    # 3. Web viewport positives (click, type, scroll)
    vp_left, vp_top = web_info["vp_origin"]
    dpr = float(web_info["dpr"])

    def css_to_px(rect: list[float]) -> list[int]:
        return [
            vp_left + round(rect[0] * dpr),
            vp_top + round(rect[1] * dpr),
            vp_left + round(rect[2] * dpr),
            vp_top + round(rect[3] * dpr),
        ]

    clicks = list(web_info["clickables"])
    rng.shuffle(clicks)
    row_clicks = [c for c in clicks if c.get("row_index") is not None]
    plain_clicks = [c for c in clicks if c.get("row_index") is None]
    chosen_clicks = (row_clicks[:2] + plain_clicks[:2])[:3]
    for c in chosen_clicks:
        bpx = css_to_px(c["rect"])
        inst = make_instruction(rng, "web_click", label=c["label"], row_index=c.get("row_index"), row_label=c.get("row_label"))
        add_sample("web_click", inst, {"action": "left_click", "coordinate": [0, 0]}, bpx, f"web:{c['label']}")

    if web_info["inputs"]:
        inp = rng.choice(web_info["inputs"])
        q = rng.choice(CHINESE_QUERY_POOL)
        bpx = css_to_px(inp["rect"])
        inst = make_instruction(rng, "web_type", label=inp["label"], text=q)
        add_sample("web_type", inst, {"action": "type", "coordinate": [0, 0], "text": q}, bpx, f"type:{inp['label']}")

    if web_info["scrolls"]:
        sc = rng.choice(web_info["scrolls"])
        amt = rng.choice([2, 3])
        bpx = css_to_px(sc["rect"])
        inst = make_instruction(rng, "web_scroll", topic=sc["topic"], direction=sc["direction"], amount=amt)
        add_sample(
            "web_scroll",
            inst,
            {"action": "scroll", "coordinate": [0, 0], "direction": sc["direction"], "amount": amt},
            bpx,
            f"scroll:{sc['topic']}",
        )

    # 4. Web same-screen hard negatives (offscreen real labels first, then semantic neighbors)
    neg_candidates: list[tuple[str, str]] = []
    for off in web_info["offscreenLabels"]:
        if off.lower() not in vis_all_low:
            neg_candidates.append(("offscreen", off))
    pool = list(ABSENT_WEB_CONTROL_POOL)
    rng.shuffle(pool)
    for term in pool:
        if term.lower() not in vis_all_low:
            neg_candidates.append(("semantic_absent", term))
    for neg_kind, term in neg_candidates[:2]:
        reason = "目标控件在当前可视区域外，需先滚动页面" if neg_kind == "offscreen" else f"当前页面未出现「{term}」控件"
        add_sample(
            "web_not_found",
            make_instruction(rng, "not_found_web", label=term),
            {"action": "not_found", "reason": reason},
            None,
            f"absent_web({neg_kind}):{term}",
        )

    return samples


def draw_qa_overlay(
    full_img: Image.Image,
    samples: list[dict[str, Any]],
    frame_meta: dict[str, Any],
    out_path: Path,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    vis = full_img.copy()
    draw = ImageDraw.Draw(vis)
    w, h = vis.size

    legend_lines = [
        f"{frame_meta['frame_id']} | {frame_meta['domain']} | {frame_meta['profile']} | img={w}x{h} | median_diff={frame_meta['median_abs_diff']:.2f}",
    ]
    for s in samples:
        bpx = s.get("bbox_px")
        if bpx is not None:
            is_native = s["bucket"].startswith("native_")
            color = (0, 230, 118) if is_native else (56, 152, 255)
            draw.rectangle([(bpx[0], bpx[1]), (bpx[2], bpx[3])], outline=color, width=3)
            cx = (bpx[0] + bpx[2]) // 2
            cy = (bpx[1] + bpx[3]) // 2
            draw.ellipse([(cx - 4, cy - 4), (cx + 4, cy + 4)], fill=color)
            tag = s["target_desc"][:20]
            draw.text((max(2, bpx[0]), max(2, bpx[1] - 18)), tag, fill=color, font=font)
        else:
            legend_lines.append(f"NEG [{s['bucket']}]: {s['instruction']}")

    banner_h = 22 * len(legend_lines) + 8
    canvas = Image.new("RGB", (w, h + banner_h), (20, 24, 30))
    canvas.paste(vis, (0, banner_h))
    cdraw = ImageDraw.Draw(canvas)
    for i, line in enumerate(legend_lines):
        cdraw.text((8, 4 + i * 22), line, fill=(240, 245, 255), font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")


def launch_chrome_session(
    chrome_exe: Path,
    profile_dir: Path,
    win_size: tuple[int, int],
    scale: float,
    dark_mode: bool,
    start_url: str,
) -> tuple[subprocess.Popen[bytes], int, int]:
    cmd = [
        str(chrome_exe),
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-CN",
        "--accept-lang=zh-CN",
        "--disable-gpu",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--window-position=20,20",
        f"--window-size={win_size[0]},{win_size[1]}",
        f"--force-device-scale-factor={scale}",
        start_url,
    ]
    if dark_mode:
        cmd.append("--force-dark-mode")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile_dir / "DevToolsActivePort"
    for _ in range(100):
        wins: list[int] = []

        def cb(h: int, _: Any) -> bool:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "Chrome_WidgetWin_1":
                if win32process.GetWindowThreadProcessId(h)[1] == proc.pid:
                    wins.append(h)
            return True

        win32gui.EnumWindows(cb, None)
        if wins and port_file.is_file() and port_file.stat().st_size > 0:
            port = int(port_file.read_text(encoding="ascii").splitlines()[0])
            return proc, wins[0], port
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("Timed out launching sandbox Chrome")


def wait_page_ready(cdp: CDPClient, target_url: str, timeout_s: float = 16.0) -> dict[str, Any]:
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.navigate", {"url": target_url}, timeout=timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = cdp.evaluate("({ready: document.readyState, len: document.body ? (document.body.innerText||'').trim().length : 0})")
            if isinstance(state, dict) and state.get("ready") == "complete" and state.get("len", 0) >= 20:
                time.sleep(0.9)
                return cdp.evaluate(JS_INSPECT_PAGE, timeout=12.0)
        except Exception:
            pass
        time.sleep(0.35)
    # One final attempt even if interactive
    time.sleep(0.8)
    return cdp.evaluate(JS_INSPECT_PAGE, timeout=12.0)


def collect_pilot(out_dir: Path, seed: int = 20261119, chrome_exe: Path = DEFAULT_CHROME) -> dict[str, Any]:
    enable_per_monitor_dpi_awareness()
    rng = random.Random(seed)
    local_pages = rich_local_pages()
    all_bg_tabs = [tab for prof in PROFILES for tab in prof["bg_tabs"]]
    http_server, base_url = start_local_fixture_server(local_pages, all_bg_tabs)
    uia = UIAHelper()
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 15) if Path(r"C:\Windows\Fonts\msyh.ttc").is_file() else ImageFont.load_default()

    frames_dir = out_dir / "frames"
    overlays_dir = out_dir / "qa_overlays"
    frames_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir.mkdir(parents=True, exist_ok=True)

    # Plan ~22 capture targets spread across the 3 profiles:
    # - 9 public sites (plus scrolled view of 3 long docs sites if available)
    # - 4 local pages x 3 profiles = 12 frames (with scrolled views on long pages)
    targets_by_profile: list[list[dict[str, Any]]] = [[], [], []]
    for i, site in enumerate(PUBLIC_ZH_SITES):
        targets_by_profile[i % 3].append({"source_type": "public_web", "domain": site["domain"], "url": site["url"], "scroll_after": i in (0, 2, 5)})
    for p_idx in range(3):
        for page in local_pages:
            targets_by_profile[p_idx].append(
                {
                    "source_type": "rich_local",
                    "domain": page["domain"],
                    "url": f"{base_url}/{page['slug']}",
                    "scroll_after": page["slug"] in ("kb_workspace", "news_feed") and p_idx == 1,
                }
            )

    all_records: list[dict[str, Any]] = []
    frame_logs: list[dict[str, Any]] = []
    skipped_logs: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    frame_seq = 0

    try:
        for prof_idx, prof in enumerate(PROFILES):
            with tempfile.TemporaryDirectory(prefix=f"chrome_prof_{prof_idx}_", ignore_cleanup_errors=True) as tmp:
                prof_dir = Path(tmp)
                prepare_chrome_profile(prof_dir, prof["show_bookmarks"])
                first_bg_url = f"{base_url}/bg/{prof['bg_tabs'][0][0]}"
                proc, hwnd, port = launch_chrome_session(
                    chrome_exe, prof_dir, prof["window_size"], prof["scale"], prof["dark_mode"], first_bg_url
                )
                try:
                    time.sleep(0.8)
                    # Find main page target and open remaining background tabs
                    tabs_json = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
                    main_target = next(t for t in tabs_json if t.get("type") == "page")
                    cdp = CDPClient(main_target["webSocketDebuggerUrl"])
                    try:
                        # Create a dedicated main tab AFTER background tabs so multiple tabs appear
                        for slug, _title in prof["bg_tabs"][1:]:
                            cdp.send("Target.createTarget", {"url": f"{base_url}/bg/{slug}"})
                        created = cdp.send("Target.createTarget", {"url": "about:blank"})
                        active_tid = created["targetId"]
                        cdp.close()
                        time.sleep(0.5)
                        tabs_json = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
                        active_target = next(t for t in tabs_json if t["id"] == active_tid)
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/activate/{active_tid}", timeout=5).read()
                        cdp = CDPClient(active_target["webSocketDebuggerUrl"])
                        cdp.send("Page.bringToFront")
                        time.sleep(0.4)

                        for task in targets_by_profile[prof_idx]:
                            for scroll_pass in ([False, True] if task.get("scroll_after") else [False]):
                                url = task["url"]
                                try:
                                    if not scroll_pass:
                                        web_info = wait_page_ready(cdp, url)
                                    else:
                                        cdp.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.75))")
                                        time.sleep(0.6)
                                        web_info = cdp.evaluate(JS_INSPECT_PAGE, timeout=12.0)

                                    if not isinstance(web_info, dict) or web_info.get("blockedReason"):
                                        skipped_logs.append(
                                            {"url": url, "profile": prof["name"], "reason": (web_info or {}).get("blockedReason", "inspect_failed")}
                                        )
                                        break

                                    info = inspect_chrome_window(hwnd)
                                    full_img = print_window_dwm(info)
                                    shot = cdp.send("Page.captureScreenshot", {"format": "png"}, timeout=15.0)
                                    vp_img = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
                                    vp_w, vp_h = vp_img.size
                                    w_dwm, h_dwm = full_img.size
                                    vp_left = info.client_rect.left - info.dwm_rect.left
                                    vp_top = (info.client_rect.bottom - info.dwm_rect.top) - vp_h

                                    if not (vp_left >= 0 and vp_top >= 40 and vp_left + vp_w <= w_dwm and vp_top + vp_h <= h_dwm):
                                        skipped_logs.append(
                                            {
                                                "url": url,
                                                "profile": prof["name"],
                                                "reason": f"bad_vp_bounds: left={vp_left}, top={vp_top}, vp=({vp_w},{vp_h}), dwm=({w_dwm},{h_dwm})",
                                            }
                                        )
                                        continue

                                    crop_vp = full_img.crop((vp_left, vp_top, vp_left + vp_w, vp_top + vp_h))
                                    diff = np.abs(np.asarray(crop_vp, dtype=np.int16) - np.asarray(vp_img, dtype=np.int16))
                                    med_diff = float(np.median(diff))
                                    mean_diff = float(np.mean(diff))
                                    if med_diff > 5.0 or mean_diff > 15.0:
                                        skipped_logs.append(
                                            {
                                                "url": url,
                                                "profile": prof["name"],
                                                "reason": f"alignment_failed: med={med_diff:.2f}, mean={mean_diff:.2f}",
                                            }
                                        )
                                        continue

                                    raw_uia = uia.query(hwnd, info.dwm_rect.top + vp_top + 8)
                                    uia_info = classify_uia_controls(raw_uia, info, vp_top)
                                    web_info["vp_origin"] = [vp_left, vp_top]

                                    frame_seq += 1
                                    frame_id = f"fw_{frame_seq:03d}"
                                    rel_img = f"frames/{frame_id}.png"
                                    img_path = out_dir / rel_img
                                    full_img.save(img_path, format="PNG", compress_level=4)
                                    img_sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
                                    if img_sha in seen_hashes:
                                        img_path.unlink()
                                        continue
                                    seen_hashes.add(img_sha)

                                    samples = generate_frame_samples(
                                        frame_id,
                                        rel_img,
                                        img_sha,
                                        task["domain"],
                                        task["source_type"],
                                        prof["name"],
                                        full_img.size,
                                        uia_info,
                                        web_info,
                                        rng,
                                    )
                                    all_records.extend(samples)
                                    fmeta = {
                                        "frame_id": frame_id,
                                        "url": url,
                                        "scroll_pass": scroll_pass,
                                        "domain": task["domain"],
                                        "source_type": task["source_type"],
                                        "profile": prof["name"],
                                        "scale": prof["scale"],
                                        "img_size": list(full_img.size),
                                        "vp_origin": [vp_left, vp_top],
                                        "vp_size": [vp_w, vp_h],
                                        "median_abs_diff": round(med_diff, 3),
                                        "mean_abs_diff": round(mean_diff, 3),
                                        "sample_count": len(samples),
                                    }
                                    frame_logs.append(fmeta)
                                    draw_qa_overlay(full_img, samples, fmeta, overlays_dir / f"{frame_id}.png", font)
                                    print(f"[CAPTURED] {frame_id} ({task['source_type']}:{task['domain']}) samples={len(samples)} diff={med_diff:.1f}/{mean_diff:.2f}")
                                except Exception as exc:
                                    skipped_logs.append({"url": url, "profile": prof["name"], "reason": f"exception:{type(exc).__name__}:{str(exc)[:160]}"})
                                    print(f"[SKIP] {url} -> {exc}")
                    finally:
                        cdp.close()
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
    finally:
        uia.close()
        http_server.shutdown()
        http_server.server_close()

    # Save records & package pilot_fullwindow_qa.zip
    with (out_dir / "records.jsonl").open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    zip_path = out_dir / "pilot_fullwindow_qa.zip"
    cap_meta = {
        "purpose": "PILOT_FULLWINDOW_QA",
        "count": len(all_records),
        "frame_count": len(frame_logs),
        "seed": seed,
        "system_prompt": SYSTEM_PROMPT,
    }
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("capture.json", json.dumps(cap_meta, ensure_ascii=False, indent=2))
        zf.writestr("holdout_chrome.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in all_records))
        added_imgs: set[str] = set()
        for r in all_records:
            rel = r["image"]
            if rel not in added_imgs:
                zf.write(out_dir / rel, rel)
                added_imgs.add(rel)

    summary = {
        "frame_count": len(frame_logs),
        "sample_count": len(all_records),
        "by_bucket": dict(Counter(r["bucket"] for r in all_records)),
        "by_source_type": dict(Counter(r["source_type"] for r in all_records)),
        "by_profile": dict(Counter(r["profile"] for r in all_records)),
        "frames": frame_logs,
        "skipped": skipped_logs,
    }
    (out_dir / "collection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("frames", "skipped")}, ensure_ascii=False, indent=2))
    return summary


def evaluate_pilot_baseline(zip_path: Path, out_dir: Path, models: list[str], port: int = 18190) -> dict[str, Any]:
    from m3_compare_holdout import run_model, score, write_json

    with zipfile.ZipFile(zip_path, "r") as zf:
        cases = [json.loads(line) for line in zf.read("holdout_chrome.jsonl").decode("utf-8").splitlines() if line.strip()]
        images: dict[str, str] = {}
        raw_cache: dict[str, str] = {}
        for case in cases:
            name = case["image"]
            if name not in raw_cache:
                raw = zf.read(name)
                if hashlib.sha256(raw).hexdigest() != case["image_sha256"]:
                    raise ValueError(f"Hash mismatch for {name}")
                raw_cache[name] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
            images[case["sample_id"]] = raw_cache[name]

    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for index, name in enumerate(models):
        predictions, latency = run_model(name, cases, images, port + index, out_dir)
        # Remap domain to bucket temporarily to reuse m3_compare_holdout.score by bucket
        bucket_cases = [{**c, "domain": c["bucket"]} for c in cases]
        by_bucket_raw = score(bucket_cases, predictions, by_domain=True)
        by_bucket = {}
        for k, counts in by_bucket_raw.items():
            bkt = k.split("/", 1)[0]
            by_bucket[bkt] = counts
        report[name] = {
            "by_action": score(cases, predictions),
            "by_bucket": by_bucket,
            "by_source_type": score([{**c, "domain": c["source_type"]} for c in cases], predictions, by_domain=True),
            "latency": latency,
        }
        write_json(out_dir / "report.json", report)
        print(json.dumps({name: report[name]}, ensure_ascii=False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Sandbox real-Chrome full-window collector & evaluator.")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "artifacts" / "m3_auto_collect_pilot_20261119")
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--eval-only", action="store_true", help="Skip collection and evaluate existing pilot_fullwindow_qa.zip.")
    parser.add_argument("--skip-eval", action="store_true", help="Only collect frames and overlays without running llama-server.")
    parser.add_argument("--models", nargs="+", default=["m2", "m3"])
    parser.add_argument("--port", type=int, default=18190)
    args = parser.parse_args()

    if not args.eval_only:
        collect_pilot(args.out, seed=args.seed, chrome_exe=args.chrome)
    if not args.skip_eval:
        evaluate_pilot_baseline(args.out / "pilot_fullwindow_qa.zip", args.out / "baseline_comparison", args.models, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
