"""Collect, verify, package, and baseline-evaluate the M3.5 leak-free Chrome full-window dataset."""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any
import urllib.parse
import urllib.request
import zipfile

from PIL import Image, ImageFont
import numpy as np
import win32gui

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from m3_auto_collect_chrome import (
    ABSENT_EXTENSION_POOL,
    ABSENT_TAB_POOL,
    ABSENT_WEB_CONTROL_POOL,
    CDPClient,
    CHINESE_QUERY_POOL,
    DEFAULT_CHROME,
    UIAHelper,
    center_norm,
    classify_uia_controls,
    draw_qa_overlay,
    launch_chrome_session,
    prepare_chrome_profile,
    print_window_dwm,
    px_box_to_norm,
)
from m3_prepare_option_a import resize_for_qwen35
from m3_train_colab import SYSTEM_PROMPT, check_action, load_sft
from roi_utils import enable_per_monitor_dpi_awareness, inspect_chrome_window

PROFILES_V2 = [
    {
        "name": "p1_1280_s125_light",
        "window_size": (1280, 800),
        "scale": 1.25,
        "dark_mode": False,
        "show_bookmarks": False,
        "bg_tabs": [("tech_weekly", "技术周刊第 42 期"), ("milestones", "项目里程碑看板")],
    },
    {
        "name": "p2_1440_s100_dark_bm",
        "window_size": (1440, 900),
        "scale": 1.0,
        "dark_mode": True,
        "show_bookmarks": True,
        "bg_tabs": [("todo", "今日待办清单"), ("notes", "架构设计笔记"), ("api_ref", "接口调试手册")],
    },
    {
        "name": "p3_1120_s150_light_bm",
        "window_size": (1120, 740),
        "scale": 1.5,
        "dark_mode": False,
        "show_bookmarks": True,
        "bg_tabs": [("travel", "城市交通时刻表")],
    },
    {
        "name": "p4_1366_s100_light_5tabs",
        "window_size": (1366, 768),
        "scale": 1.0,
        "dark_mode": False,
        "show_bookmarks": False,
        "bg_tabs": [
            ("minutes", "会议纪要汇总"),
            ("roadmap", "需求排期表"),
            ("monitor", "监控大盘"),
            ("stars", "开源收藏夹"),
        ],
    },
]

SPLIT_PUBLIC_TASKS: dict[str, list[dict[str, Any]]] = {
    "train": [
        {"domain": "www.bilibili.com", "url": "https://www.bilibili.com/", "profile_idx": 0, "scroll": True},
        {"domain": "www.youtube.com", "url": "https://www.youtube.com/", "profile_idx": 1, "scroll": True},
        {"domain": "github.com", "url": "https://github.com/explore", "profile_idx": 2, "scroll": False},
        {"domain": "github.com", "url": "https://github.com/trending", "profile_idx": 0, "scroll": False},
        {"domain": "github.com", "url": "https://github.com/QwenLM/Qwen3", "profile_idx": 1, "scroll": False},
        {"domain": "github.com", "url": "https://github.com/QwenLM/Qwen3/issues", "profile_idx": 3, "scroll": False},
        {"domain": "www.deepl.com", "url": "https://www.deepl.com/zh/translator", "profile_idx": 0, "scroll": False},
        {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/", "profile_idx": 0, "scroll": True},
        {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/tutorial/index.html", "profile_idx": 1, "scroll": False},
        {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/library/index.html", "profile_idx": 3, "scroll": False},
        {"domain": "docs.python.org", "url": "https://docs.python.org/zh-cn/3/reference/index.html", "profile_idx": 2, "scroll": False},
        {"domain": "developer.mozilla.org", "url": "https://developer.mozilla.org/zh-CN/", "profile_idx": 2, "scroll": True},
        {"domain": "developer.mozilla.org", "url": "https://developer.mozilla.org/zh-CN/docs/Web/JavaScript", "profile_idx": 1, "scroll": False},
        {"domain": "developer.mozilla.org", "url": "https://developer.mozilla.org/zh-CN/docs/Web/HTML", "profile_idx": 3, "scroll": False},
        {"domain": "www.baidu.com", "url": "https://www.baidu.com/", "profile_idx": 0, "scroll": False},
        {"domain": "www.runoob.com", "url": "https://www.runoob.com/", "profile_idx": 2, "scroll": True},
        {"domain": "www.runoob.com", "url": "https://www.runoob.com/python3/python3-tutorial.html", "profile_idx": 3, "scroll": False},
        {"domain": "juejin.cn", "url": "https://juejin.cn/", "profile_idx": 0, "scroll": False},
        {"domain": "www.cnblogs.com", "url": "https://www.cnblogs.com/", "profile_idx": 3, "scroll": False},
        {"domain": "huggingface.co", "url": "https://huggingface.co/models", "profile_idx": 1, "scroll": False},
        {"domain": "cn.vuejs.org", "url": "https://cn.vuejs.org/", "profile_idx": 1, "scroll": False},
        {"domain": "cn.vuejs.org", "url": "https://cn.vuejs.org/guide/introduction.html", "profile_idx": 2, "scroll": False},
        {"domain": "learn.microsoft.com", "url": "https://learn.microsoft.com/zh-cn/windows/", "profile_idx": 3, "scroll": False},
        {"domain": "www.tslang.cn", "url": "https://www.tslang.cn/", "profile_idx": 0, "scroll": False},
    ],
    "dev": [
        {"domain": "cn.bing.com", "url": "https://cn.bing.com/", "profile_idx": 1, "scroll": False},
        {"domain": "www.v2ex.com", "url": "https://www.v2ex.com/", "profile_idx": 0, "scroll": True},
        {"domain": "www.v2ex.com", "url": "https://www.v2ex.com/?tab=tech", "profile_idx": 3, "scroll": False},
        {"domain": "element-plus.org", "url": "https://element-plus.org/zh-CN/", "profile_idx": 2, "scroll": True},
        {"domain": "element-plus.org", "url": "https://element-plus.org/zh-CN/component/button.html", "profile_idx": 0, "scroll": False},
        {"domain": "rustwiki.org", "url": "https://rustwiki.org/zh-CN/", "profile_idx": 3, "scroll": True},
        {"domain": "rustwiki.org", "url": "https://rustwiki.org/zh-CN/book/", "profile_idx": 1, "scroll": False},
    ],
    "holdout": [
        {"domain": "zh.wikipedia.org", "url": "https://zh.wikipedia.org/wiki/Wikipedia:%E9%A6%96%E9%A1%B5", "profile_idx": 0, "scroll": True},
        {"domain": "zh.wikipedia.org", "url": "https://zh.wikipedia.org/wiki/Python", "profile_idx": 3, "scroll": False},
        {"domain": "git-scm.com", "url": "https://git-scm.com/book/zh/v2", "profile_idx": 1, "scroll": True},
        {"domain": "www.ruanyifeng.com", "url": "https://www.ruanyifeng.com/blog/", "profile_idx": 3, "scroll": True},
        {"domain": "www.liaoxuefeng.com", "url": "https://www.liaoxuefeng.com/", "profile_idx": 2, "scroll": True},
        {"domain": "react.dev", "url": "https://react.dev/", "profile_idx": 1, "scroll": True},
        {"domain": "react.dev", "url": "https://react.dev/learn", "profile_idx": 0, "scroll": False},
        {"domain": "vite.dev", "url": "https://vite.dev/", "profile_idx": 0, "scroll": True},
        {"domain": "www.reddit.com", "url": "https://www.reddit.com/", "profile_idx": 2, "scroll": False},
    ],
}

SPLIT_LOCAL_PLANS: dict[str, list[dict[str, Any]]] = {
    "train": [
        {"slug": "newtab_hub", "domain": "local_newtab_hub", "profile_idx": 0, "scroll": False},
        {"slug": "newtab_hub", "domain": "local_newtab_hub", "profile_idx": 1, "scroll": False},
        {"slug": "newtab_hub", "domain": "local_newtab_hub", "profile_idx": 2, "scroll": False},
        {"slug": "newtab_hub", "domain": "local_newtab_hub", "profile_idx": 3, "scroll": False},
        {"slug": "ai_chat", "domain": "local_ai_chat", "profile_idx": 0, "scroll": False},
        {"slug": "ai_chat", "domain": "local_ai_chat", "profile_idx": 1, "scroll": False},
        {"slug": "ai_chat", "domain": "local_ai_chat", "profile_idx": 2, "scroll": False},
        {"slug": "ai_chat_modal", "domain": "local_ai_chat", "profile_idx": 3, "scroll": False},
        {"slug": "translator", "domain": "local_translator", "profile_idx": 0, "scroll": False},
        {"slug": "translator", "domain": "local_translator", "profile_idx": 1, "scroll": False},
        {"slug": "translator", "domain": "local_translator", "profile_idx": 2, "scroll": False},
        {"slug": "translator", "domain": "local_translator", "profile_idx": 3, "scroll": False},
        {"slug": "admin_table", "domain": "local_admin_table", "profile_idx": 0, "scroll": False},
        {"slug": "admin_table", "domain": "local_admin_table", "profile_idx": 1, "scroll": False},
        {"slug": "admin_table", "domain": "local_admin_table", "profile_idx": 2, "scroll": False},
        {"slug": "admin_table_modal", "domain": "local_admin_table", "profile_idx": 3, "scroll": False},
        {"slug": "ecommerce", "domain": "local_ecommerce", "profile_idx": 0, "scroll": False},
        {"slug": "ecommerce", "domain": "local_ecommerce", "profile_idx": 1, "scroll": False},
        {"slug": "ecommerce", "domain": "local_ecommerce", "profile_idx": 2, "scroll": False},
        {"slug": "ecommerce", "domain": "local_ecommerce", "profile_idx": 3, "scroll": False},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "profile_idx": 0, "scroll": False},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "profile_idx": 1, "scroll": True},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "profile_idx": 2, "scroll": False},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "profile_idx": 3, "scroll": False},
    ],
    "dev": [
        {"slug": "video_feed", "domain": "local_video_feed", "profile_idx": 0, "scroll": False},
        {"slug": "video_feed", "domain": "local_video_feed", "profile_idx": 1, "scroll": True},
        {"slug": "video_feed", "domain": "local_video_feed", "profile_idx": 2, "scroll": False},
        {"slug": "video_feed", "domain": "local_video_feed", "profile_idx": 3, "scroll": True},
    ],
    "holdout": [
        {"slug": "forum_thread", "domain": "local_forum_thread", "profile_idx": 0, "scroll": False},
        {"slug": "forum_thread", "domain": "local_forum_thread", "profile_idx": 1, "scroll": True},
        {"slug": "forum_thread", "domain": "local_forum_thread", "profile_idx": 2, "scroll": False},
        {"slug": "forum_thread_modal", "domain": "local_forum_thread", "profile_idx": 3, "scroll": False},
    ],
}

NEAR_MATCH_MAP = {
    "设置": ["高级设置", "偏好设置"],
    "保存": ["批量保存", "另存为副本"],
    "登录": ["退出登录", "切换账号"],
    "索引": ["主索引", "全局索引"],
    "文档": ["导出文档", "新建文档"],
    "推荐": ["历史推荐", "关闭推荐"],
    "搜索": ["高级搜索", "全站清空"],
    "查看": ["查看全部日志", "导出明细"],
    "分享": ["分享到微信", "生成短链"],
    "收藏": ["取消收藏", "清空收藏夹"],
    "下载": ["下载桌面客户端", "离线安装包"],
    "评论": ["清空评论", "导出评论"],
    "编辑": ["批量编辑", "撤销编辑"],
    "新建工单": ["批量删除工单", "清空工单"],
    "发送消息": ["撤回消息", "清空对话"],
    "复制译文": ["导出双语PDF", "清空译文"],
    "立即结算": ["清空购物车", "申请退款"],
    "发表评论": ["删除全部评论", "屏蔽作者"],
    "立即订阅": ["取消全部订阅", "管理分组"],
    "提交回复": ["删除主题帖", "举报发帖人"],
    "AI 模式": ["离线模式", "无痕模式"],
}

COLOR_OPPOSITES = {
    "蓝色": ["红色", "绿色", "橙色"],
    "绿色": ["红色", "蓝色"],
    "红色": ["蓝色", "绿色"],
    "橙色": ["蓝色", "绿色"],
}

JS_INSPECT_PAGE_V2 = r"""
(() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const dpr = window.devicePixelRatio || 1;
  const title = (document.title || '').trim();
  const host = (location.hostname || '').toLowerCase();
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const bodyTrim = bodyText.trim();
  const topText = bodyTrim.slice(0, 800).toLowerCase();
  const titleLow = title.toLowerCase();

  const blockSignals = [
    '请完成安全验证', '安全验证', '人机验证', '滑动验证', '点击完成验证',
    'just a moment', 'checking your browser', 'access denied', '403 forbidden',
    '404 not found', 'verify you are human', 'blocked by network security',
    'log in to continue', 'sign in to continue'
  ];
  let blockedReason = null;
  for (const sig of blockSignals) {
    if (titleLow.includes(sig) || topText.includes(sig)) {
      blockedReason = 'signal:' + sig;
      break;
    }
  }
  if (!blockedReason) {
    for (const f of document.querySelectorAll('iframe')) {
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
    return !(st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') <= 0.05);
  }

  function cleanText(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  const visChunks = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const val = cleanText(node.nodeValue);
      if (!val || !node.parentElement || !isVisibleStyle(node.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width > 1 && r.height > 1 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
        visChunks.push(val);
      }
    }
  }
  for (const inp of document.querySelectorAll('input, textarea, button, select')) {
    if (!isVisibleStyle(inp)) continue;
    const r = inp.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
      const extra = cleanText(inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || inp.value || '');
      if (extra) visChunks.push(extra);
    }
  }
  const visibleTextJoined = visChunks.join('\n').toLowerCase();

  // Pre-scan true data tables: >= 2 data rows, >= 3 td columns per row, and repeated button labels across >= 2 rows
  const trueDataTables = new Set();
  for (const tbl of document.querySelectorAll('table')) {
    const rows = [...tbl.querySelectorAll('tr')].filter(r => r.querySelectorAll('td').length >= 3);
    if (rows.length < 2) continue;
    const rowLabelSets = rows.map(r => {
      const s = new Set();
      for (const b of r.querySelectorAll('button, a, [role="button"]')) {
        const lbl = cleanText(b.innerText || b.getAttribute('aria-label') || '');
        if (lbl && lbl.length <= 12) s.add(lbl);
      }
      return s;
    });
    const labelRows = new Map();
    for (const s of rowLabelSets) {
      for (const lbl of s) labelRows.set(lbl, (labelRows.get(lbl) || 0) + 1);
    }
    if ([...labelRows.values()].some(c => c >= 2)) {
      trueDataTables.add(tbl);
    }
  }

  function getRowInfo(el, label) {
    const row = el.closest('tr');
    if (!row) return null;
    const tbl = row.closest('table');
    if (!tbl || !trueDataTables.has(tbl)) return null;
    const rows = [...tbl.querySelectorAll('tr')].filter(r => r.querySelectorAll('td').length >= 3);
    const dataIdx = rows.indexOf(row) + 1;
    if (dataIdx <= 0) return null;
    // Check that this label appears in at least 2 different rows of this table
    let hits = 0;
    for (const r of rows) {
      for (const cand of r.querySelectorAll('button, a, [role="button"], input[type="checkbox"]')) {
        const cl = cleanText(cand.innerText || cand.getAttribute('data-check-label') || cand.getAttribute('aria-label') || '');
        if (cl === label) { hits++; break; }
      }
    }
    if (hits < 2 && el.type !== 'checkbox') return null;
    const explicitLabel = row.getAttribute('data-row-label');
    const firstCell = row.querySelector('td');
    const rowLbl = cleanText(explicitLabel || (firstCell ? firstCell.innerText : ''));
    return { row_index: dataIdx, row_label: rowLbl };
  }

  function isUnoccluded(el, rect) {
    const cx = (rect.left + rect.right) / 2;
    const cy = (rect.top + rect.bottom) / 2;
    const hit = document.elementFromPoint(cx, cy);
    return !!(hit && (hit === el || el.contains(hit) || (el.tagName === 'INPUT' && el.id && hit.closest(`label[for="${CSS.escape(el.id)}"]`))));
  }

  const clickables = [];
  const offscreenLabels = [];
  const seenOffscreen = new Set();

  const clickSel = 'button, a, input[type="submit"], input[type="button"], [role="button"], [role="tab"], [role="menuitem"], select, [role="combobox"]';
  for (const el of document.querySelectorAll(clickSel)) {
    if (!isVisibleStyle(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;

    let label = '';
    const isDropdown = el.tagName.toLowerCase() === 'select' || el.getAttribute('role') === 'combobox';
    if (el.tagName.toLowerCase() === 'select') {
      const opt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      label = cleanText(el.getAttribute('data-label') || el.getAttribute('aria-label') || (opt ? opt.text : ''));
    } else {
      label = cleanText(el.getAttribute('data-label') || el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
    }
    if (!label || label.length < 2 || label.length > 20) continue;
    if (/^[\d\s.,:%+\-×/]+$/.test(label)) continue;

    if (r.top > vh + 240 && r.top < vh + 3200) {
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

    const rowInfo = getRowInfo(el, label);
    clickables.push({
      label,
      rect: [r.left, r.top, r.right, r.bottom],
      tag: el.tagName.toLowerCase(),
      is_checkbox: false,
      is_dropdown: isDropdown,
      color_zh: el.getAttribute('data-color-zh') || null,
      row_index: rowInfo ? rowInfo.row_index : null,
      row_label: rowInfo ? rowInfo.row_label : null,
    });
  }

  // Checkboxes
  for (const el of document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
    if (!isVisibleStyle(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.left < 8 || r.top < 8 || r.right > vw - 8 || r.bottom > vh - 8) continue;
    if (r.width * dpr < 12 || r.height * dpr < 12) continue;
    if (!isUnoccluded(el, r)) continue;
    let label = cleanText(el.getAttribute('data-check-label') || el.getAttribute('aria-label') || '');
    if (!label && el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) label = cleanText(lbl.innerText);
    }
    if (!label) {
      const pl = el.closest('label');
      if (pl) label = cleanText(pl.innerText);
    }
    if (!label || label.length > 22) continue;
    const rowInfo = getRowInfo(el, label);
    clickables.push({
      label,
      rect: [r.left, r.top, r.right, r.bottom],
      tag: 'checkbox',
      is_checkbox: true,
      is_dropdown: false,
      color_zh: null,
      row_index: rowInfo ? rowInfo.row_index : null,
      row_label: rowInfo ? rowInfo.row_label : null,
    });
  }

  const labelCounts = new Map();
  for (const c of clickables) {
    if (c.row_index == null) {
      labelCounts.set(c.label, (labelCounts.get(c.label) || 0) + 1);
    }
  }
  const uniqueClickables = clickables.filter(c => c.row_index != null || labelCounts.get(c.label) === 1);

  // Text inputs with label sanitization
  const inputs = [];
  const inputKeywords = /搜索|输入|查询|查找|问题|评论|翻译|地址|工单|商品|关键词|问问/;
  for (const el of document.querySelectorAll('input, textarea')) {
    if (!isVisibleStyle(el)) continue;
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (el.tagName.toLowerCase() === 'input' && !['text', 'search', 'url', 'email', ''].includes(t)) continue;
    if (el.disabled || el.readOnly) continue;
    const r = el.getBoundingClientRect();
    if (r.left < 8 || r.top < 8 || r.right > vw - 8 || r.bottom > vh - 8) continue;
    if (r.width * dpr < 80 || r.height * dpr < 22) continue;
    if (!isUnoccluded(el, r)) continue;

    let label = cleanText(el.getAttribute('aria-label') || '');
    if (!label && el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) label = cleanText(lbl.innerText);
    }
    if (!label) {
      const pl = el.closest('label');
      if (pl) label = cleanText(pl.innerText);
    }
    const ph = cleanText(el.getAttribute('placeholder') || '');
    const nm = (el.getAttribute('name') || '').toLowerCase();
    const idl = (el.id || '').toLowerCase();
    if (host.includes('baidu.com') && (idl === 'kw' || nm === 'wd')) {
      label = '百度搜索框';
    } else if (host.includes('bilibili.com') && (t === 'search' || ph.length > 0)) {
      label = 'B站搜索框';
    } else if (host.includes('youtube.com') && (nm === 'search_query' || idl === 'search')) {
      label = 'YouTube 搜索框';
    } else if (host.includes('bing.com') && (idl === 'sb_form_q' || nm === 'q')) {
      label = '必应搜索框';
    } else if (!label) {
      if (ph && (ph.length <= 16 || inputKeywords.test(ph))) {
        label = ph.slice(0, 22);
      } else {
        label = (t === 'search' || nm === 'q') ? '搜索框' : '文本输入框';
      }
    }
    inputs.push({ label: label.slice(0, 22), rect: [r.left, r.top, r.right, r.bottom] });
  }

  const scrolls = [];
  const hasModal = !!document.querySelector('.modal-mask');
  if (!hasModal) {
    for (const el of document.querySelectorAll('[data-scroll-topic], div, section, main, article')) {
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
      const dir = window.scrollY > 40 ? 'up' : 'down';
      scrolls.push({
        topic: cleanText(title.split(/[｜|\-]/)[0] || '当前页面').slice(0, 20),
        rect: [Math.round(vw * 0.15), Math.round(vh * 0.25), Math.round(vw * 0.85), Math.round(vh * 0.88)],
        direction: dir,
        is_page: true,
      });
    }
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


def build_local_apps_v2() -> list[dict[str, str]]:
    """Return 8 domain-disjoint rich local Chinese web apps (6 train, 1 dev, 1 holdout) + modal variants."""
    theme_css = """
      :root { --bg:#f4f6fa; --card:#ffffff; --ink:#172130; --muted:#5c6c80; --line:#dde4ee; --accent:#2358d8; --accent-soft:#e8effe; --green:#1e8e5a; --red:#d63d3d; --orange:#d97316; }
      @media (prefers-color-scheme: dark) {
        :root { --bg:#121720; --card:#1b2330; --ink:#e9eff8; --muted:#97a7bc; --line:#2d394b; --accent:#4e83ff; --accent-soft:#1e2f4f; --green:#24a268; --red:#e24c4c; --orange:#e8852b; }
      }
      * { box-sizing:border-box; }
      body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 'Microsoft YaHei UI','Microsoft YaHei',sans-serif; }
      button, input, textarea, select { font:inherit; color:inherit; }
      button { cursor:pointer; }
      input[type="checkbox"] { width:16px; height:16px; accent-color:var(--accent); cursor:pointer; vertical-align:middle; }
      .modal-mask { position:fixed; inset:0; background:rgba(10,14,22,0.58); display:flex; align-items:center; justify-content:center; z-index:50; }
      .modal-box { width:440px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px 22px; box-shadow:0 18px 40px rgba(0,0,0,0.35); }
    """

    # 1. train: Chrome New Tab hub matching user's screenshot
    shortcuts = [
        ("ChatGPT", "#10a37f", "C"),
        ("YouTube", "#e62117", "▶"),
        ("Bilibili", "#00a1d6", "B"),
        ("X", "#1d1f23", "𝕏"),
        ("GitHub", "#24292e", "G"),
        ("Gemini", "#4285f4", "✦"),
        ("Grok", "#2b2b2b", "Ø"),
        ("Reddit", "#ff4500", "R"),
        ("DeepL", "#0f2b46", "译"),
        ("添加快捷方式", "#5a4b45", "+"),
    ]
    tiles_html = "".join(
        f'<a role="button" data-label="{name}" href="#sc-{i}" style="display:flex;flex-direction:column;align-items:center;gap:8px;text-decoration:none;color:var(--ink);width:86px;padding:8px 4px;border-radius:10px">'
        f'<span aria-hidden="true" style="width:46px;height:46px;border-radius:50%;background:{bg};color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700">{sym}</span>'
        f'<span style="font-size:12px">{name}</span></a>'
        for i, (name, bg, sym) in enumerate(shortcuts)
    )
    newtab_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>新标签页</title>
<style>{theme_css}
  body {{ background:var(--bg); display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; padding:24px; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#3c312b; }} }}
  .logo {{ font-size:74px; font-weight:700; letter-spacing:-2px; margin-bottom:26px; color:#e5c8b5; }}
  @media (prefers-color-scheme: light) {{ .logo {{ color:#2f4a7a; }} }}
  .search-pill {{ width:min(700px, 92vw); height:48px; background:var(--card); border:1px solid var(--line); border-radius:24px; display:flex; align-items:center; padding:0 10px 0 18px; gap:10px; box-shadow:0 4px 16px rgba(0,0,0,0.12); }}
  .search-pill input {{ flex:1; border:none; outline:none; background:transparent; font-size:15px; }}
  .ai-btn {{ padding:7px 14px; border-radius:18px; border:1px solid var(--line); background:var(--accent-soft); color:var(--ink); font-size:13px; font-weight:600; }}
  .cards {{ display:flex; gap:14px; margin:18px 0 26px; flex-wrap:wrap; justify-content:center; }}
  .feat {{ padding:10px 18px; border-radius:12px; border:1px solid var(--line); background:var(--card); text-align:left; min-width:210px; }}
  .tiles {{ display:flex; gap:10px; flex-wrap:wrap; justify-content:center; max-width:980px; }}
</style>
<body>
  <div class="logo">Google</div>
  <div class="search-pill">
    <input type="search" aria-label="问问 Google" placeholder="问问 Google">
    <button class="ai-btn" type="button">AI 模式</button>
  </div>
  <div class="cards">
    <button class="feat" data-label="开启 AI 模式，事半功倍" type="button"><b>开启 AI 模式，事半功倍</b><div style="font-size:12px;color:var(--muted)">创建、总结、规划</div></button>
    <button class="feat" data-label="生成图片" type="button"><b>生成图片</b><div style="font-size:12px;color:var(--muted)">创意编辑、绘图、修改</div></button>
  </div>
  <div class="tiles">{tiles_html}</div>
</body></html>"""

    # 2. train: AI chat workspace + modal variant
    def ai_chat_page(with_modal: bool) -> str:
        modal = (
            '<div class="modal-mask"><div class="modal-box">'
            '<h3 style="margin:0 0 10px">模型与联网偏好设置</h3>'
            '<label style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><input type="checkbox" data-check-label="启用代码解释器" checked> 启用代码解释器</label>'
            '<label style="display:flex;align-items:center;gap:8px;margin-bottom:16px"><input type="checkbox" data-check-label="保存历史对话"> 保存历史对话</label>'
            '<div style="display:flex;justify-content:flex-end;gap:10px">'
            '<button type="button" style="padding:7px 14px;border:1px solid var(--line);border-radius:6px;background:var(--bg)">取消</button>'
            '<button type="button" data-color-zh="蓝色" style="padding:7px 14px;border:none;border-radius:6px;background:var(--accent);color:#fff">保存设置</button>'
            '</div></div></div>'
            if with_modal
            else ""
        )
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>智核 AI 对话助手</title>
<style>{theme_css}
  .app {{ display:grid; grid-template-columns:230px 1fr; height:100vh; }}
  .side {{ background:var(--card); border-right:1px solid var(--line); padding:14px; display:flex; flex-direction:column; gap:8px; }}
  .new-chat {{ padding:9px 12px; border:1px solid var(--line); border-radius:8px; background:var(--accent-soft); color:var(--accent); font-weight:600; text-align:left; }}
  .hist {{ padding:8px 10px; border-radius:6px; text-decoration:none; color:var(--ink); display:block; }}
  .main {{ display:flex; flex-direction:column; height:100vh; }}
  .top {{ height:52px; border-bottom:1px solid var(--line); padding:0 20px; display:flex; align-items:center; justify-content:space-between; background:var(--card); }}
  .msgs {{ flex:1; overflow-y:auto; padding:20px 32px; display:flex; flex-direction:column; gap:14px; }}
  .msg {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .composer {{ border-top:1px solid var(--line); background:var(--card); padding:14px 24px; display:flex; flex-direction:column; gap:10px; }}
  .composer textarea {{ width:100%; height:64px; border:1px solid var(--line); border-radius:8px; padding:10px; background:var(--bg); resize:none; }}
  .bar {{ display:flex; justify-content:space-between; align-items:center; }}
  .btn {{ padding:7px 14px; border-radius:7px; border:1px solid var(--line); background:var(--bg); }}
</style>
<body><div class="app">
  <aside class="side">
    <button class="new-chat" type="button">开启新对话</button>
    <a class="hist" href="#c1">Python 异步采集优化</a><a class="hist" href="#c2">显卡显存调度分析</a>
    <a class="hist" href="#c3">Chrome DPI 对齐排查</a><a class="hist" href="#c4">多轮定位错误复盘</a>
  </aside>
  <section class="main">
    <header class="top">
      <select data-label="模型选择" aria-label="模型选择"><option>模型：Flash-2.5</option><option>模型：Pro-2.5</option></select>
      <div style="display:flex;gap:8px"><button class="btn" type="button">分享对话</button><button class="btn" type="button">偏好设置</button></div>
    </header>
    <div class="msgs" data-scroll-topic="AI 对话记录">
      <div class="msg"><b>我的提问：</b>请对比 Q4_K_M 与 Q8_0 在端侧推理时的显存与速度表现。</div>
      <div class="msg"><b>助手回复：</b>Q4_K_M 显存占用约 3.0 GiB，热态单步约 0.6s；Q8_0 约 3.7 GiB。<div style="margin-top:10px;display:flex;gap:8px"><button class="btn" type="button">复制回复</button><button class="btn" type="button">重新生成</button></div></div>
      {"".join(f'<div class="msg"><b>历史消息 #{i}：</b>确认 Win32 DPI 感知上下文已在启动第一时间初始化。</div>' for i in range(1, 7))}
    </div>
    <footer class="composer">
      <textarea placeholder="输入你的问题或粘贴代码"></textarea>
      <div class="bar">
        <div style="display:flex;gap:10px"><button class="btn" type="button">联网搜索</button><button class="btn" type="button">深度思考</button></div>
        <button class="btn" type="button" data-color-zh="蓝色" style="background:var(--accent);color:#fff;border-color:var(--accent)">发送消息</button>
      </div>
    </footer>
  </section>
</div>{modal}</body></html>"""

    # 3. train: DeepL-style bilingual translator
    translator_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>简译 DeepL 风格翻译台</title>
<style>{theme_css}
  header {{ background:var(--card); border-bottom:1px solid var(--line); padding:12px 24px; display:flex; align-items:center; justify-content:space-between; }}
  .wrap {{ max-width:1120px; margin:20px auto; padding:0 24px; }}
  .ctrl {{ display:flex; align-items:center; gap:12px; margin-bottom:12px; }}
  .ctrl select {{ height:36px; padding:0 12px; border:1px solid var(--line); border-radius:7px; background:var(--card); }}
  .panes {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .pane {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; display:flex; flex-direction:column; gap:12px; min-height:360px; }}
  .pane textarea {{ flex:1; border:none; outline:none; background:transparent; font-size:15px; resize:none; }}
  .foot {{ display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--line); padding-top:10px; }}
  .btn {{ padding:7px 14px; border:1px solid var(--line); border-radius:7px; background:var(--bg); }}
</style>
<body>
  <header>
    <div><b>简译 Translator</b></div>
    <div style="display:flex;gap:10px"><button class="btn" type="button">文档翻译</button><button class="btn" type="button">自定义术语表</button></div>
  </header>
  <div class="wrap">
    <div class="ctrl">
      <select data-label="源语言"><option>检测语言</option><option>英语</option><option>日语</option></select>
      <button class="btn" type="button">互换语言</button>
      <select data-label="目标语言"><option>中文（简体）</option><option>英语（美式）</option></select>
      <label style="margin-left:auto;display:flex;align-items:center;gap:6px"><input type="checkbox" data-check-label="保留专业术语"> 保留专业术语</label>
    </div>
    <div class="panes">
      <section class="pane">
        <textarea placeholder="输入或粘贴要翻译的文本"></textarea>
        <div class="foot"><button class="btn" type="button">清空原文</button><button class="btn" type="button">上传文件</button></div>
      </section>
      <section class="pane">
        <div style="flex:1;font-size:15px">实时译文区域：支持长文档分段对照与上下文一致校对。</div>
        <div class="foot">
          <button class="btn" type="button">朗读译文</button>
          <button class="btn" type="button" data-color-zh="绿色" style="background:var(--green);color:#fff;border-color:var(--green)">复制译文</button>
        </div>
      </section>
    </div>
  </div>
</body></html>"""

    # 4. train: Enterprise admin table + modal variant
    def admin_table_page(with_modal: bool) -> str:
        modal = (
            '<div class="modal-mask"><div class="modal-box">'
            '<h3 style="margin:0 0 8px">确认关闭弹窗或提交变更</h3>'
            '<p style="color:var(--muted);margin:0 0 16px">将对所选工单状态进行批量同步，是否继续？</p>'
            '<div style="display:flex;justify-content:flex-end;gap:10px">'
            '<button type="button" style="padding:7px 14px;border:1px solid var(--line);border-radius:6px;background:var(--bg)">关闭弹窗</button>'
            '<button type="button" data-color-zh="红色" style="padding:7px 14px;border:none;border-radius:6px;background:var(--red);color:#fff">确认删除</button>'
            '</div></div></div>'
            if with_modal
            else ""
        )
        rows = [
            ("ORD-202611-01", "华东节点GPU扩容", "陈思远", "待处理"),
            ("ORD-202611-02", "模型镜像同步校验", "林知夏", "进行中"),
            ("ORD-202611-03", "夜间离线批测队列", "周明宇", "待审核"),
            ("ORD-202611-04", "对象存储权限巡检", "许安然", "已完成"),
            ("ORD-202611-05", "网关证书自动续期", "顾清河", "待处理"),
        ]
        tr_html = "".join(
            f'<tr data-row-label="{oid}">'
            f'<td><input type="checkbox" data-check-label="选择工单"> {oid}</td><td>{topic}</td><td>{owner}</td><td>{st}</td>'
            f'<td style="display:flex;gap:6px"><button class="mini" type="button">查看</button><button class="mini" type="button">编辑</button><button class="mini" type="button">指派</button></td></tr>'
            for oid, topic, owner, st in rows
        )
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>工单与资源调度台｜后台管理</title>
<style>{theme_css}
  .shell {{ display:grid; grid-template-columns:205px 1fr; min-height:100vh; }}
  .nav {{ background:var(--card); border-right:1px solid var(--line); padding:16px 14px; display:flex; flex-direction:column; gap:8px; }}
  .nav a {{ padding:8px 10px; border-radius:6px; text-decoration:none; color:var(--ink); display:block; }}
  .nav a.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .main {{ padding:20px 24px; }}
  .toolbar {{ display:flex; gap:10px; align-items:center; margin-bottom:14px; flex-wrap:wrap; }}
  .toolbar input {{ flex:1; min-width:200px; height:36px; border:1px solid var(--line); border-radius:6px; padding:0 12px; background:var(--card); }}
  .toolbar select {{ height:36px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:var(--card); }}
  .btn {{ padding:7px 13px; border:1px solid var(--line); border-radius:6px; background:var(--card); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:11px 14px; text-align:left; border-bottom:1px solid var(--line); }}
  .mini {{ padding:4px 9px; font-size:12px; border:1px solid var(--line); border-radius:5px; background:var(--bg); }}
</style>
<body><div class="shell">
  <aside class="nav"><b style="color:var(--accent);font-size:16px;margin-bottom:6px">云枢调度平台</b>
    <a class="active" href="#orders">工单列表</a><a href="#nodes">算力节点</a><a href="#logs">操作日志</a><a href="#alerts">异常告警</a>
  </aside>
  <main class="main">
    <div class="toolbar">
      <input type="search" placeholder="输入工单号或负责人姓名搜索">
      <select data-label="工单状态"><option>全部状态</option><option>待处理</option><option>已完成</option></select>
      <button class="btn" type="button" data-color-zh="红色" style="background:var(--red);color:#fff;border-color:var(--red)">清空草稿</button>
      <button class="btn" type="button" data-color-zh="蓝色" style="background:var(--accent);color:#fff;border-color:var(--accent)">新建工单</button>
    </div>
    <div class="card"><table>
      <thead><tr><th>工单编号</th><th>任务主题</th><th>负责人</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{tr_html}</tbody>
    </table></div>
  </main>
</div>{modal}</body></html>"""

    # 5. train: E-commerce
    shop_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>极物严选 · 数码好物馆</title>
<style>{theme_css}
  header {{ background:var(--card); border-bottom:1px solid var(--line); padding:12px 24px; display:flex; align-items:center; gap:14px; }}
  .search-wrap {{ flex:1; display:flex; gap:8px; max-width:520px; }}
  .search-wrap input {{ flex:1; height:36px; border:1px solid var(--line); border-radius:18px; padding:0 14px; background:var(--bg); }}
  .pill {{ padding:6px 14px; border-radius:16px; border:1px solid var(--line); background:var(--card); }}
  .container {{ max-width:1180px; margin:0 auto; padding:16px 24px; }}
  .notice {{ background:var(--accent-soft); border:1px solid var(--line); border-radius:8px; padding:10px 14px; display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }}
  .layout {{ display:grid; grid-template-columns:1fr 300px; gap:16px; margin-top:12px; }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
  .item {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
  .review-box {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; height:330px; overflow-y:auto; }}
</style>
<body>
  <header>
    <b style="font-size:18px;color:var(--accent)">极物严选</b>
    <div class="search-wrap"><input type="search" placeholder="搜索机械键盘、显示器或耳机"><button class="pill" data-color-zh="蓝色" style="background:var(--accent);color:#fff;border:none" type="button">搜索商品</button></div>
    <button class="pill" type="button">我的购物车</button>
    <button class="pill" data-color-zh="橙色" style="background:var(--orange);color:#fff;border:none" type="button">立即结算</button>
  </header>
  <div class="container">
    <div class="notice"><span>双十一返场优惠：数码外设满 500 减 50</span><button class="pill" type="button">我知道了</button></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button class="pill" type="button">全部商品</button><button class="pill" type="button">机械键盘</button>
      <button class="pill" type="button">高刷显示器</button><button class="pill" type="button">降噪耳机</button>
      <label style="margin-left:auto;display:flex;align-items:center;gap:6px"><input type="checkbox" data-check-label="仅看现货"> 仅看现货</label>
    </div>
    <div class="layout">
      <div class="grid">
        <div class="item"><b>星辰87三模机械键盘</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥399</div><button class="pill" type="button">加入收藏</button></div>
        <div class="item"><b>4K HDR 设计显示器</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥1899</div><button class="pill" type="button">查看参数</button></div>
        <div class="item"><b>轻羽无线降噪耳机</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥699</div><button class="pill" type="button">立即选购</button></div>
        <div class="item"><b>双臂显示器支架</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥249</div><button class="pill" type="button">对比规格</button></div>
        <div class="item"><b>氮化镓140W快充头</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥179</div><button class="pill" type="button">领券购买</button></div>
        <div class="item"><b>桌面拾音条形音箱</b><div style="color:var(--red);font-weight:700;margin:8px 0">¥329</div><button class="pill" type="button">了解详情</button></div>
      </div>
      <aside class="review-box" data-scroll-topic="商品评价列表">
        <h3 style="margin-top:0">精选买家评价</h3>
        {"".join(f'<div style="padding:10px 0;border-bottom:1px solid var(--line)"><b>用户_{i:02d}</b>：手感扎实，连接稳定。</div>' for i in range(1, 14))}
      </aside>
    </div>
  </div>
</body></html>"""

    # 6. train: Collaborative doc workspace
    kb_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>云文档协同空间｜开发规范</title>
<style>{theme_css}
  .topbar {{ height:48px; background:var(--card); border-bottom:1px solid var(--line); padding:0 18px; display:flex; align-items:center; justify-content:space-between; }}
  .icon-btn {{ padding:4px 10px; font-size:12px; border:1px solid var(--line); border-radius:5px; background:var(--bg); }}
  .workspace {{ display:grid; grid-template-columns:220px 1fr 290px; height:calc(100vh - 48px); }}
  .tree {{ background:var(--card); border-right:1px solid var(--line); padding:14px; display:flex; flex-direction:column; gap:6px; }}
  .tree a {{ padding:7px 9px; border-radius:6px; text-decoration:none; color:var(--ink); }}
  .tree a.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .doc {{ padding:22px 30px; overflow-y:auto; }}
  .comments {{ background:var(--card); border-left:1px solid var(--line); padding:14px; display:flex; flex-direction:column; gap:10px; }}
  .comments textarea {{ width:100%; height:92px; border:1px solid var(--line); border-radius:6px; padding:8px; background:var(--bg); resize:none; }}
</style>
<body>
  <div class="topbar">
    <b>端侧视觉模型协同规范 v2.4</b>
    <div style="display:flex;gap:8px">
      <button class="icon-btn" type="button">收藏文档</button><button class="icon-btn" type="button">历史版本</button>
      <button class="icon-btn" type="button">共享协作</button><button class="icon-btn" type="button">页面设置</button>
    </div>
  </div>
  <div class="workspace">
    <nav class="tree">
      <a class="active" href="#dpi">DPI 与物理像素</a><a href="#coord">归一化坐标映射</a>
      <a href="#schema">闭集动作协议</a><a href="#safety">步前窗口校验</a><a href="#release">上线验收标准</a>
    </nav>
    <article class="doc" data-scroll-topic="文档正文">
      <h1>1. Windows DPI 与物理像素对齐规范</h1>
      {"".join(f"<p>第 {i} 节：所有窗口截图必须在 PER_MONITOR_AWARE_V2 上下文中读取 DWM 边界并完成像素级校验。</p>" for i in range(1, 20))}
    </article>
    <aside class="comments">
      <h3 style="margin:0">评审意见 (3)</h3>
      <textarea placeholder="输入您的评审意见或修改建议"></textarea>
      <button class="icon-btn" data-color-zh="蓝色" style="background:var(--accent);color:#fff;border-color:var(--accent);padding:8px" type="button">发表评论</button>
    </aside>
  </div>
</body></html>"""

    # 7. dev: Video feed (Bilibili/YouTube style)
    video_feed_html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>光影频道 · 精选视频流</title>
<style>{theme_css}
  header {{ background:var(--card); border-bottom:1px solid var(--line); padding:10px 24px; display:flex; align-items:center; gap:14px; }}
  .tabs {{ display:flex; gap:8px; }}
  .tab {{ padding:6px 12px; border-radius:6px; border:none; background:transparent; }}
  .tab.active {{ background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .search {{ margin-left:auto; display:flex; gap:8px; }}
  .search input {{ width:230px; height:34px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:var(--bg); }}
  .wrap {{ max-width:1180px; margin:16px auto; padding:0 24px; display:grid; grid-template-columns:1fr 290px; gap:18px; }}
  .feed {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px; height:520px; overflow-y:auto; padding-right:4px; }}
  .vcard {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }}
  .thumb {{ height:90px; border-radius:6px; background:linear-gradient(135deg,var(--accent-soft),var(--line)); margin-bottom:8px; }}
  .rank {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; height:fit-content; }}
</style>
<body>
  <header>
    <b style="font-size:17px">光影频道</b>
    <div class="tabs"><button class="tab active" type="button">综合推荐</button><button class="tab" type="button">科技测评</button><button class="tab" type="button">开源实战</button><button class="tab" type="button">生活记录</button></div>
    <div class="search"><input type="search" placeholder="搜索视频标题或UP主"><button class="tab" style="background:var(--accent);color:#fff" type="button">搜索视频</button></div>
  </header>
  <div class="wrap">
    <section class="feed" data-scroll-topic="精选视频列表">
      {"".join(f'<article class="vcard"><div class="thumb"></div><b>端侧多模态实战第 {i} 期</b><div style="margin-top:8px"><a href="#v{i}">播放视频 #{i}</a></div></article>' for i in range(1, 13))}
    </section>
    <aside class="rank">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><b>热门榜单</b><button class="tab" style="border:1px solid var(--line)" type="button">换一批</button></div>
      <button style="width:100%;padding:8px;border:none;border-radius:7px;background:var(--red);color:#fff;margin-bottom:10px" data-color-zh="红色" type="button">立即订阅</button>
      <ol style="padding-left:18px;margin:0;display:grid;gap:8px"><li><a href="#r1">4060 笔记本部署实录</a></li><li><a href="#r2">高DPI截图避坑合集</a></li><li><a href="#r3">浏览器动作协议讲解</a></li></ol>
    </aside>
  </div>
</body></html>"""

    # 8. holdout: Forum thread (Reddit/V2EX style) + modal variant
    def forum_thread_page(with_modal: bool) -> str:
        modal = (
            '<div class="modal-mask"><div class="modal-box">'
            '<h3 style="margin:0 0 8px">帖子管理操作</h3>'
            '<label style="display:flex;align-items:center;gap:8px;margin-bottom:14px"><input type="checkbox" data-check-label="开启回复提醒"> 开启回复提醒</label>'
            '<div style="display:flex;justify-content:flex-end;gap:10px">'
            '<button type="button" style="padding:7px 14px;border:1px solid var(--line);border-radius:6px;background:var(--bg)">关闭窗口</button>'
            '<button type="button" data-color-zh="蓝色" style="padding:7px 14px;border:none;border-radius:6px;background:var(--accent);color:#fff">确认应用</button>'
            '</div></div></div>'
            if with_modal
            else ""
        )
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>技术讨论社区｜话题详情</title>
<style>{theme_css}
  header {{ background:var(--card); border-bottom:1px solid var(--line); padding:10px 24px; display:flex; align-items:center; gap:14px; }}
  header input {{ width:260px; height:34px; border:1px solid var(--line); border-radius:16px; padding:0 12px; background:var(--bg); }}
  .wrap {{ max-width:1080px; margin:16px auto; padding:0 24px; display:grid; grid-template-columns:1fr 290px; gap:18px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }}
  .btn {{ padding:6px 12px; border:1px solid var(--line); border-radius:6px; background:var(--bg); }}
  .stream {{ max-height:360px; overflow-y:auto; margin-top:12px; border-top:1px solid var(--line); }}
  .cmt {{ padding:10px 0; border-bottom:1px solid var(--line); }}
  textarea {{ width:100%; height:78px; border:1px solid var(--line); border-radius:7px; padding:8px; background:var(--bg); margin-top:10px; resize:none; }}
</style>
<body>
  <header>
    <b style="font-size:17px;color:var(--accent)">开源技术社区</b>
    <input type="search" placeholder="搜索讨论主题或作者">
    <select data-label="排序方式" style="height:34px;border:1px solid var(--line);border-radius:6px;padding:0 8px;background:var(--bg)"><option>按热度排序</option><option>按时间排序</option></select>
    <button class="btn" style="margin-left:auto" type="button">管理帖子</button>
  </header>
  <div class="wrap">
    <main class="card">
      <h2 style="margin:0 0 8px">本地部署 2B 视觉模型的操作稳定度经验分享</h2>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button class="btn" type="button">赞同主题</button><button class="btn" type="button">收藏帖子</button><button class="btn" type="button">转发链接</button>
      </div>
      <textarea placeholder="写下你的讨论回复"></textarea>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" data-check-label="匿名发表"> 匿名发表</label>
        <button class="btn" data-color-zh="绿色" style="background:var(--green);color:#fff;border-color:var(--green)" type="button">提交回复</button>
      </div>
      <div class="stream" data-scroll-topic="讨论回复流">
        {"".join(f'<div class="cmt"><label style="display:inline-flex;align-items:center;gap:6px"><input type="checkbox" data-check-label="标记第 {i} 楼"> <b>楼层 #{i}</b></label>：补充了多窗口缩放测试建议。</div>' for i in range(1, 11))}
      </div>
    </main>
    <aside class="card" style="height:fit-content">
      <h3 style="margin-top:0">社区版块</h3>
      <div style="display:grid;gap:8px"><a href="#b1">视觉智能体</a><a href="#b2">本地推理优化</a><a href="#b3">前端无障碍与UIA</a></div>
    </aside>
  </div>
{modal}</body></html>"""

    return [
        {"slug": "newtab_hub", "domain": "local_newtab_hub", "html": newtab_html},
        {"slug": "ai_chat", "domain": "local_ai_chat", "html": ai_chat_page(False)},
        {"slug": "ai_chat_modal", "domain": "local_ai_chat", "html": ai_chat_page(True)},
        {"slug": "translator", "domain": "local_translator", "html": translator_html},
        {"slug": "admin_table", "domain": "local_admin_table", "html": admin_table_page(False)},
        {"slug": "admin_table_modal", "domain": "local_admin_table", "html": admin_table_page(True)},
        {"slug": "ecommerce", "domain": "local_ecommerce", "html": shop_html},
        {"slug": "kb_workspace", "domain": "local_kb_workspace", "html": kb_html},
        {"slug": "video_feed", "domain": "local_video_feed", "html": video_feed_html},
        {"slug": "forum_thread", "domain": "local_forum_thread", "html": forum_thread_page(False)},
        {"slug": "forum_thread_modal", "domain": "local_forum_thread", "html": forum_thread_page(True)},
    ]


def start_local_server_v2(pages: list[dict[str, str]], bg_tabs: list[tuple[str, str]]) -> tuple[ThreadingHTTPServer, str]:
    routes: dict[str, str] = {f"/{p['slug']}": p["html"] for p in pages}
    for slug, title in bg_tabs:
        routes[f"/bg/{slug}"] = (
            f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{title}</title>"
            f"<body style='font-family:sans-serif;padding:40px'><h2>{title}</h2><p>背景参考标签页。</p></body></html>"
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

    srv = QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def spatial_region(norm_box: list[int]) -> str | None:
    cx = (norm_box[0] + norm_box[2]) / 2.0
    cy = (norm_box[1] + norm_box[3]) / 2.0
    if cx > 680 and cy < 260:
        return "top_right"
    if cx < 280 and cy < 260:
        return "top_left"
    if cx > 680 and cy > 740:
        return "bottom_right"
    if cx < 280 and cy > 740:
        return "bottom_left"
    if cy < 240:
        return "top"
    if cy > 760:
        return "bottom"
    if cx < 230:
        return "left"
    if cx > 770:
        return "right"
    return None


REGION_ZH = {
    "top_right": "右上角",
    "top_left": "左上角",
    "bottom_right": "右下角",
    "bottom_left": "左下角",
    "top": "页面顶部",
    "bottom": "页面底部",
    "left": "页面左侧",
    "right": "页面右侧",
}

OPPOSITE_REGION_ZH = {
    "top_right": "左下角",
    "top_left": "右下角",
    "bottom_right": "左上角",
    "bottom_left": "右上角",
    "top": "页面底部",
    "bottom": "页面顶部",
    "left": "页面右侧",
    "right": "页面左侧",
}


def make_instruction_v2(rng: random.Random, kind: str, **kw: Any) -> str:
    if kind == "native_click":
        t = kw["target"]
        return rng.choice([f"点击 Chrome 顶部的「{t}」。", f"请帮我点一下浏览器{t}。", f"定位并左键点击{t}。"])
    if kind == "tab_click":
        t = kw["title"]
        return rng.choice([f"切换到标题为「{t}」的标签页。", f"点击顶部标签栏里的「{t}」标签页。", f"点一下「{t}」浏览器标签页。"])
    if kind == "bookmark_click":
        t = kw["title"]
        return rng.choice([f"点击书签栏里的「{t}」。", f"打开书签栏中的「{t}」书签。"])
    if kind == "web_click":
        lbl = kw["label"]
        if kw.get("is_checkbox"):
            if kw.get("row_index") is not None:
                return f"勾选表格第 {kw['row_index']} 行的「{lbl}」复选框。"
            return rng.choice([f"勾选「{lbl}」复选框。", f"点击勾选「{lbl}」。"])
        if kw.get("is_dropdown"):
            return rng.choice([f"点击「{lbl}」下拉菜单。", f"打开「{lbl}」下拉选择框。"])
        if kw.get("row_index") is not None:
            r_idx = kw["row_index"]
            r_lbl = kw.get("row_label") or ""
            opts = [f"点击表格第 {r_idx} 行的「{lbl}」按钮。", f"请在表格第 {r_idx} 行点一下「{lbl}」。"]
            if r_lbl:
                opts.append(f"点击「{r_lbl}」这一行的「{lbl}」按钮。")
            return rng.choice(opts)
        if kw.get("spatial_zh"):
            return f"点击{kw['spatial_zh']}的「{lbl}」按钮。"
        if kw.get("color_zh"):
            return f"点击{kw['color_zh']}的「{lbl}」按钮。"
        return rng.choice(
            [f"点击页面上的「{lbl}」。", f"请帮我点一下「{lbl}」。", f"定位并左键点击「{lbl}」控件。", f"在网页中选择「{lbl}」。"]
        )
    if kind == "web_type":
        lbl, text = kw["label"], kw["text"]
        return rng.choice([f"在「{lbl}」输入框里填入「{text}」，不要提交。", f"点击「{lbl}」并输入文字「{text}」。", f"帮我在「{lbl}」中输入「{text}」。"])
    if kind == "web_scroll":
        dir_zh = "向下" if kw["direction"] == "down" else "向上"
        return rng.choice([f"在「{kw['topic']}」区域{dir_zh}滚动 {kw['amount']} 格。", f"把「{kw['topic']}」内容{dir_zh}滑动 {kw['amount']} 格。"])
    if kind == "not_found_tab":
        return rng.choice([f"点击标题为「{kw['title']}」的标签页。", f"切换到顶部名为「{kw['title']}」的浏览器标签页。"])
    if kind == "not_found_ext":
        return rng.choice([f"点击工具栏中的「{kw['ext']}」扩展按钮。", f"打开右上角扩展栏里的「{kw['ext']}」图标。"])
    if kind == "not_found_spatial":
        return f"点击{kw['opp_region']}的「{kw['label']}」按钮。"
    if kind == "not_found_color":
        return f"点击{kw['opp_color']}的「{kw['label']}」按钮。"
    if kind == "not_found_web":
        return rng.choice([f"点击当前画面中的「{kw['label']}」按钮。", f"在当前页面找到并点一下「{kw['label']}」。", f"请帮我点击页面里的「{kw['label']}」。"])
    raise ValueError(kind)


def generate_samples_v2(
    frame_id: str,
    split: str,
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

    def add_sample(
        bucket: str,
        instruction: str,
        expected: dict[str, Any],
        bbox_px: list[int] | None,
        target_desc: str,
        neg_subtype: str | None = None,
    ) -> None:
        nonlocal idx
        check_action(expected)
        idx += 1
        bbox_norm = px_box_to_norm(bbox_px, w, h) if bbox_px is not None else None
        if bbox_norm is not None and "coordinate" in expected:
            expected["coordinate"] = center_norm(bbox_norm)
        samples.append(
            {
                "sample_id": f"{frame_id}_{idx:02d}_{bucket}",
                "split": split,
                "image": rel_image,
                "image_sha256": image_sha256,
                "domain": domain,
                "source_type": source_type,
                "profile": profile_name,
                "bucket": bucket,
                "neg_subtype": neg_subtype,
                "target_desc": target_desc,
                "instruction": instruction,
                "expected": expected,
                "bbox": bbox_norm,
                "bbox_px": bbox_px,
            }
        )

    # 1. Native shell positives (3-4 items/frame; prioritize tab_search & small native icons!)
    picked_native: list[tuple[str, str, list[int]]] = []
    controls = uia_info["controls"]
    if "tab_search" in controls and rng.random() < 0.78:
        c = controls["tab_search"]
        picked_native.append(("native_click", c["label"], c["box_px"]))
    small_pool = [
        ("native_click", controls[k]["label"], controls[k]["box_px"])
        for k in ("bookmark_star", "new_tab", "chrome_menu", "reload", "back")
        if k in controls
    ]
    rng.shuffle(small_pool)
    for item in small_pool:
        if len(picked_native) < 2 and item not in picked_native:
            picked_native.append(item)

    other_native: list[tuple[str, str, list[int]]] = []
    if "address_bar" in controls:
        other_native.append(("native_click", controls["address_bar"]["label"], controls["address_bar"]["box_px"]))
    for tab in uia_info["tabs"]:
        st = tab["title"].split(" - Google Chrome")[0].strip()
        if len(st) > 26:
            st = st.split(" - ")[0].split("｜")[0].strip()[:26].strip()
        if len(st) >= 2:
            other_native.append(("tab_click", st, tab["box_px"]))
    for bm in uia_info["bookmarks"]:
        other_native.append(("bookmark_click", bm["title"][:20], bm["box_px"]))
    rng.shuffle(other_native)
    for item in other_native:
        if len(picked_native) >= 4:
            break
        if item not in picked_native:
            picked_native.append(item)

    for kind, label, box_px in picked_native:
        inst = (
            make_instruction_v2(rng, "tab_click", title=label)
            if kind == "tab_click"
            else make_instruction_v2(rng, "bookmark_click", title=label)
            if kind == "bookmark_click"
            else make_instruction_v2(rng, "native_click", target=label)
        )
        add_sample("native_shell_click", inst, {"action": "left_click", "coordinate": [0, 0]}, box_px, f"native:{label}")

    # 2. Native shell hard negatives (1-2 items)
    all_uia_text = " ".join(
        [t["title"] for t in uia_info["tabs"]]
        + [b["title"] for b in uia_info["bookmarks"]]
        + [c["uia_name"] for c in controls.values()]
    ).lower()
    vis_all_low = (all_uia_text + "\n" + web_info["visibleTextJoined"]).lower()

    absent_tabs = [t for t in ABSENT_TAB_POOL if t.lower() not in vis_all_low]
    if absent_tabs:
        tab_title = rng.choice(absent_tabs)
        add_sample(
            "native_not_found",
            make_instruction_v2(rng, "not_found_tab", title=tab_title),
            {"action": "not_found", "reason": f"未在标签栏中找到「{tab_title}」标签页"},
            None,
            f"absent_tab:{tab_title}",
            neg_subtype="absent_tab",
        )
    absent_exts = [e for e in ABSENT_EXTENSION_POOL if e.lower() not in vis_all_low]
    if absent_exts and rng.random() < 0.75:
        ext = rng.choice(absent_exts)
        add_sample(
            "native_not_found",
            make_instruction_v2(rng, "not_found_ext", ext=ext),
            {"action": "not_found", "reason": f"工具栏中未出现「{ext}」扩展图标"},
            None,
            f"absent_ext:{ext}",
            neg_subtype="absent_extension",
        )

    # 3. Web viewport positives (3-4 clicks + optional type + optional scroll)
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
    special_clicks = [c for c in clicks if c.get("is_checkbox") or c.get("is_dropdown") or c.get("color_zh")]
    plain_clicks = [c for c in clicks if c not in row_clicks and c not in special_clicks]
    chosen_clicks: list[dict[str, Any]] = []
    for bucket_src in (row_clicks[:2], special_clicks[:2], plain_clicks[:3]):
        for c in bucket_src:
            if len(chosen_clicks) < 4 and c not in chosen_clicks:
                chosen_clicks.append(c)

    for c in chosen_clicks:
        bpx = css_to_px(c["rect"])
        bnorm = px_box_to_norm(bpx, w, h)
        reg = spatial_region(bnorm)
        use_spatial = (
            reg is not None
            and c.get("row_index") is None
            and not c.get("is_checkbox")
            and not c.get("is_dropdown")
            and vis_all_low.count(c["label"].lower()) == 1
            and rng.random() < 0.3
        )
        use_color = bool(c.get("color_zh")) and not use_spatial and rng.random() < 0.45
        inst = make_instruction_v2(
            rng,
            "web_click",
            label=c["label"],
            is_checkbox=c.get("is_checkbox", False),
            is_dropdown=c.get("is_dropdown", False),
            row_index=c.get("row_index"),
            row_label=c.get("row_label"),
            spatial_zh=REGION_ZH[reg] if use_spatial else None,
            color_zh=c.get("color_zh") if use_color else None,
        )
        add_sample("web_click", inst, {"action": "left_click", "coordinate": [0, 0]}, bpx, f"web:{c['label']}")

    if web_info["inputs"]:
        inp = rng.choice(web_info["inputs"])
        q = rng.choice(CHINESE_QUERY_POOL)
        bpx = css_to_px(inp["rect"])
        add_sample(
            "web_type",
            make_instruction_v2(rng, "web_type", label=inp["label"], text=q),
            {"action": "type", "coordinate": [0, 0], "text": q},
            bpx,
            f"type:{inp['label']}",
        )

    if web_info["scrolls"]:
        sc = rng.choice(web_info["scrolls"])
        amt = rng.choice([2, 3])
        bpx = css_to_px(sc["rect"])
        add_sample(
            "web_scroll",
            make_instruction_v2(rng, "web_scroll", topic=sc["topic"], direction=sc["direction"], amount=amt),
            {"action": "scroll", "coordinate": [0, 0], "direction": sc["direction"], "amount": amt},
            bpx,
            f"scroll:{sc['topic']}",
        )

    # 4. Web same-screen counterfactual & hard negatives (2-3 items)
    web_negs_added = 0
    used_neg_labels: set[str] = set()

    # 4a. Spatial counterfactual (target exists clearly in extreme region A, ask for opposite region B)
    spatial_cands = []
    for c in web_info["clickables"]:
        if c.get("row_index") is not None or c.get("is_checkbox"):
            continue
        if re.match(r"^(#\d+|[a-z0-9_.-]+)$", c["label"]):
            continue
        bpx = css_to_px(c["rect"])
        bnorm = px_box_to_norm(bpx, w, h)
        reg = spatial_region(bnorm)
        if reg is not None and vis_all_low.count(c["label"].lower()) == 1:
            spatial_cands.append((c["label"], reg))
    if spatial_cands:
        lbl, reg = rng.choice(spatial_cands)
        opp_zh = OPPOSITE_REGION_ZH[reg]
        used_neg_labels.add(lbl)
        add_sample(
            "web_not_found",
            make_instruction_v2(rng, "not_found_spatial", opp_region=opp_zh, label=lbl),
            {"action": "not_found", "reason": "目标不在指令指定的屏幕位置"},
            None,
            f"spatial_cf:{lbl}({reg}->{opp_zh})",
            neg_subtype="spatial_counterfactual",
        )
        web_negs_added += 1

    # 4b. Color counterfactual (when local button has verified color_zh)
    color_cands = [c for c in web_info["clickables"] if c.get("color_zh") in COLOR_OPPOSITES]
    if color_cands:
        cc = rng.choice(color_cands)
        same_label_colors = {x.get("color_zh") for x in web_info["clickables"] if x["label"] == cc["label"]}
        opp_choices = [col for col in COLOR_OPPOSITES[cc["color_zh"]] if col not in same_label_colors]
        if opp_choices:
            opp_col = rng.choice(opp_choices)
            add_sample(
                "web_not_found",
                make_instruction_v2(rng, "not_found_color", opp_color=opp_col, label=cc["label"]),
                {"action": "not_found", "reason": "当前页面未见该颜色的目标按钮"},
                None,
                f"color_cf:{cc['label']}({cc['color_zh']}->{opp_col})",
                neg_subtype="color_counterfactual",
            )
            web_negs_added += 1

    # 4c. Near-match counterfactual
    near_cands: list[str] = []
    for c in web_info["clickables"]:
        for key, variants in NEAR_MATCH_MAP.items():
            if key in c["label"]:
                for v in variants:
                    if v.lower() not in vis_all_low and v not in used_neg_labels:
                        near_cands.append(v)
    if near_cands and web_negs_added < 3:
        term = rng.choice(near_cands)
        used_neg_labels.add(term)
        add_sample(
            "web_not_found",
            make_instruction_v2(rng, "not_found_web", label=term),
            {"action": "not_found", "reason": f"当前页面未出现「{term}」控件"},
            None,
            f"near_match:{term}",
            neg_subtype="near_match",
        )
        web_negs_added += 1

    # 4d. Offscreen or semantic absent
    if web_negs_added < 3:
        off_cands = [o for o in web_info["offscreenLabels"] if o.lower() not in vis_all_low and o not in used_neg_labels]
        if off_cands:
            term = rng.choice(off_cands)
            used_neg_labels.add(term)
            add_sample(
                "web_not_found",
                make_instruction_v2(rng, "not_found_web", label=term),
                {"action": "not_found", "reason": "目标控件在当前可视区域外，需先滚动页面"},
                None,
                f"offscreen:{term}",
                neg_subtype="offscreen",
            )
            web_negs_added += 1

    if web_negs_added < 3:
        pool = [t for t in ABSENT_WEB_CONTROL_POOL if t.lower() not in vis_all_low and t not in used_neg_labels]
        if pool:
            term = rng.choice(pool)
            add_sample(
                "web_not_found",
                make_instruction_v2(rng, "not_found_web", label=term),
                {"action": "not_found", "reason": f"当前页面未出现「{term}」控件"},
                None,
                f"semantic_absent:{term}",
                neg_subtype="semantic_absent",
            )

    return samples


def wait_page_ready_v2(cdp: CDPClient, target_url: str, timeout_s: float = 16.0) -> dict[str, Any]:
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.navigate", {"url": target_url}, timeout=timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = cdp.evaluate("({ready: document.readyState, len: document.body ? (document.body.innerText||'').trim().length : 0})")
            if isinstance(state, dict) and state.get("ready") == "complete" and state.get("len", 0) >= 20:
                time.sleep(0.9)
                return cdp.evaluate(JS_INSPECT_PAGE_V2, timeout=12.0)
        except Exception:
            pass
        time.sleep(0.35)
    time.sleep(0.8)
    return cdp.evaluate(JS_INSPECT_PAGE_V2, timeout=12.0)


def collect_m35_dataset(out_dir: Path, seed: int = 20261119, chrome_exe: Path = DEFAULT_CHROME) -> dict[str, Any]:
    # Assert strict domain disjointness across train / dev / holdout BEFORE launching Chrome
    split_domains: dict[str, set[str]] = {}
    for sp in ("train", "dev", "holdout"):
        split_domains[sp] = {t["domain"] for t in SPLIT_PUBLIC_TASKS[sp]} | {t["domain"] for t in SPLIT_LOCAL_PLANS[sp]}
    assert not (split_domains["train"] & split_domains["dev"]), "Domain overlap train vs dev"
    assert not (split_domains["train"] & split_domains["holdout"]), "Domain overlap train vs holdout"
    assert not (split_domains["dev"] & split_domains["holdout"]), "Domain overlap dev vs holdout"

    enable_per_monitor_dpi_awareness()
    rng = random.Random(seed)
    local_pages = build_local_apps_v2()
    all_bg_tabs = [tab for prof in PROFILES_V2 for tab in prof["bg_tabs"]]
    http_server, base_url = start_local_server_v2(local_pages, all_bg_tabs)
    uia = UIAHelper()
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 15) if Path(r"C:\Windows\Fonts\msyh.ttc").is_file() else ImageFont.load_default()

    # Build task list grouped by profile_idx so Chrome is launched once per profile
    tasks_by_profile: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for sp in ("train", "dev", "holdout"):
        for t in SPLIT_PUBLIC_TASKS[sp]:
            tasks_by_profile[t["profile_idx"]].append(
                {"split": sp, "source_type": "public_web", "domain": t["domain"], "url": t["url"], "scroll": t["scroll"]}
            )
        for t in SPLIT_LOCAL_PLANS[sp]:
            tasks_by_profile[t["profile_idx"]].append(
                {"split": sp, "source_type": "rich_local", "domain": t["domain"], "url": f"{base_url}/{t['slug']}", "scroll": t["scroll"]}
            )

    all_records: list[dict[str, Any]] = []
    frame_logs: list[dict[str, Any]] = []
    skipped_logs: list[dict[str, Any]] = []
    seen_png_hashes: set[str] = set()
    split_seq = Counter()

    try:
        for prof_idx, prof in enumerate(PROFILES_V2):
            prof_tasks = tasks_by_profile.get(prof_idx, [])
            if not prof_tasks:
                continue
            with tempfile.TemporaryDirectory(prefix=f"m35_prof_{prof_idx}_", ignore_cleanup_errors=True) as tmp:
                prof_dir = Path(tmp)
                prepare_chrome_profile(prof_dir, prof["show_bookmarks"])
                first_bg_url = f"{base_url}/bg/{prof['bg_tabs'][0][0]}"
                proc, hwnd, port = launch_chrome_session(
                    chrome_exe, prof_dir, prof["window_size"], prof["scale"], prof["dark_mode"], first_bg_url
                )
                try:
                    time.sleep(0.8)
                    tabs_json = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
                    main_target = next(t for t in tabs_json if t.get("type") == "page")
                    cdp = CDPClient(main_target["webSocketDebuggerUrl"])
                    try:
                        for slug, _ in prof["bg_tabs"][1:]:
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

                        for task in prof_tasks:
                            sp = task["split"]
                            for scroll_pass in ([False, True] if task["scroll"] else [False]):
                                url = task["url"]
                                try:
                                    if not scroll_pass:
                                        web_info = wait_page_ready_v2(cdp, url)
                                    else:
                                        cdp.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.72))")
                                        time.sleep(0.6)
                                        web_info = cdp.evaluate(JS_INSPECT_PAGE_V2, timeout=12.0)

                                    if not isinstance(web_info, dict) or web_info.get("blockedReason"):
                                        skipped_logs.append(
                                            {"split": sp, "url": url, "profile": prof["name"], "reason": (web_info or {}).get("blockedReason", "inspect_failed")}
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
                                            {"split": sp, "url": url, "profile": prof["name"], "reason": f"bad_vp_bounds:{vp_left},{vp_top}"}
                                        )
                                        continue

                                    crop_vp = full_img.crop((vp_left, vp_top, vp_left + vp_w, vp_top + vp_h))
                                    diff = np.abs(np.asarray(crop_vp, dtype=np.int16) - np.asarray(vp_img, dtype=np.int16))
                                    med_diff = float(np.median(diff))
                                    mean_diff = float(np.mean(diff))
                                    if med_diff > 5.0 or mean_diff > 15.0:
                                        skipped_logs.append(
                                            {"split": sp, "url": url, "profile": prof["name"], "reason": f"align:{med_diff:.2f}/{mean_diff:.2f}"}
                                        )
                                        continue

                                    raw_uia = uia.query(hwnd, info.dwm_rect.top + vp_top + 8)
                                    allowed_bm = {"Python 中文文档", "MDN 参考手册", "菜鸟教程"} if prof["show_bookmarks"] else set()
                                    uia_info = classify_uia_controls(raw_uia, info, vp_top, allowed_bookmarks=allowed_bm)
                                    web_info["vp_origin"] = [vp_left, vp_top]

                                    split_seq[sp] += 1
                                    frame_id = f"{sp}_{split_seq[sp]:03d}"
                                    rel_img = f"frames/{sp}/{frame_id}.png"
                                    img_path = out_dir / rel_img
                                    img_path.parent.mkdir(parents=True, exist_ok=True)
                                    full_img.save(img_path, format="PNG", compress_level=4)
                                    img_sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
                                    if img_sha in seen_png_hashes:
                                        img_path.unlink()
                                        continue
                                    seen_png_hashes.add(img_sha)

                                    samples = generate_samples_v2(
                                        frame_id,
                                        sp,
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
                                        "split": sp,
                                        "image": rel_img,
                                        "url": url,
                                        "scroll_pass": scroll_pass,
                                        "domain": task["domain"],
                                        "source_type": task["source_type"],
                                        "profile": prof["name"],
                                        "scale": prof["scale"],
                                        "img_size": list(full_img.size),
                                        "png_sha256": img_sha,
                                        "median_abs_diff": round(med_diff, 3),
                                        "mean_abs_diff": round(mean_diff, 3),
                                        "sample_count": len(samples),
                                    }
                                    frame_logs.append(fmeta)
                                    draw_qa_overlay(full_img, samples, fmeta, out_dir / "qa_overlays" / sp / f"{frame_id}.png", font)
                                    print(f"[{sp.upper()}] {frame_id} ({task['source_type']}:{task['domain']}) n={len(samples)} diff={med_diff:.1f}/{mean_diff:.2f}")
                                except Exception as exc:
                                    skipped_logs.append({"split": sp, "url": url, "profile": prof["name"], "reason": f"exc:{type(exc).__name__}:{str(exc)[:140]}"})
                                    print(f"[SKIP:{sp}] {url} -> {exc}")
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

    with (out_dir / "metadata_all.jsonl").open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    manifest = package_and_verify(out_dir, frame_logs, all_records, seed)
    summary = {
        "frames_by_split": dict(Counter(f["split"] for f in frame_logs)),
        "samples_by_split": dict(Counter(r["split"] for r in all_records)),
        "by_split_bucket": {
            sp: dict(Counter(r["bucket"] for r in all_records if r["split"] == sp))
            for sp in ("train", "dev", "holdout")
        },
        "by_split_source": {
            sp: dict(Counter(r["source_type"] for r in all_records if r["split"] == sp))
            for sp in ("train", "dev", "holdout")
        },
        "web_neg_subtypes": dict(Counter(r["neg_subtype"] for r in all_records if r["bucket"] == "web_not_found")),
        "frames": frame_logs,
        "skipped": skipped_logs,
        "bundles": manifest,
    }
    (out_dir / "collection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("frames", "skipped")}, ensure_ascii=False, indent=2))
    return summary


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_and_verify(
    out_dir: Path,
    frame_logs: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """Verify zero cross-split / M3-receipt hash overlap and pack train_dev.tar + holdout.zip."""
    receipt_path = PROJECT_ROOT / "models" / "m3_gguf" / "data_receipt.json"
    prior_hashes: set[str] = set()
    if receipt_path.is_file():
        rec = json.loads(receipt_path.read_text(encoding="utf-8"))
        prior_hashes = set(rec.get("train_hashes", [])) | set(rec.get("validation_hashes", []))

    split_png_hashes: dict[str, set[str]] = defaultdict(set)
    split_domains: dict[str, set[str]] = defaultdict(set)
    for f in frame_logs:
        assert f["png_sha256"] not in prior_hashes, f"PNG hash overlaps M3 prior receipt: {f['frame_id']}"
        split_png_hashes[f["split"]].add(f["png_sha256"])
        split_domains[f["split"]].add(f["domain"])

    for a, b in (("train", "dev"), ("train", "holdout"), ("dev", "holdout")):
        assert not (split_png_hashes[a] & split_png_hashes[b]), f"Image hash leakage {a} vs {b}"
        assert not (split_domains[a] & split_domains[b]), f"Domain leakage {a} vs {b}"

    # Package 1: train + dev tar
    td_dir = out_dir / "_stage_train_dev"
    if td_dir.exists():
        shutil.rmtree(td_dir)
    (td_dir / "images").mkdir(parents=True)
    jpg_map: dict[str, tuple[str, str]] = {}
    td_jpg_hashes: dict[str, set[str]] = defaultdict(set)

    for f in frame_logs:
        if f["split"] not in {"train", "dev"}:
            continue
        src_png = out_dir / "frames" / f["split"] / f["frame_id"]
        src_png = src_png.with_suffix(".png")
        with Image.open(src_png) as raw:
            resized = resize_for_qwen35(raw.convert("RGB"))
        rel_jpg = f"images/{f['frame_id']}.jpg"
        dst_jpg = td_dir / rel_jpg
        resized.save(dst_jpg, format="JPEG", quality=93)
        jhash = sha256_file(dst_jpg)
        assert jhash not in prior_hashes, f"JPEG hash overlaps M3 prior receipt: {f['frame_id']}"
        td_jpg_hashes[f["split"]].add(jhash)
        jpg_map[f["image"]] = (rel_jpg, jhash)

    assert not (td_jpg_hashes["train"] & td_jpg_hashes["dev"]), "JPEG hash overlap train vs dev"

    sft_train: list[dict[str, Any]] = []
    sft_val: list[dict[str, Any]] = []
    val_eval_rows: list[dict[str, Any]] = []
    meta_rows: list[dict[str, Any]] = []

    for r in all_records:
        check_action(r["expected"])
        if r["split"] in {"train", "dev"}:
            rel_jpg, _ = jpg_map[r["image"]]
            sft_row = {
                "sample_id": r["sample_id"],
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"<image>{r['instruction']}"},
                    {"role": "assistant", "content": json.dumps(r["expected"], ensure_ascii=False, separators=(",", ":"))},
                ],
                "images": [rel_jpg],
            }
            if r["split"] == "train":
                sft_train.append(sft_row)
            else:
                sft_val.append(sft_row)
                val_eval_rows.append(
                    {
                        "sample_id": r["sample_id"],
                        "image": rel_jpg,
                        "domain": r["domain"],
                        "source_type": r["source_type"],
                        "bucket": r["bucket"],
                        "neg_subtype": r["neg_subtype"],
                        "instruction": r["instruction"],
                        "expected": r["expected"],
                        "bbox": r["bbox"],
                    }
                )
            meta_rows.append({k: v for k, v in r.items() if k != "bbox_px"} | {"jpg_path": rel_jpg})

    def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as fp:
            for row in rows:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")

    write_jsonl(td_dir / "train_real_chrome.jsonl", sft_train)
    write_jsonl(td_dir / "val_real_chrome.jsonl", sft_val)
    write_jsonl(td_dir / "val_real_chrome_eval.jsonl", val_eval_rows)
    write_jsonl(td_dir / "metadata_real_chrome.jsonl", meta_rows)

    # Dry-run m3_train_colab.load_sft to guarantee 100% compatibility!
    img_hashes_cache: dict[Path, str] = {}
    loaded_train = load_sft(td_dir, "train_real_chrome.jsonl", "m35_real", img_hashes_cache, training=True)
    loaded_val = load_sft(td_dir, "val_real_chrome.jsonl", "m35_real", img_hashes_cache, training=False)
    assert len(loaded_train) == len(sft_train) and len(loaded_val) == len(sft_val)

    tar_path = out_dir / "m35_real_chrome_train_dev.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(td_dir, arcname=".")
    shutil.rmtree(td_dir)

    # Package 2: Locked Holdout zip
    holdout_records = [r for r in all_records if r["split"] == "holdout"]
    holdout_zip_rows: list[dict[str, Any]] = []
    zip_path = out_dir / "M35_real_chrome_HOLDOUT_DO_NOT_TRAIN.zip"
    added_imgs: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for r in holdout_records:
            src_png = out_dir / r["image"]
            arc_img = f"images/{Path(r['image']).name}"
            if arc_img not in added_imgs:
                zf.write(src_png, arc_img)
                added_imgs.add(arc_img)
            holdout_zip_rows.append(
                {
                    "sample_id": r["sample_id"],
                    "image": arc_img,
                    "image_sha256": r["image_sha256"],
                    "domain": r["domain"],
                    "source_type": r["source_type"],
                    "profile": r["profile"],
                    "bucket": r["bucket"],
                    "neg_subtype": r["neg_subtype"],
                    "instruction": r["instruction"],
                    "expected": r["expected"],
                    "bbox": r["bbox"],
                }
            )
        cap = {
            "purpose": "HOLDOUT_DO_NOT_TRAIN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "seed": seed,
            "count": len(holdout_zip_rows),
            "frame_count": len(added_imgs),
            "domains": sorted(split_domains["holdout"]),
            "system_prompt": SYSTEM_PROMPT,
        }
        zf.writestr("capture.json", json.dumps(cap, ensure_ascii=False, indent=2))
        zf.writestr("holdout_chrome.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in holdout_zip_rows))

    manifest = {
        "m35_real_chrome_train_dev.tar": {
            "bytes": tar_path.stat().st_size,
            "sha256": sha256_file(tar_path),
            "train_count": len(sft_train),
            "val_count": len(sft_val),
            "train_domains": sorted(split_domains["train"]),
            "dev_domains": sorted(split_domains["dev"]),
            "load_sft_verified": True,
        },
        "M35_real_chrome_HOLDOUT_DO_NOT_TRAIN.zip": {
            "bytes": zip_path.stat().st_size,
            "sha256": sha256_file(zip_path),
            "count": len(holdout_zip_rows),
            "frame_count": len(added_imgs),
            "holdout_domains": sorted(split_domains["holdout"]),
        },
    }
    (out_dir / "bundle_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def evaluate_holdout_baseline(zip_path: Path, out_dir: Path, models: list[str], port: int = 18210) -> dict[str, Any]:
    from m3_compare_holdout import run_model, score, write_json

    with zipfile.ZipFile(zip_path, "r") as zf:
        cap = json.loads(zf.read("capture.json"))
        assert cap["purpose"] == "HOLDOUT_DO_NOT_TRAIN"
        cases = [json.loads(line) for line in zf.read("holdout_chrome.jsonl").decode("utf-8").splitlines() if line.strip()]
        images: dict[str, str] = {}
        raw_cache: dict[str, str] = {}
        for case in cases:
            name = case["image"]
            if name not in raw_cache:
                raw = zf.read(name)
                if hashlib.sha256(raw).hexdigest() != case["image_sha256"]:
                    raise ValueError(f"Holdout hash mismatch for {name}")
                raw_cache[name] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
            images[case["sample_id"]] = raw_cache[name]

    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for index, name in enumerate(models):
        predictions, latency = run_model(name, cases, images, port + index, out_dir)
        by_bucket_raw = score([{**c, "domain": c["bucket"]} for c in cases], predictions, by_domain=True)
        by_bucket = {k.split("/", 1)[0]: v for k, v in by_bucket_raw.items()}
        web_neg_cases = [{**c, "domain": c.get("neg_subtype") or "unknown"} for c in cases if c["bucket"] == "web_not_found"]
        web_neg_preds = [p for p in predictions if p["sample_id"] in {c["sample_id"] for c in web_neg_cases}]
        by_neg_raw = score(web_neg_cases, web_neg_preds, by_domain=True)
        by_neg_subtype = {k.split("/", 1)[0]: v for k, v in by_neg_raw.items()}

        pos_total = sum(1 for c in cases if c["expected"]["action"] != "not_found")
        pos_false_ref = sum(v.get("false_refusal", 0) for k, v in score(cases, predictions).items() if k != "not_found")
        report[name] = {
            "by_action": score(cases, predictions),
            "by_bucket": by_bucket,
            "by_web_neg_subtype": by_neg_subtype,
            "by_source_type": score([{**c, "domain": c["source_type"]} for c in cases], predictions, by_domain=True),
            "false_refusal_overall": {"count": pos_false_ref, "total_pos": pos_total},
            "latency": latency,
        }
        write_json(out_dir / "report.json", report)
        print(json.dumps({name: report[name]}, ensure_ascii=False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="M3.5 leak-free full-window Chrome collector & holdout evaluator.")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "artifacts" / "m35_real_chrome_20261119")
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--eval-only", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--models", nargs="+", default=["m2", "m3"])
    parser.add_argument("--port", type=int, default=18210)
    parser.add_argument("--eval-out", type=Path, default=None)
    args = parser.parse_args()

    if not args.eval_only:
        collect_m35_dataset(args.out, seed=args.seed, chrome_exe=args.chrome)
    if not args.skip_eval:
        evaluate_holdout_baseline(
            args.out / "M35_real_chrome_HOLDOUT_DO_NOT_TRAIN.zip",
            args.eval_out or (args.out / "holdout_baseline"),
            args.models,
            port=args.port,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
