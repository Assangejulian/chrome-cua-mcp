"""Hybrid Chrome Computer Use engine combining M3.5 visual grounding with CDP/DOM execution."""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont
import win32gui

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from action_executor import (
    check_corner_failsafe,
    click_abs,
    execute_action,
    paste_text_via_clipboard,
    send_hotkey,
)
from action_schema import ActionValidationError, ParsedAction, parse_and_validate_action
from desktop_perceiver import (
    DesktopChromeURLReader,
    PersistentWinRTOCR,
    analyze_click_effect,
    match_ocr_target,
)
from live_chrome_pilot import (
    default_local_model,
    execute_confirmed_action,
    focus_chrome_window,
    start_llama_server,
    stop_llama_server,
)
from m0_bench_vlm import (
    M1_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    call_vlm,
    encode_image_b64,
    query_gpu_memory_mib,
    resize_to_token_budget,
    wait_for_server,
)
from m3_auto_collect_chrome import (
    CDPClient,
    DEFAULT_CHROME,
    UIAHelper,
    classify_uia_controls,
    launch_chrome_session,
    print_window_dwm,
)
from roi_utils import (
    ChromeWindowInfo,
    Rect,
    capture_chrome_window,
    capture_roi,
    enable_per_monitor_dpi_awareness,
    find_all_windows,
    find_chrome_windows,
    get_window_process_name,
    inspect_chrome_window,
    inspect_primary_screen,
    norm_to_screen,
)

MODEL_PRESETS = {
    "M4": ("m4_gguf", "m4-qwen35-2b-", "m4-mmproj-f16.gguf"),
    "M3.5": ("m35_gguf", "m35-qwen35-2b-", "m35-mmproj-f16.gguf"),
    "M3": ("m3_gguf", "m3-qwen35-2b-", "m3-mmproj-f16.gguf"),
    "M2": ("m2_gguf", "m2-qwen35-2b-", "m2-mmproj-f16.gguf"),
    "基座": ("qwen35_2b_gguf", "Qwen3.5-2B-", "mmproj-F16.gguf"),
}

RED_TIER_PATTERNS = [
    "付款", "支付", "立即购买", "提交订单", "确认支付", "去结算", "结算", "下单",
    "删除", "注销", "清空", "转账", "退出登录", "confirm order", "delete account",
    "checkout", "purchase", "pay now", "submit order",
]
RED_TIER_WORD_RE = re.compile(r"\b(?:pay|delete|remove|erase)\b", re.IGNORECASE)

JS_EXTRACT_DOM = r"""((maxElements) => {
  const vw = window.innerWidth || 1;
  const vh = window.innerHeight || 1;
  const dpr = window.devicePixelRatio || 1;
  const url = location.href;
  const title = document.title || '';
  const scrollY = window.scrollY || window.pageYOffset || 0;
  const maxScrollY = Math.max(0, (document.documentElement ? document.documentElement.scrollHeight : 0) - vh);

  function cleanText(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  function isVisible(el) {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') <= 0.05) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) return false;
    return true;
  }

  const allRoots = [document];
  const seenRoots = new Set([document]);
  function collectShadowRoots(root, depth = 0) {
    if (!root || depth > 8 || !root.querySelectorAll) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot && !seenRoots.has(el.shadowRoot)) {
        seenRoots.add(el.shadowRoot);
        allRoots.push(el.shadowRoot);
        collectShadowRoots(el.shadowRoot, depth + 1);
      }
    }
  }
  collectShadowRoots(document, 0);

  function queryAllDeep(sel) {
    const out = [];
    for (const r of allRoots) {
      for (const el of r.querySelectorAll(sel)) out.push(el);
    }
    return out;
  }

  function deepElementFromPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    let guard = 0;
    while (el && el.shadowRoot && guard < 8) {
      guard++;
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  function collectDeepText() {
    const chunks = [];
    if (document.body && document.body.innerText) {
      chunks.push(document.body.innerText);
    }
    for (const r of allRoots) {
      if (r === document) continue;
      for (const child of r.children) {
        if (child.tagName === 'STYLE' || child.tagName === 'SCRIPT' || child.tagName === 'LINK') continue;
        const txt = cleanText(child.innerText || child.textContent || '');
        if (txt) chunks.push(txt);
      }
    }
    return cleanText(chunks.join(' '));
  }

  function isContainedInOverlay(el, containers) {
    for (const c of containers) {
      let cur = el;
      let steps = 0;
      while (cur && steps < 40) {
        if (cur === c) return true;
        cur = cur.parentNode || cur.host || null;
        steps++;
      }
    }
    return false;
  }

  const bodyText = collectDeepText();
  const titleLow = title.toLowerCase();
  const topLow = bodyText.slice(0, 700).toLowerCase();

  let gateReason = null;
  for (const sig of ['请完成安全验证', '人机验证', '滑动验证', 'just a moment', 'checking your browser', 'verify you are human', 'access denied', '403 forbidden']) {
    if (titleLow.includes(sig) || topLow.includes(sig)) {
      gateReason = 'anti_bot:' + sig;
      break;
    }
  }
  if (!gateReason) {
    for (const f of queryAllDeep('iframe')) {
      const src = (f.getAttribute('src') || '').toLowerCase();
      if ((src.includes('captcha') || src.includes('recaptcha') || src.includes('turnstile') || src.includes('hcaptcha')) && isVisible(f)) {
        gateReason = 'captcha_iframe';
        break;
      }
    }
  }

  // Detect modal popups / blocking overlays (excluding thin sticky headers)
  let popupDetected = false;
  const overlayContainers = [];
  const modalSel = '[role="dialog"], [aria-modal="true"], dialog[open], .modal, .dialog, .popup, [class*="modal"], [class*="popup"], [class*="dialog"]';
  for (const el of queryAllDeep(modalSel)) {
    if (!isVisible(el)) continue;
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const isModalAttr = el.hasAttribute('aria-modal') || el.getAttribute('role') === 'dialog' || el.tagName === 'DIALOG';
    const isFixedTall = (st.position === 'fixed' || st.position === 'absolute') && (parseInt(st.zIndex || '0', 10) >= 10 || isModalAttr) && r.height >= 90 && r.width >= 180;
    if (isModalAttr || isFixedTall) {
      popupDetected = true;
      overlayContainers.push(el);
    }
  }
  if (!popupDetected && document.body) {
    for (const el of document.body.children) {
      if (!isVisible(el)) continue;
      const st = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (st.position === 'fixed' && parseInt(st.zIndex || '0', 10) >= 50 && r.width >= vw * 0.3 && r.height >= vh * 0.2 && el.tagName !== 'HEADER' && el.tagName !== 'NAV') {
        popupDetected = true;
        overlayContainers.push(el);
      }
    }
  }
  if (!gateReason) {
    for (const pwd of queryAllDeep('input[type="password"]')) {
      if (isVisible(pwd) && isContainedInOverlay(pwd, overlayContainers)) {
        gateReason = 'password_login_modal';
        break;
      }
    }
  }

  const interactiveSel = 'a, button, input, textarea, select, [role="button"], [role="tab"], [role="menuitem"], [role="link"], [role="checkbox"], [tabindex]';
  const closePattern = /^(?:×|✕|✖|x|关闭|暂不需要|跳过|我知道了|取消|不再提示|以后再说|close|dismiss)$/i;
  const elements = [];
  const popupCloseIds = [];
  const seenKey = new Set();

  for (const el of queryAllDeep(interactiveSel)) {
    if (elements.length >= maxElements) break;
    if (!isVisible(el)) continue;
    if (el.getAttribute('tabindex') === '-1' && !['A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) continue;

    const r = el.getBoundingClientRect();
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), vh - 1);
    const hit = deepElementFromPoint(cx, cy);
    if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) continue;

    const tag = el.tagName.toLowerCase();
    const typeAttr = (el.getAttribute('type') || '').toLowerCase();
    if (typeAttr === 'hidden') continue;
    const role = (el.getAttribute('role') || '').toLowerCase();
    const aria = cleanText(el.getAttribute('aria-label') || el.getAttribute('title') || '');
    const placeholder = cleanText(el.getAttribute('placeholder') || '');
    const text = cleanText(el.innerText || el.value || aria || placeholder || '');
    const href = tag === 'a' ? (el.getAttribute('href') || '').slice(0, 180) : '';
    const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
    const sig = tag + '|' + Math.round(r.left) + '|' + Math.round(r.top) + '|' + text.slice(0, 40);
    if (seenKey.has(sig)) continue;
    seenKey.add(sig);

    const eid = elements.length + 1;
    elements.push({
      id: eid,
      tag,
      type: typeAttr,
      role,
      text: text.slice(0, 80),
      placeholder: placeholder.slice(0, 60),
      aria: aria.slice(0, 60),
      href,
      disabled,
      rect_css: [
        Math.round(r.left * 10) / 10,
        Math.round(r.top * 10) / 10,
        Math.round(r.right * 10) / 10,
        Math.round(r.bottom * 10) / 10,
      ],
    });

    const labelTest = cleanText(text || aria);
    const inOverlay = isContainedInOverlay(el, overlayContainers);
    if ((inOverlay || popupDetected) && (closePattern.test(labelTest) || /close|关闭|dismiss|暂不需要|跳过/i.test(aria + ' ' + labelTest))) {
      popupCloseIds.push(eid);
    }
  }

  const valSig = queryAllDeep('input, textarea, select')
    .slice(0, 30)
    .map(el => (el.type === 'checkbox' || el.type === 'radio') ? (el.checked ? '1' : '0') : String(el.value || '').slice(0, 30))
    .join('|');

  return {
    url,
    title,
    scroll_y: Math.round(scrollY),
    max_scroll_y: Math.round(maxScrollY),
    viewport: [vw, vh, dpr],
    text_excerpt: bodyText.slice(0, 2500),
    dom_signature: url + '|' + elements.length + '|' + valSig + '|' + bodyText.slice(0, 220),
    popup_detected: popupDetected,
    popup_close_ids: popupCloseIds,
    auth_or_captcha_gate: gateReason,
    elements,
  };
})"""


def _encode_mcp_png(img: Image.Image, max_width: int = 1280) -> str:
    """Downscale oversized captures to max_width for faster MCP transport while keeping text crisp."""
    out = img
    if max_width > 0 and img.width > max_width:
        new_h = max(1, round(img.height * max_width / img.width))
        out = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
    buf = BytesIO()
    out.save(buf, format="PNG", compress_level=6)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _encode_mcp_image(
    img: Image.Image,
    max_width: int = 1024,
    fmt: str = "jpeg",
    quality: int = 78,
) -> tuple[str, str]:
    """Encode screenshot compactly as JPEG (default) or crisp PNG for MCP responses."""
    fmt_norm = (fmt or "jpeg").strip().lower()
    if fmt_norm == "png":
        return _encode_mcp_png(img, max_width=1280 if max_width == 1024 else max_width), "image/png"
    out = img if img.mode == "RGB" else img.convert("RGB")
    if max_width > 0 and out.width > max_width:
        new_h = max(1, round(out.height * max_width / out.width))
        out = out.resize((max_width, new_h), Image.Resampling.LANCZOS)
    buf = BytesIO()
    out.save(buf, format="JPEG", quality=int(quality), optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _compact_dom_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip internal CSS rects and empty fields to keep MCP JSON payloads small."""
    out: list[dict[str, Any]] = []
    for el in elements:
        item: dict[str, Any] = {
            "id": el.get("id"),
            "tag": el.get("tag", ""),
            "rect_norm": el.get("rect_norm"),
        }
        for key in ("text", "type", "role", "placeholder", "aria", "href"):
            val = el.get(key)
            if val:
                item[key] = val
        if el.get("disabled"):
            item["disabled"] = True
        out.append(item)
    return out


def _compact_ocr_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return compact OCR elements without redundant center_norm or empty attrs."""
    return [
        {
            "id": el.get("id"),
            "text": el.get("text", ""),
            "rect_norm": el.get("rect_norm"),
        }
        for el in elements
    ]


def _normalize_action_json(action_json: dict[str, Any] | str) -> str:
    """Ensure action_json scroll payloads include a fallback viewport coordinate."""
    if isinstance(action_json, dict):
        payload: dict[str, Any] | None = dict(action_json)
    else:
        raw = str(action_json).strip()
        try:
            obj = json.loads(raw)
            payload = obj if isinstance(obj, dict) else None
        except Exception:
            payload = None
        if payload is None:
            return raw
    if payload.get("action") == "scroll" and payload.get("coordinate") is None:
        payload["coordinate"] = [500, 550]
    return json.dumps(payload, ensure_ascii=False)


class VLMCallError(RuntimeError):
    """Raised when an invocation to the local VLM server fails after dispatch."""


_OBS_INTERNAL_KEYS = ("dom_signature", "vp_origin", "viewport", "frame_size")


def _brief_observation(obs: dict[str, Any]) -> dict[str, Any]:
    """Drop engine-internal bookkeeping and empty flags from an observe/navigate payload (detail='brief')."""
    out = {k: v for k, v in obs.items() if k not in _OBS_INTERNAL_KEYS}
    if not out.get("popup_close_ids"):
        out.pop("popup_close_ids", None)
    if "auth_or_captcha_gate" in out and out["auth_or_captcha_gate"] is None:
        del out["auth_or_captcha_gate"]
    # Token slimming: brief payloads are for the upper model's next decision, not full reading.
    for key, cap in (("text_excerpt", 1200), ("page_text", 1200)):
        val = out.get(key)
        if isinstance(val, str) and len(val) > cap:
            out[key] = val[:cap]
    els = out.get("elements")
    if isinstance(els, list):
        slimmed = []
        for el in els[:40]:
            if isinstance(el, dict):
                el = dict(el)
                for key in ("text", "aria", "placeholder", "href"):
                    val = el.get(key)
                    if isinstance(val, str) and len(val) > 48:
                        el[key] = val[:48]
            slimmed.append(el)
        out["elements"] = slimmed
    ocr = out.get("ocr_elements")
    if isinstance(ocr, list) and len(ocr) > 80:
        out["ocr_elements"] = ocr[:80]
    return out


_TRACE_DROP_IF_DEFAULT: dict[str, Any] = {
    "popup_detected": False,
    "auth_or_captcha_gate": None,
    "auto_healed": False,
    "used_zoom_crop": False,
    "infer_s": 0.0,
}


def _brief_trace_entry(entry: dict[str, Any], prev_page: tuple[Any, Any]) -> tuple[dict[str, Any], tuple[Any, Any]]:
    """Compact one run_steps trace entry for the upper model without touching status or submission evidence.

    Removed: the echoed step `input`, zero-call `local_usage` (the plan-level aggregate stays complete),
    `raw_prediction` unless it is the diagnostic for invalid_json, url/title equal to the previous entry,
    per-step `last_click_effect` duplicates, and flags sitting at their default value.
    """
    out = dict(entry)
    out.pop("input", None)
    usage = out.get("local_usage")
    if isinstance(usage, dict) and int(usage.get("calls") or 0) == 0 and usage.get("usage_complete", True):
        del out["local_usage"]
    if out.get("status") != "invalid_json":
        out.pop("raw_prediction", None)
    if out.get("kind") == "ask":
        out.pop("last_click_effect", None)
    for key, default in _TRACE_DROP_IF_DEFAULT.items():
        if key in out and out[key] == default and type(out[key]) is type(default):
            del out[key]
    page = (out.get("url", prev_page[0]), out.get("title", prev_page[1]))
    if "url" in out and out["url"] == prev_page[0]:
        del out["url"]
    if "title" in out and out["title"] == prev_page[1]:
        del out["title"]
    return out, page


def _new_local_usage() -> dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_complete": True,
        "missing_usage_calls": 0,
    }


def _is_int_metric(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _accumulate_usage(acc: dict[str, Any], usage: dict[str, Any] | None) -> None:
    """Accumulate local VLM call count and token usage while flagging missing metrics."""
    prev_calls = int(acc.get("calls") or 0)
    prev_missing = int(acc.get("missing_usage_calls") or 0)
    acc["calls"] = prev_calls + 1

    has_pt = isinstance(usage, dict) and _is_int_metric(usage.get("prompt_tokens"))
    has_ct = isinstance(usage, dict) and _is_int_metric(usage.get("completion_tokens"))
    has_tt = isinstance(usage, dict) and _is_int_metric(usage.get("total_tokens"))

    if has_pt and has_ct:
        pt = int(usage["prompt_tokens"])
        ct = int(usage["completion_tokens"])
        tt = int(usage["total_tokens"]) if has_tt else (pt + ct)
        acc["prompt_tokens"] = (int(acc["prompt_tokens"]) if _is_int_metric(acc.get("prompt_tokens")) else 0) + pt
        acc["completion_tokens"] = (
            int(acc["completion_tokens"]) if _is_int_metric(acc.get("completion_tokens")) else 0
        ) + ct
        acc["total_tokens"] = (int(acc["total_tokens"]) if _is_int_metric(acc.get("total_tokens")) else 0) + tt
        acc["missing_usage_calls"] = prev_missing
        acc["usage_complete"] = bool(acc.get("usage_complete", True)) and (prev_missing == 0)
        return

    acc["usage_complete"] = False
    acc["missing_usage_calls"] = prev_missing + 1
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        cur_valid = _is_int_metric(acc.get(k)) and (prev_calls > 0)
        add_valid = isinstance(usage, dict) and _is_int_metric(usage.get(k))
        if cur_valid or add_valid:
            cur = int(acc[k]) if cur_valid else 0
            add = int(usage[k]) if add_valid else 0
            acc[k] = cur + add
        else:
            acc[k] = None


def _merge_local_usage(agg: dict[str, Any], src: Any) -> None:
    """Merge a step/tool local_usage dict into an aggregate run_steps usage dict."""
    if not isinstance(src, dict):
        return
    src_calls = int(src.get("calls") or 0)
    if src_calls <= 0:
        return
    src_missing = int(src.get("missing_usage_calls") or 0)
    src_complete = bool(src.get("usage_complete", True))
    if not src_complete and src_missing == 0:
        src_missing = src_calls

    prev_calls = int(agg.get("calls") or 0)
    prev_missing = int(agg.get("missing_usage_calls") or 0)
    agg["calls"] = prev_calls + src_calls
    agg["missing_usage_calls"] = prev_missing + src_missing
    agg["usage_complete"] = bool(agg.get("usage_complete", True)) and src_complete and (agg["missing_usage_calls"] == 0)

    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        cur_valid = _is_int_metric(agg.get(k)) and (prev_calls > 0)
        src_valid = _is_int_metric(src.get(k))
        if cur_valid or src_valid:
            cur = int(agg[k]) if cur_valid else 0
            add = int(src[k]) if src_valid else 0
            agg[k] = cur + add
        else:
            agg[k] = None


def _classify_managed_submission(
    before_state: dict[str, Any],
    post_state: dict[str, Any],
    *,
    text_val: str,
    expect_submit: bool | None,
    wait_settle: bool,
    until_text: str | list[str] | None,
    until_matched: bool,
    timed_out: bool,
    probe_failed: bool,
    until_preexisted: bool = False,
) -> tuple[str, bool | None, str]:
    """Classify post-Enter submission state in managed Chrome without false positives."""
    if expect_submit is False:
        return "not_expected", None, "expect_submit_false"
    if until_text and not until_matched:
        if not wait_settle:
            return "unknown", None, "unverified_no_wait"
        return "not_observed", False, "until_text_timeout"
    if until_text and until_matched and not until_preexisted:
        return "observed", True, "until_text_matched"

    if post_state.get("form_submitted"):
        return "observed", True, "form_submit_event"

    before_url = str(before_state.get("url") or "")
    after_url = str(post_state.get("url") or "")
    if before_url and after_url and after_url != before_url:
        return "observed", True, "url_changed"

    before_probe_id = before_state.get("probe_id")
    if before_probe_id and post_state.get("probe_id") != before_probe_id and not probe_failed:
        return "observed", True, "page_navigated"

    before_body = str(before_state.get("body_text") or "")
    after_body = str(post_state.get("body_text") or "")
    before_norm = re.sub(r"\d+", "#", before_body)
    after_norm = re.sub(r"\d+", "#", after_body)

    if (
        before_state.get("is_multiline")
        and int(post_state.get("active_newlines") or 0) > int(before_state.get("active_newlines") or 0)
        and before_norm == after_norm
    ):
        return "not_observed", False, "multiline_newline"

    if not wait_settle:
        return "unknown", None, "unverified_no_wait"
    if probe_failed:
        return "unknown", None, "probe_failed"
    if timed_out:
        return "unknown", None, "wait_timeout"

    q = text_val.strip()
    query_reflected = bool(len(q) >= 2 and q.lower() not in before_body.lower() and q.lower() in after_body.lower())
    if query_reflected:
        # An echo can be a draft preview or suggestion; it does not prove submission.
        return "unknown", None, "query_reflected_unverified"

    el_delta = abs(int(post_state.get("el_count") or 0) - int(before_state.get("el_count") or 0))
    if before_norm == after_norm and el_delta == 0:
        if until_text and until_preexisted:
            return "not_observed", False, "until_text_preexisting_no_submit"
        return "not_observed", False, "no_change_observed"

    if until_text and until_preexisted:
        return "unknown", None, "until_text_preexisting_unverified"
    return "unknown", None, "ui_change_only_no_submit"


def _classify_desktop_submission(
    *,
    before_title: str,
    after_title: str,
    before_url: str,
    after_url: str,
    pixel_change_frac: float | None,
    expect_submit: bool | None,
    wait_settle: bool,
    until_text: str | list[str] | None,
    until_matched: bool,
    timed_out: bool,
    until_preexisted: bool = False,
) -> tuple[str, bool | None, str]:
    """Classify post-Enter submission state in desktop Chrome without pixel-noise false positives."""
    if expect_submit is False:
        return "not_expected", None, "expect_submit_false"
    if until_text and not until_matched:
        if not wait_settle:
            return "unknown", None, "unverified_no_wait"
        return "not_observed", False, "until_text_timeout"
    if until_text and until_matched and not until_preexisted:
        return "observed", True, "until_text_matched"
    if before_url and after_url and after_url != before_url:
        return "observed", True, "url_changed"
    if before_title and after_title and after_title != before_title:
        return "observed", True, "title_changed"
    if not wait_settle:
        return "unknown", None, "unverified_no_wait"
    if timed_out:
        return "unknown", None, "wait_timeout"
    if pixel_change_frac is None:
        return "unknown", None, "capture_unavailable"
    if pixel_change_frac <= 0.015:
        if until_text and until_preexisted:
            return "not_observed", False, "until_text_preexisting_no_submit"
        return "not_observed", False, "no_change_observed"
    if until_text and until_preexisted:
        return "unknown", None, "until_text_preexisting_unverified"
    return "unknown", None, "desktop_visual_change_only"


def _format_harness_name(raw_name: str) -> str:
    """Normalize incoming MCP clientInfo.name into a clean display label."""
    low = (raw_name or "").strip().lower()
    if not low:
        return "MCP Harness"
    for token, label in (
        ("antigravity", "Antigravity"),
        ("jetski", "Antigravity"),
        ("gemini", "Antigravity"),
        ("codex", "Codex"),
        ("claude", "Claude"),
        ("grok", "Grok"),
        ("cursor", "Cursor"),
    ):
        if token in low:
            return label
    return raw_name.strip()[:22]


def available_model_options(models_root: Path = PROJECT_ROOT / "models") -> list[str]:
    """Return available '<model> · <quant>' presets whose GGUF and mmproj exist locally."""
    options: list[str] = []
    for model, (directory, prefix, mmproj) in MODEL_PRESETS.items():
        for quant in ("Q4_K_M", "Q8_0"):
            if (models_root / directory / f"{prefix}{quant}.gguf").is_file() and (models_root / directory / mmproj).is_file():
                options.append(f"{model} · {quant}")
    return options


@dataclass(frozen=True)
class PolicyDecision:
    tier: str  # "green" | "red" | "takeover"
    allowed: bool
    reason: str
    matched_rule: str = ""


def evaluate_policy(
    instruction: str,
    element_meta: dict[str, Any] | None = None,
    gate_reason: str | None = None,
) -> PolicyDecision:
    """Classify an action as green (auto), red (human confirmation), or takeover (CAPTCHA/login gate)."""
    if gate_reason:
        return PolicyDecision(
            tier="takeover",
            allowed=False,
            reason=f"检测到人机验证或登录拦截（{gate_reason}），需由人工接管处理。",
            matched_rule=gate_reason,
        )
    if element_meta and str(element_meta.get("type", "")).lower() == "password":
        return PolicyDecision(
            tier="red",
            allowed=False,
            reason="目标为密码输入框，需人工确认后才能操作。",
            matched_rule="password_input",
        )
    combined = " ".join(
        str(x or "")
        for x in (
            instruction,
            (element_meta or {}).get("text"),
            (element_meta or {}).get("aria"),
            (element_meta or {}).get("placeholder"),
        )
    )
    low = combined.lower()
    for pat in RED_TIER_PATTERNS:
        if pat in low:
            return PolicyDecision(
                tier="red",
                allowed=False,
                reason=f"触发高风险保护词「{pat}」，执行前需人工确认。",
                matched_rule=f"keyword:{pat}",
            )
    m = RED_TIER_WORD_RE.search(combined)
    if m:
        return PolicyDecision(
            tier="red",
            allowed=False,
            reason=f"触发高风险保护词「{m.group(0)}」，执行前需人工确认。",
            matched_rule=f"keyword:{m.group(0)}",
        )
    return PolicyDecision(tier="green", allowed=True, reason="green_auto")


def append_audit(record: dict[str, Any], audit_path: Path = PROJECT_ROOT / "artifacts" / "cua_audit.jsonl") -> None:
    """Append one compact JSONL audit event without failing execution on I/O errors."""
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


class M35Grounder:
    """Warm llama-server wrapper for M3.5 / M3 local GGUF inference plus zoom-in refinement."""

    def __init__(self, port: int = 18086, max_image_tokens: int = 1024, prompt_style: str = "m1") -> None:
        self.port = port
        self.max_image_tokens = max_image_tokens
        self.prompt_style = prompt_style
        self.proc: Optional[subprocess.Popen[str]] = None
        self.base_url: str = f"http://127.0.0.1:{port}"
        self.model_label: str = ""
        self._lock = threading.Lock()
        self._shutdown = False
        self._resolve_default_preset()

    def _resolve_default_preset(self) -> None:
        opts = available_model_options()
        if opts:
            self.model_label = opts[0]
        else:
            d, _, _ = default_local_model()
            self.model_label = f"{d.name} · Q4_K_M"

    def _paths_for_label(self, label: str) -> tuple[Path, str, str, str]:
        if " · " in label:
            model_key, quant = label.split(" · ", 1)
            if model_key in MODEL_PRESETS:
                directory, prefix, mmproj = MODEL_PRESETS[model_key]
                return PROJECT_ROOT / "models" / directory, prefix, f"{prefix}{quant}.gguf", mmproj
        d, prefix, mmproj = default_local_model()
        return d, prefix, f"{prefix}Q4_K_M.gguf", mmproj

    def set_model(self, model_label: str) -> None:
        with self._lock:
            if model_label != self.model_label:
                self.stop()
                self.model_label = model_label

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # Engine calls are serialized and every screenshot differs, so extra slots and the host-RAM prompt
    # cache only cost memory (A/B: artifacts/perf_v14/llama_flags_ab.json, identical outputs).
    SERVER_EXTRA_ARGS: tuple[str, ...] = ("-np", "1", "--cache-ram", "0")

    def ensure_started(self) -> float:
        with self._lock:
            if self.is_running():
                return 0.0
            if self._shutdown:
                raise RuntimeError("grounder_shut_down")
            model_dir, _prefix, model_file, mmproj_file = self._paths_for_label(self.model_label)
            args_ns = type(
                "ServerArgs",
                (),
                {
                    "port": self.port,
                    "max_image_tokens": self.max_image_tokens,
                    "min_image_tokens": None,
                    "model_dir": model_dir,
                    "model_prefix": _prefix,
                    "quant": model_file.replace(_prefix, "").replace(".gguf", ""),
                    "mmproj_name": mmproj_file,
                    "server_extra_args": list(self.SERVER_EXTRA_ARGS),
                },
            )()
            try:
                load_s = self._launch_and_wait(args_ns)
                return load_s
            except RuntimeError:
                if self._shutdown:
                    raise
                # Re-pick an ephemeral localhost port if default port is already held
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    self.port = int(sock.getsockname()[1])
                args_ns.port = self.port
                return self._launch_and_wait(args_ns)

    def _launch_and_wait(self, args_ns: Any) -> float:
        self.proc, self.base_url = start_llama_server(args_ns)
        if self._shutdown:
            # close() raced a background prewarm: never leave an orphaned llama-server behind
            self.stop()
            raise RuntimeError("grounder_shut_down")
        return wait_for_server(self.base_url, self.proc, timeout_s=90.0)

    def predict(self, raw_img: Image.Image, instruction: str) -> tuple[str, Optional[ParsedAction], float, dict[str, Any]]:
        self.ensure_started()
        resized = resize_to_token_budget(raw_img, max_tokens=self.max_image_tokens)
        img_b64 = encode_image_b64(resized)
        sys_prompt = M1_SYSTEM_PROMPT if self.prompt_style == "m1" else SYSTEM_PROMPT
        t0 = time.perf_counter()
        try:
            raw_text, usage = call_vlm(self.base_url, img_b64, instruction, max_tokens=96, system_prompt=sys_prompt)
        except Exception as exc:
            raise VLMCallError(f"vlm_call_failed: {exc}") from exc
        infer_s = time.perf_counter() - t0
        try:
            parsed = parse_and_validate_action(raw_text)
        except ActionValidationError:
            parsed = None
        return raw_text, parsed, infer_s, usage

    def predict_zoomed(
        self,
        raw_img: Image.Image,
        instruction: str,
        initial_coord: tuple[int, int],
        crop_w: int = 420,
        crop_h: int = 280,
    ) -> tuple[str, Optional[ParsedAction], float, dict[str, Any]]:
        """Re-ground an ambiguous coordinate inside a 2x upscaled local crop and map back to [0, 1000]."""
        w_full, h_full = raw_img.size
        cx = round(initial_coord[0] / 1000.0 * max(1, w_full - 1))
        cy = round(initial_coord[1] / 1000.0 * max(1, h_full - 1))
        pw = min(crop_w, w_full)
        ph = min(crop_h, h_full)
        left = max(0, min(cx - pw // 2, w_full - pw))
        top = max(0, min(cy - ph // 2, h_full - ph))
        patch = raw_img.crop((left, top, left + pw, top + ph))
        upscaled = patch.resize((max(32, pw * 2), max(32, ph * 2)), Image.Resampling.BICUBIC)
        raw_text, parsed, infer_s, usage = self.predict(upscaled, instruction)
        if parsed is not None and parsed.coordinate is not None:
            zx, zy = parsed.coordinate
            px = min(max(left + round(zx / 1000.0 * max(1, pw - 1)), 0), w_full - 1)
            py = min(max(top + round(zy / 1000.0 * max(1, ph - 1)), 0), h_full - 1)
            full_nx = min(max(round(px / max(1, w_full - 1) * 1000.0), 0), 1000)
            full_ny = min(max(round(py / max(1, h_full - 1) * 1000.0), 0), 1000)
            remapped = ParsedAction(
                action=parsed.action,
                coordinate=(full_nx, full_ny),
                direction=parsed.direction,
                amount=parsed.amount,
                text=parsed.text,
                keys=parsed.keys,
                seconds=parsed.seconds,
                reason=parsed.reason,
                raw=parsed.raw,
            )
            return raw_text, remapped, infer_s, usage
        return raw_text, parsed, infer_s, usage

    _SCREEN_READER_PROMPT = (
        "You are an accurate visual screen reader. Answer the user's question directly and concisely "
        "based on the browser screenshot and OCR excerpt. Reply in plain Chinese or English text ONLY "
        "(do NOT output action JSON)."
    )

    def ask_screen(
        self,
        raw_img: Image.Image,
        question: str,
        ocr_context: str = "",
        crop_center: tuple[int, int] | None = None,
        max_tokens: int = 220,
    ) -> tuple[str, float, dict[str, Any]]:
        """Answer a factual question about the current screen, optionally zooming around crop_center."""
        self.ensure_started()
        if crop_center is not None and raw_img.width > 64 and raw_img.height > 64:
            w_full, h_full = raw_img.size
            cx = round(crop_center[0] / 1000.0 * max(1, w_full - 1))
            cy = round(crop_center[1] / 1000.0 * max(1, h_full - 1))
            pw = min(320, w_full)
            ph = min(160, h_full)
            left = max(0, min(cx - pw // 2, w_full - pw))
            top = max(0, min(cy - ph // 2, h_full - ph))
            patch = raw_img.crop((left, top, left + pw, top + ph))
            work_img = patch.resize((max(32, pw * 2), max(32, ph * 2)), Image.Resampling.BICUBIC)
        else:
            work_img = resize_to_token_budget(raw_img, max_tokens=self.max_image_tokens)

        img_b64 = encode_image_b64(work_img)
        prompt = question.strip()
        ctx = (ocr_context or "").strip()[:1000]
        if ctx:
            prompt = f"问题：{prompt}\n\n屏幕文字参考：\n{ctx}"
        t0 = time.perf_counter()
        try:
            answer, usage = call_vlm(
                self.base_url,
                img_b64,
                prompt,
                max_tokens=max_tokens,
                system_prompt=self._SCREEN_READER_PROMPT,
            )
        except Exception as exc:
            raise VLMCallError(f"vlm_call_failed: {exc}") from exc
        infer_s = time.perf_counter() - t0
        return answer.strip(), infer_s, usage

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            stop_llama_server(proc)

    def shutdown(self) -> None:
        """Final stop: also cancels an in-flight background load so no server outlives the engine."""
        self._shutdown = True
        self.stop()
        if self._lock.acquire(timeout=5.0):
            try:
                self.stop()
            finally:
                self._lock.release()


class ManagedChromeSession:
    """Isolated persistent background Chrome controlled via PrintWindow + CDP + optional UIA."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        chrome_exe: Path = DEFAULT_CHROME,
        win_size: tuple[int, int] = (1366, 840),
    ) -> None:
        self.profile_dir = profile_dir or (PROJECT_ROOT / "profiles" / "cua_chrome")
        self.chrome_exe = chrome_exe
        self.win_size = win_size
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.hwnd: int = 0
        self.cdp_port: int = 0
        self.cdp: Optional[CDPClient] = None
        self.active_target_id: str = ""
        self._uia: Optional[UIAHelper] = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.hwnd != 0 and bool(win32gui.IsWindow(self.hwnd))

    def _list_targets(self) -> list[dict[str, Any]]:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=5).read()
        return [t for t in json.loads(raw) if t.get("type") == "page"]

    def _connect_target(self, target_id: str | None = None) -> None:
        targets = self._list_targets()
        if not targets:
            raise RuntimeError("No active page tab found in managed Chrome")
        chosen = next((t for t in targets if t["id"] == target_id), None) if target_id else targets[0]
        if chosen is None:
            chosen = targets[0]
        if self.cdp is not None:
            self.cdp.close()
        self.cdp = CDPClient(chosen["webSocketDebuggerUrl"])
        self.active_target_id = chosen["id"]
        self.cdp.send("Page.enable")
        self.cdp.send("Runtime.enable")

    def ensure_started(self) -> None:
        if self.is_running() and self.cdp is not None:
            try:
                self.cdp.evaluate("1")
                return
            except Exception:
                try:
                    self._connect_target(self.active_target_id)
                    return
                except Exception:
                    pass
        self.close()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        start_html = urllib.parse.quote(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Chrome Pilot · 就绪</title>"
            "<body style='margin:0;background:#11161f;color:#e2e8f0;font-family:\"Microsoft YaHei UI\",sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<div style='text-align:center'><h2 style='margin:0 0 8px;font-weight:600'>Chrome Pilot 托管浏览区</h2>"
            "<p style='margin:0;color:#94a3b8;font-size:14px'>后台独立会话已就绪，可通过悬浮指令窗或 MCP 接入操作。</p></div></body></html>"
        )
        self.proc, self.hwnd, self.cdp_port = launch_chrome_session(
            self.chrome_exe,
            self.profile_dir,
            self.win_size,
            scale=1.0,
            dark_mode=False,
            start_url=f"data:text/html;charset=utf-8,{start_html}",
        )
        time.sleep(0.7)
        self._connect_target()

    def _get_uia(self) -> UIAHelper:
        if self._uia is None:
            self._uia = UIAHelper()
        return self._uia

    def capture_full_window(self) -> tuple[ChromeWindowInfo, Image.Image]:
        self.ensure_started()
        info = inspect_chrome_window(self.hwnd)
        img = print_window_dwm(info)
        return info, img

    def window_geometry(self) -> tuple[ChromeWindowInfo, tuple[int, int]]:
        """Window info + frame size (== capture_full_window().size) without paying for a PrintWindow capture."""
        self.ensure_started()
        info = inspect_chrome_window(self.hwnd)
        return info, (info.dwm_rect.right - info.dwm_rect.left, info.dwm_rect.bottom - info.dwm_rect.top)

    def extract_dom_snapshot(self, info: ChromeWindowInfo, w_dwm: int, h_dwm: int, max_elements: int = 60) -> dict[str, Any]:
        assert self.cdp is not None
        snap = self.cdp.evaluate(f"({JS_EXTRACT_DOM})({int(max_elements)})", timeout=10.0)
        vw, vh, dpr = [float(x) for x in snap.get("viewport", [w_dwm, h_dwm, 1.0])]
        vp_left = info.client_rect.left - info.dwm_rect.left
        vp_top = (info.client_rect.bottom - info.dwm_rect.top) - round(vh * dpr)
        for el in snap.get("elements", []):
            l, t, r, b = el["rect_css"]
            px_box = [
                vp_left + round(l * dpr),
                vp_top + round(t * dpr),
                vp_left + round(r * dpr),
                vp_top + round(b * dpr),
            ]
            el["rect_norm"] = [
                min(max(round(px_box[0] / max(1, w_dwm - 1) * 1000.0), 0), 1000),
                min(max(round(px_box[1] / max(1, h_dwm - 1) * 1000.0), 0), 1000),
                min(max(round(px_box[2] / max(1, w_dwm - 1) * 1000.0), 0), 1000),
                min(max(round(px_box[3] / max(1, h_dwm - 1) * 1000.0), 0), 1000),
            ]
        snap["vp_origin"] = [vp_left, vp_top]
        snap["frame_size"] = [w_dwm, h_dwm]
        return snap

    def extract_viewport_text(self, max_chars: int = 4000) -> str:
        """Extract visible text currently intersecting the viewport (including open shadow DOM)."""
        self.ensure_started()
        assert self.cdp is not None
        js = r"""((maxChars) => {
  const vw = window.innerWidth || 0;
  const vh = window.innerHeight || 0;
  if (vw <= 0 || vh <= 0) return '';
  const allRoots = [document];
  const seenRoots = new Set([document]);
  function collectRoots(root, depth = 0) {
    if (!root || depth > 8 || !root.querySelectorAll) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot && !seenRoots.has(el.shadowRoot)) {
        seenRoots.add(el.shadowRoot);
        allRoots.push(el.shadowRoot);
        collectRoots(el.shadowRoot, depth + 1);
      }
    }
  }
  collectRoots(document, 0);

  function elHidden(el) {
    let cur = el;
    let steps = 0;
    while (cur && steps < 16) {
      if (cur.nodeType === 1) {
        const tag = cur.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return true;
        const st = getComputedStyle(cur);
        if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') <= 0.05) return true;
      }
      cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
      steps++;
    }
    return false;
  }

  const parts = [];
  let total = 0;
  for (const root of allRoots) {
    const startNode = (root === document) ? (document.body || document.documentElement) : root;
    if (!startNode) continue;
    const walker = document.createTreeWalker(startNode, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const txt = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
      if (!txt) continue;
      const parent = node.parentElement;
      if (parent && elHidden(parent)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      let visibleInVp = false;
      for (const r of range.getClientRects()) {
        if (r.width >= 1 && r.height >= 1 && r.right > 0 && r.left < vw && r.bottom > 0 && r.top < vh) {
          visibleInVp = true;
          break;
        }
      }
      if (visibleInVp) {
        parts.push(txt);
        total += txt.length + 1;
        if (total >= maxChars) break;
      }
    }
    if (total >= maxChars) break;
  }
  return parts.join(' ').slice(0, maxChars);
})"""
        res = self.cdp.evaluate(f"({js})({int(max_chars)})")
        return str(res or "")

    def navigate(self, url: str, timeout_s: float = 15.0) -> dict[str, Any]:
        self.ensure_started()
        assert self.cdp is not None
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Only http/https URLs are supported, got: {url!r}")
        self.cdp.send("Page.navigate", {"url": url}, timeout=timeout_s)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = self.cdp.evaluate("({r: document.readyState, l: document.body ? (document.body.innerText||'').trim().length : 0})")
                if isinstance(state, dict) and state.get("r") == "complete" and state.get("l", 0) >= 1:
                    time.sleep(0.25)
                    break
            except Exception:
                pass
            time.sleep(0.2)
        return self.cdp.evaluate("({url: location.href, title: document.title})")

    def list_tabs(self) -> list[dict[str, Any]]:
        self.ensure_started()
        return [
            {
                "index": i,
                "id": t["id"],
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "active": t["id"] == self.active_target_id,
            }
            for i, t in enumerate(self._list_targets())
        ]

    def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        self.ensure_started()
        assert self.cdp is not None
        if url != "about:blank" and urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError(f"Invalid URL scheme: {url!r}")
        res = self.cdp.send("Target.createTarget", {"url": url})
        tid = res["targetId"]
        urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/activate/{tid}", timeout=5).read()
        time.sleep(0.4)
        self._connect_target(tid)
        return {"tab_id": tid, "tabs": self.list_tabs()}

    @staticmethod
    def _resolve_target(tabs: list[dict[str, Any]], tab_ref: str | int) -> dict[str, Any]:
        target = None
        if isinstance(tab_ref, int) or (isinstance(tab_ref, str) and tab_ref.isdigit()):
            idx = int(tab_ref)
            if 0 <= idx < len(tabs):
                target = tabs[idx]
        if target is None:
            ref_s = str(tab_ref).lower()
            target = next(
                (t for t in tabs if t["id"] == tab_ref or ref_s in t.get("title", "").lower() or ref_s in t.get("url", "").lower()),
                None,
            )
        if target is None:
            raise ValueError(f"Tab not found: {tab_ref!r}")
        return target

    def switch_tab(self, tab_ref: str | int) -> dict[str, Any]:
        self.ensure_started()
        tabs = self._list_targets()
        target = self._resolve_target(tabs, tab_ref)
        urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/activate/{target['id']}", timeout=5).read()
        time.sleep(0.25)
        self._connect_target(target["id"])
        return {"active_tab": target["id"], "title": target.get("title", ""), "url": target.get("url", "")}

    def close_tab(self, tab_ref: str | int | None = None) -> dict[str, Any]:
        self.ensure_started()
        assert self.cdp is not None
        tabs = self._list_targets()
        if len(tabs) <= 1:
            raise ValueError("Cannot close the last remaining tab")
        tid = self.active_target_id
        if tab_ref is not None:
            tid = self._resolve_target(tabs, tab_ref)["id"]
        self.cdp.send("Target.closeTarget", {"targetId": tid})
        time.sleep(0.25)
        self._connect_target()
        return {"closed": tid, "tabs": self.list_tabs()}

    def dispatch_viewport_action(
        self,
        parsed: ParsedAction,
        info: ChromeWindowInfo,
        frame_size: tuple[int, int],
    ) -> tuple[bool, str]:
        assert self.cdp is not None
        if parsed.action == "done":
            return True, "done"
        if parsed.action == "not_found":
            return True, "safe_refusal_no_input"
        if parsed.action == "wait":
            time.sleep(min(parsed.seconds or 1.0, 5.0))
            return True, "waited"
        if parsed.action == "hotkey":
            self.send_key(list(parsed.keys or ()))
            return True, f"hotkey:{'+'.join(parsed.keys or ())}"

        w_dwm, h_dwm = frame_size
        vp_metrics = self.cdp.evaluate("[window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]")
        inner_w, inner_h, dpr = float(vp_metrics[0]), float(vp_metrics[1]), float(vp_metrics[2])
        vp_left = info.client_rect.left - info.dwm_rect.left
        vp_top = (info.client_rect.bottom - info.dwm_rect.top) - round(inner_h * dpr)

        if parsed.coordinate is not None:
            px = min(max(round(parsed.coordinate[0] / 1000.0 * (w_dwm - 1)), 0), w_dwm - 1)
            py = min(max(round(parsed.coordinate[1] / 1000.0 * (h_dwm - 1)), 0), h_dwm - 1)
        else:
            px, py = round(vp_left + inner_w * dpr / 2), round(vp_top + inner_h * dpr / 2)

        # Native top Chrome strip handling via UIA hit verification
        if parsed.action == "left_click" and py < vp_top:
            raw_uia = self._get_uia().query(self.hwnd, info.dwm_rect.top + vp_top + 8)
            uia_info = classify_uia_controls(raw_uia, info, vp_top, allowed_bookmarks=None)
            for key, action_fn in (
                ("back", lambda: self.cdp.evaluate("history.back()")),
                ("reload", lambda: self.cdp.send("Page.reload")),
                ("new_tab", lambda: self.new_tab("about:blank")),
            ):
                ctrl = uia_info["controls"].get(key)
                if ctrl and ctrl["box_px"][0] <= px <= ctrl["box_px"][2] and ctrl["box_px"][1] <= py <= ctrl["box_px"][3]:
                    action_fn()
                    return True, f"cdp_native:{key}"
            for t in uia_info["tabs"]:
                b = t["box_px"]
                if b[0] <= px <= b[2] and b[1] <= py <= b[3]:
                    clean = t["title"].split(" - Google Chrome")[0].strip()
                    self.switch_tab(clean[:20])
                    return True, f"cdp_native_tab:{clean[:24]}"
            return False, f"outside_viewport_chrome_strip:({px},{py})"

        css_x = (px - vp_left) / dpr
        css_y = (py - vp_top) / dpr
        if not (0.0 <= css_x <= inner_w and 0.0 <= css_y <= inner_h):
            return False, f"outside_viewport:css=({css_x:.1f},{css_y:.1f})"

        if parsed.action in {"left_click", "double_click", "right_click", "move"}:
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": css_x, "y": css_y, "button": "none"})
            if parsed.action == "move":
                return True, "cdp_executed:move"
            btn = "right" if parsed.action == "right_click" else "left"
            clicks = 2 if parsed.action == "double_click" else 1
            for c in range(1, clicks + 1):
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": css_x, "y": css_y, "button": btn, "buttons": 1 if btn == "left" else 2, "clickCount": c})
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": css_x, "y": css_y, "button": btn, "buttons": 0, "clickCount": c})
            return True, f"cdp_executed:{parsed.action}"

        if parsed.action == "type":
            if parsed.coordinate is not None:
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": css_x, "y": css_y, "button": "none"})
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": css_x, "y": css_y, "button": "left", "buttons": 1, "clickCount": 1})
                self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": css_x, "y": css_y, "button": "left", "buttons": 0, "clickCount": 1})
                time.sleep(0.03)
            self.cdp.send("Input.insertText", {"text": parsed.text or ""})
            return True, "cdp_executed:type"

        if parsed.action == "scroll":
            ticks = max(1, int(parsed.amount or 3))
            direction = parsed.direction or "down"
            step_x = 120.0 if direction == "right" else (-120.0 if direction == "left" else 0.0)
            step_y = 120.0 if direction == "down" else (-120.0 if direction == "up" else 0.0)

            def _scroll_state() -> tuple[float, bool]:
                try:
                    st = self.cdp.evaluate(
                        "({y:window.scrollY||document.documentElement.scrollTop||0,max:Math.max(0,(document.documentElement.scrollHeight||0)-window.innerHeight),x:window.scrollX||0})"
                    )
                    if isinstance(st, dict):
                        return float(st.get("y", 0.0)), bool(st.get("max", 0) > 4)
                except Exception:
                    pass
                return 0.0, True

            before_y, scrollable = _scroll_state()
            # real-input path: hover first, then per-notch wheel ticks like a physical wheel
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": css_x, "y": css_y, "button": "none"})
            time.sleep(0.02)
            for _ in range(ticks):
                self.cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": css_x, "y": css_y,
                    "deltaX": step_x, "deltaY": step_y, "buttons": 0,
                })
                time.sleep(0.03)
            time.sleep(0.12)
            after_y, _ = _scroll_state()
            total_dy = step_y * ticks
            if abs(after_y - before_y) > 2 or not scrollable or step_y == 0:
                return True, "cdp_executed:scroll"
            # fallback 1: wheel at viewport centre (target element may swallow wheel events)
            try:
                dims = self.cdp.evaluate("({w:window.innerWidth,h:window.innerHeight})")
                if isinstance(dims, dict) and dims.get("w") and dims.get("h"):
                    cx2, cy2 = float(dims["w"]) / 2.0, float(dims["h"]) / 2.0
                    self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx2, "y": cy2, "button": "none"})
                    time.sleep(0.02)
                    for _ in range(ticks):
                        self.cdp.send("Input.dispatchMouseEvent", {
                            "type": "mouseWheel", "x": cx2, "y": cy2,
                            "deltaX": step_x, "deltaY": step_y, "buttons": 0,
                        })
                        time.sleep(0.03)
                    time.sleep(0.12)
                    after_y, _ = _scroll_state()
                    if abs(after_y - before_y) > 2:
                        return True, "cdp_executed:scroll_center_retry"
            except Exception:
                pass
            # fallback 2: direct DOM scroll so the action is never a silent no-op
            try:
                self.cdp.evaluate(f"window.scrollBy({step_x * ticks:.0f},{total_dy:.0f});'ok'")
                return True, "cdp_executed:scroll_js_fallback"
            except Exception:
                return True, "cdp_executed:scroll_no_effect"

        return False, f"unsupported_action:{parsed.action}"

    def click_dom_element(self, rect_css: list[float], text_to_type: str | None = None) -> tuple[bool, str]:
        assert self.cdp is not None
        cx = (rect_css[0] + rect_css[2]) / 2.0
        cy = (rect_css[1] + rect_css[3]) / 2.0
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"})
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "buttons": 1, "clickCount": 1})
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "buttons": 0, "clickCount": 1})
        if text_to_type is not None:
            time.sleep(0.03)
            self.cdp.send("Input.insertText", {"text": text_to_type})
            return True, "dom_element_type"
        return True, "dom_element_click"

    def send_key(self, keys: list[str]) -> None:
        assert self.cdp is not None
        norm = [k.strip().lower() for k in keys if k.strip()]
        mods = 0
        main_key = norm[-1] if norm else "enter"
        for k in norm[:-1]:
            if k in {"ctrl", "control"}:
                mods |= 2
            elif k == "alt":
                mods |= 1
            elif k == "shift":
                mods |= 8
        key_defs = {
            "enter": ("Enter", "Enter", 13),
            "return": ("Enter", "Enter", 13),
            "escape": ("Escape", "Escape", 27),
            "esc": ("Escape", "Escape", 27),
            "tab": ("Tab", "Tab", 9),
            "backspace": ("Backspace", "Backspace", 8),
        }
        if main_key in key_defs:
            key_name, code, vk = key_defs[main_key]
        elif len(main_key) == 1 and main_key.isalpha():
            key_name = main_key.upper() if (mods & 8) else main_key
            code = f"Key{main_key.upper()}"
            vk = ord(main_key.upper())
        else:
            key_name = main_key.upper() if len(main_key) == 1 else main_key
            code = ""
            vk = ord(main_key.upper()) if len(main_key) == 1 else 0

        down_ev: dict[str, Any] = {
            "type": "rawKeyDown",
            "key": key_name,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": mods,
        }
        if code:
            down_ev["code"] = code
        if (mods & 2) and main_key == "a":
            down_ev["commands"] = ["selectAll"]
        if main_key in {"enter", "return"}:
            down_ev["text"] = "\r"
            down_ev["unmodifiedText"] = "\r"

        self.cdp.send("Input.dispatchKeyEvent", down_ev)
        if main_key in {"enter", "return"}:
            self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "char",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    "text": "\r",
                    "unmodifiedText": "\r",
                    "modifiers": mods,
                },
            )
        up_ev: dict[str, Any] = {
            "type": "keyUp",
            "key": key_name,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": mods,
        }
        if code:
            up_ev["code"] = code
        self.cdp.send("Input.dispatchKeyEvent", up_ev)

    def close(self) -> None:
        if self._uia is not None:
            self._uia.close()
            self._uia = None
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.hwnd = 0


def _draw_som_badges(raw_img: Image.Image, elements: list[dict[str, Any]], max_badges: int = 40) -> Image.Image:
    vis = raw_img.copy()
    draw = ImageDraw.Draw(vis)
    w, h = vis.size
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 12) if Path(r"C:\Windows\Fonts\msyh.ttc").is_file() else ImageFont.load_default()
    for el in elements[:max_badges]:
        x1, y1, x2, y2 = el["rect_norm"]
        px1 = round(x1 / 1000.0 * (w - 1))
        py1 = round(y1 / 1000.0 * (h - 1))
        px2 = round(x2 / 1000.0 * (w - 1))
        py2 = round(y2 / 1000.0 * (h - 1))
        draw.rectangle([px1, py1, px2, py2], outline=(59, 130, 246), width=1)
        tag = str(el["id"])
        bx2 = min(w - 1, px1 + 10 + 7 * len(tag))
        by2 = min(h - 1, py1 + 16)
        draw.rectangle([px1, py1, bx2, by2], fill=(37, 99, 235))
        draw.text((px1 + 3, py1 + 1), tag, fill=(255, 255, 255), font=font)
    return vis


def _frame_changed_fraction(img_a: Image.Image, img_b: Image.Image) -> float:
    if img_a.size != img_b.size:
        return 1.0
    diff = np.abs(np.asarray(img_a, dtype=np.int16) - np.asarray(img_b, dtype=np.int16))
    return float(np.mean(np.max(diff, axis=2) >= 12))


class ChromeCUAEngine:
    """Thread-safe CUA orchestrator serving both MCP tools and the floating desktop panel."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        vlm_port: int = 18086,
        mcp_port: int = 18390,
        status_listener: Optional[Callable[[dict[str, Any]], None]] = None,
        target_mode: str = "managed",
        desktop_hwnd: int = 0,
        desktop_hide_hook: Optional[Callable[[bool], None]] = None,
    ) -> None:
        if target_mode not in {"managed", "desktop", "os"}:
            raise ValueError(f"Unsupported target_mode: {target_mode!r}")
        enable_per_monitor_dpi_awareness()
        self.chrome = ManagedChromeSession(profile_dir=profile_dir)
        self.grounder = M35Grounder(port=vlm_port)
        self.mcp_port = mcp_port
        self.status_listener = status_listener
        self.target_mode = target_mode
        self.desktop_hwnd = int(desktop_hwnd or 0)
        self.panel_hwnd: int = 0
        self.desktop_hide_hook = desktop_hide_hook
        self._desktop_uia: Optional[UIAHelper] = None
        self._desktop_ocr = PersistentWinRTOCR()
        self._desktop_url_reader = DesktopChromeURLReader()
        self._last_ocr_elements: list[dict[str, Any]] = []
        self._last_click_coord: tuple[int, int] | None = None
        self._last_click_effect: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self.abort_event = threading.Event()
        self._pending_confirms: dict[str, tuple[float, str]] = {}
        self._mcp_sessions: dict[str, dict[str, Any]] = {}
        self.active_harness: str = ""
        self.task_id: str = ""
        self.task_title: str = ""
        self.task_state: str = "idle"  # "idle" | "running" | "done" | "confirm" | "error"
        self.task_explicit: bool = False
        self.task_step: int = 0
        self.task_total_steps: int = 0
        self.task_started_at: float = 0.0
        self.task_last_activity: float = 0.0
        self.task_step_label: str = ""
        self.recent_steps: list[str] = []
        self.step_count = 0
        self.mcp_clients = 0
        self.state = "idle"
        self.last_message = "就绪"
        self.active_url = ""
        self.active_title = ""
        self._prewarm_started = False
        self._vram_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._status_heavy: dict[str, Any] = {"wins": [], "tabs_count": 0}

    VRAM_TTL_S = 10.0

    @staticmethod
    def prewarm_enabled() -> bool:
        """Opt-in switch: off unless CUA_PREWARM is 1/true/on/yes, so DOM-only sessions never hold the model's VRAM."""
        return os.environ.get("CUA_PREWARM", "0").strip().lower() in {"1", "true", "on", "yes"}

    def prewarm_async(self) -> bool:
        """With CUA_PREWARM=1, load the local VLM on a daemon thread so the first visual call skips the cold start.

        Off by default: the model then loads lazily on the first call that needs vision.
        """
        if self._prewarm_started or self.grounder.is_running() or not self.prewarm_enabled():
            return False
        self._prewarm_started = True

        def _load() -> None:
            try:
                self.grounder.ensure_started()
            except Exception:
                pass

        threading.Thread(target=_load, daemon=True, name="cua-vlm-prewarm").start()
        return True

    def _emit_status(
        self,
        state: str,
        message: str,
        event_kind: str = "status",
        play_sound: bool = False,
    ) -> None:
        self.state = state
        self.last_message = message
        if self.status_listener is not None:
            try:
                payload = self.get_status(light=True)
                payload["event_kind"] = event_kind
                payload["play_sound"] = play_sound
                self.status_listener(payload)
            except Exception:
                pass

    def request_abort(self) -> None:
        self.abort_event.set()
        self.task_state = "error"
        self.task_step_label = "已请求急停"
        self._emit_status("stopped", "已请求急停中断当前动作", event_kind="task_error", play_sound=True)

    def reset_abort(self) -> None:
        self.abort_event.clear()

    def register_mcp_session(self, session_id: str, client_name: str = "", client_version: str = "") -> dict[str, Any]:
        """Register an active MCP harness connection and announce takeover cleanly."""
        with self._lock:
            display = _format_harness_name(client_name)
            self._mcp_sessions[session_id] = {
                "name": display,
                "raw_name": client_name,
                "version": client_version,
                "connected_at": time.monotonic(),
            }
            self.mcp_clients = len(self._mcp_sessions)
            self.active_harness = display
            msg = f"已接入 {display}" if self.mcp_clients == 1 else f"{self.mcp_clients} 个接入 · 主控 {display}"
            self._emit_status(self.state, msg, event_kind="harness_connected", play_sound=True)
            return self.get_status()

    def mark_mcp_activity(self, session_id: str | None, tool_name: str) -> None:
        """Keep track of which MCP harness invoked the latest tool."""
        with self._lock:
            if session_id and session_id in self._mcp_sessions:
                self.active_harness = self._mcp_sessions[session_id]["name"]
            elif not self.active_harness and self._mcp_sessions:
                last_meta = list(self._mcp_sessions.values())[-1]
                self.active_harness = str(last_meta["name"])
            self.task_last_activity = time.monotonic()

    def unregister_mcp_session(self, session_id: str) -> None:
        """Remove a closing MCP harness session and update the active display."""
        with self._lock:
            if session_id in self._mcp_sessions:
                self._mcp_sessions.pop(session_id, None)
            self.mcp_clients = len(self._mcp_sessions)
            if self._mcp_sessions:
                last_meta = list(self._mcp_sessions.values())[-1]
                self.active_harness = str(last_meta["name"])
                msg = f"当前接入：{self.active_harness} ({self.mcp_clients})"
            else:
                self.active_harness = ""
                msg = "MCP 连接已断开"
            self._emit_status(self.state, msg, event_kind="harness_disconnected", play_sound=False)

    def register_mcp_client(self, delta: int) -> None:
        with self._lock:
            if delta > 0:
                for _ in range(delta):
                    self.register_mcp_session(f"legacy_{secrets.token_hex(4)}", "MCP Harness", "")
            elif delta < 0:
                for _ in range(-delta):
                    legacy_keys = [k for k in self._mcp_sessions if k.startswith("legacy_")]
                    if legacy_keys:
                        self.unregister_mcp_session(legacy_keys[-1])
                    elif self._mcp_sessions:
                        self.unregister_mcp_session(list(self._mcp_sessions.keys())[-1])

    def _begin_auto_step(self, label: str) -> None:
        now = time.monotonic()
        if self.task_state != "running" or (self.task_last_activity and (now - self.task_last_activity) > 60.0):
            self.task_id = secrets.token_hex(4)
            self.task_title = ""
            self.task_state = "running"
            self.task_explicit = False
            self.task_step = 0
            self.task_total_steps = 0
            self.task_started_at = now
            self.recent_steps = []
        self.task_step += 1
        self.task_last_activity = now
        self.task_step_label = label.strip()[:60]
        self.recent_steps = (self.recent_steps + [self.task_step_label])[-5:]
        self._emit_status("running", self.task_step_label, event_kind="step_running", play_sound=False)

    def update_task(
        self,
        event: str,
        title: str | None = None,
        message: str | None = None,
        step: int | None = None,
        total_steps: int | None = None,
        play_sound: bool = True,
    ) -> dict[str, Any]:
        """Explicitly start, update, complete, or fail an orchestrated task from an upper harness."""
        if event not in {"start", "progress", "done", "error"}:
            raise ValueError(f"Unsupported task event: {event!r}")
        with self._lock:
            now = time.monotonic()
            if event == "start":
                self.task_id = secrets.token_hex(4)
                self.task_title = (title or "执行任务").strip()
                self.task_state = "running"
                self.task_explicit = True
                self.task_step = int(step) if step is not None else 0
                self.task_total_steps = int(total_steps) if total_steps is not None else 0
                self.task_started_at = now
                self.task_last_activity = now
                self.task_step_label = (message or self.task_title).strip()[:60]
                self.recent_steps = []
                self._emit_status("running", self.task_step_label, event_kind="task_start", play_sound=False)
            elif event == "progress":
                if self.task_state != "running":
                    self.task_id = secrets.token_hex(4)
                    self.task_started_at = now
                    self.task_state = "running"
                    self.recent_steps = []
                if title:
                    self.task_title = title.strip()
                self.task_step = int(step) if step is not None else (self.task_step + 1)
                if total_steps is not None:
                    self.task_total_steps = int(total_steps)
                self.task_last_activity = now
                if message:
                    self.task_step_label = message.strip()[:60]
                    self.recent_steps = (self.recent_steps + [self.task_step_label])[-5:]
                self._emit_status(
                    "running",
                    self.task_step_label or self.task_title or "进行中",
                    event_kind="task_progress",
                    play_sound=False,
                )
            elif event == "done":
                if title:
                    self.task_title = title.strip()
                if step is not None:
                    self.task_step = int(step)
                if total_steps is not None:
                    self.task_total_steps = int(total_steps)
                self.task_state = "done"
                self.task_explicit = False
                self.task_last_activity = now
                self.task_step_label = (message or f"{self.task_title or '任务'}已完成").strip()[:60]
                self._emit_status("idle", self.task_step_label, event_kind="task_done", play_sound=bool(play_sound))
            else:
                if title:
                    self.task_title = title.strip()
                self.task_state = "error"
                self.task_explicit = False
                self.task_last_activity = now
                self.task_step_label = (message or "任务异常终止").strip()[:60]
                self._emit_status("error", self.task_step_label, event_kind="task_error", play_sound=bool(play_sound))
            end_t = now if self.task_state == "running" else self.task_last_activity
            return {
                "status": "ok",
                "task_state": self.task_state,
                "task_title": self.task_title,
                "task_step": self.task_step,
                "task_total_steps": self.task_total_steps,
                "task_step_label": self.task_step_label,
                "task_elapsed_s": round(max(0.0, end_t - self.task_started_at), 2) if self.task_started_at > 0.0 else 0.0,
            }

    def complete_auto_task(self, message: str | None = None, play_sound: bool = True) -> dict[str, Any]:
        """Auto-close an implicit (non-cua_task) running sequence after idle grace without stretching elapsed time."""
        with self._lock:
            if self.task_state != "running" or getattr(self, "task_explicit", False):
                return {"status": "noop", "task_state": self.task_state}
            self.task_state = "done"
            self.task_explicit = False
            self.task_step_label = (message or "任务已完成").strip()[:60]
            self._emit_status("idle", self.task_step_label, event_kind="task_done", play_sound=bool(play_sound))
            elapsed = round(max(0.0, self.task_last_activity - self.task_started_at), 2) if self.task_started_at > 0.0 else 0.0
            return {
                "status": "ok",
                "task_state": self.task_state,
                "task_step": self.task_step,
                "task_step_label": self.task_step_label,
                "task_elapsed_s": elapsed,
            }

    def _is_desktop_like(self) -> bool:
        return getattr(self, "target_mode", "managed") in {"desktop", "os"}

    def _list_desktop_windows(self) -> list[ChromeWindowInfo]:
        managed_hwnd = self.chrome.hwnd
        return [w for w in find_chrome_windows() if not managed_hwnd or w.hwnd != managed_hwnd]

    def _list_os_windows(self) -> list[tuple[ChromeWindowInfo, str]]:
        excluded = {h for h in (self.chrome.hwnd, getattr(self, "panel_hwnd", 0)) if h}
        return find_all_windows(exclude_hwnds=excluded)

    @staticmethod
    def _serialize_windows(wins: list[ChromeWindowInfo]) -> list[dict[str, Any]]:
        return [
            {
                "hwnd": w.hwnd,
                "title": w.title,
                "is_foreground": w.is_foreground,
                "is_minimized": w.is_minimized,
            }
            for w in wins
        ]

    @staticmethod
    def _serialize_os_windows(wins: list[tuple[ChromeWindowInfo, str]]) -> list[dict[str, Any]]:
        return [
            {
                "hwnd": w.hwnd,
                "title": w.title,
                "process": proc,
                "is_foreground": w.is_foreground,
                "is_minimized": w.is_minimized,
                "rect": list(w.dwm_rect.to_tuple()),
            }
            for w, proc in wins
        ]

    @staticmethod
    def _check_desktop_title_gate(title: str, page_text: str = "") -> str | None:
        low = f"{title or ''}\n{(page_text or '')[:1000]}".lower()
        for sig in ("just a moment", "请完成安全验证", "人机验证", "verify you are human", "checking your browser"):
            if sig in low:
                return f"anti_bot:{sig}"
        return None

    def set_target_mode(self, mode: str, hwnd: int | None = None) -> dict[str, Any]:
        """Switch CUA engine target between 'managed' Chrome, 'desktop' Chrome, and 'os' (any window or full screen)."""
        if mode not in {"managed", "desktop", "os"}:
            raise ValueError(f"Unsupported target_mode: {mode!r}")
        with self._lock:
            if mode == "os":
                os_wins = self._list_os_windows()
                target_hwnd = int(hwnd) if hwnd is not None else 0
                if target_hwnd > 0:
                    if not win32gui.IsWindow(target_hwnd):
                        raise ValueError(f"OS window hwnd not found: {target_hwnd}")
                    info = inspect_chrome_window(target_hwnd)
                    proc = get_window_process_name(target_hwnd)
                    self.desktop_hwnd = target_hwnd
                    desktop_title = info.title
                else:
                    self.desktop_hwnd = 0
                    desktop_title = "Primary Screen"
                    proc = "desktop"
                self.target_mode = "os"
                self.active_title = desktop_title
                self._emit_status("idle", f"已切换到：全桌面 Computer Use（{desktop_title[:24]}）")
                return {
                    "target_mode": self.target_mode,
                    "desktop_hwnd": self.desktop_hwnd,
                    "desktop_title": desktop_title,
                    "desktop_process": proc,
                    "os_windows": self._serialize_os_windows(os_wins),
                }

            wins = self._list_desktop_windows()
            if hwnd is not None and int(hwnd) > 0:
                target_hwnd = int(hwnd)
                if not any(w.hwnd == target_hwnd for w in wins):
                    raise ValueError(f"Desktop Chrome hwnd not found: {target_hwnd}")
                self.desktop_hwnd = target_hwnd
            elif mode == "desktop":
                cur = next((w for w in wins if w.hwnd == self.desktop_hwnd), None)
                if cur is None and wins:
                    cur = next((w for w in wins if not w.is_minimized), wins[0])
                self.desktop_hwnd = cur.hwnd if cur is not None else 0
            self.target_mode = mode
            chosen = next((w for w in wins if w.hwnd == self.desktop_hwnd), None)
            desktop_title = chosen.title if chosen is not None else ""
            if mode == "desktop" and desktop_title:
                self.active_title = desktop_title
            self._emit_status("idle", f"已切换到：{'本机桌面 Chrome' if mode == 'desktop' else '托管后台 Chrome'}")
            return {
                "target_mode": self.target_mode,
                "desktop_hwnd": self.desktop_hwnd,
                "desktop_title": desktop_title,
                "desktop_windows": self._serialize_windows(wins),
            }

    def _resolve_desktop_window(self, focus: bool = False) -> ChromeWindowInfo:
        if getattr(self, "target_mode", "desktop") == "os":
            if self.desktop_hwnd > 0:
                if not win32gui.IsWindow(self.desktop_hwnd):
                    closed_hwnd = self.desktop_hwnd
                    self.desktop_hwnd = 0
                    raise RuntimeError(f"target_window_closed: 绑定窗口 hwnd={closed_hwnd} 已关闭，已中止操作以防退回全主屏")
                chosen = inspect_chrome_window(self.desktop_hwnd)
                if focus or chosen.is_minimized:
                    chosen = focus_chrome_window(chosen, settle_s=0.18)
                return chosen
            self.desktop_hwnd = 0
            return inspect_primary_screen()

        wins = self._list_desktop_windows()
        if not wins:
            self.desktop_hwnd = 0
            raise RuntimeError("未检测到本机已打开的桌面 Chrome 窗口；请先打开本机 Chrome，或切换到 managed / os 模式。")
        chosen = next((w for w in wins if w.hwnd == self.desktop_hwnd), None)
        if chosen is None:
            chosen = next((w for w in wins if not w.is_minimized), wins[0])
            self.desktop_hwnd = chosen.hwnd
        if focus or chosen.is_minimized:
            chosen = focus_chrome_window(chosen, settle_s=0.18)
        return chosen

    def _capture_desktop_window(self, focus: bool = False) -> tuple[ChromeWindowInfo, Image.Image]:
        info = self._resolve_desktop_window(focus=focus)
        if info.hwnd == 0:
            return info, capture_roi(info.dwm_rect)
        try:
            img = print_window_dwm(info)
            if getattr(self, "target_mode", "desktop") == "os" and int(np.asarray(img).max()) == 0:
                raise RuntimeError("print_window_blank")
        except Exception:
            info = focus_chrome_window(info, settle_s=0.18)
            if getattr(self, "target_mode", "desktop") == "os":
                img = capture_roi(info.dwm_rect)
            else:
                img = capture_chrome_window(info)
        return info, img

    def _focus_and_verify_desktop(self, info: ChromeWindowInfo, settle_s: float = 0.16) -> ChromeWindowInfo:
        """Bring target window to foreground and fail closed before any SendInput if focus did not switch."""
        if getattr(self, "target_mode", "desktop") == "os" and info.hwnd == 0:
            return info
        focused = focus_chrome_window(info, settle_s=settle_s)
        if win32gui.GetForegroundWindow() != focused.hwnd:
            if getattr(self, "target_mode", "desktop") == "os":
                raise RuntimeError("target_window_not_foreground: 未能将目标窗口切至前台，已中止按键发送以防误输入到其他软件")
            raise RuntimeError("chrome_not_foreground: 未能将本机 Chrome 切至前台，已中止按键发送以防误输入到其他软件")
        return focused

    @staticmethod
    def _attach_screenshot(
        out: dict[str, Any],
        img: Image.Image | None,
        include_screenshot: bool,
        screenshot_format: str = "jpeg",
    ) -> None:
        if include_screenshot and img is not None:
            b64, mime = _encode_mcp_image(img, fmt=screenshot_format)
            out["screenshot_b64"] = b64
            out["screenshot_mime"] = mime

    @staticmethod
    def _resolve_until_texts(until_text: str | list[str] | tuple[str, ...] | None) -> list[str]:
        if until_text is None:
            return []
        raw_items = [until_text] if isinstance(until_text, str) else list(until_text)
        return [re.sub(r"\s+", " ", str(x)).strip().lower() for x in raw_items if str(x).strip()]

    @staticmethod
    def _text_matches_any(haystack: str, targets: list[str]) -> bool:
        if not targets:
            return True
        low = (haystack or "").lower()
        norm = re.sub(r"\s+", " ", low)
        return any((t in low) or (t in norm) for t in targets)

    def _read_desktop_url_safe(self, info: ChromeWindowInfo | None) -> str:
        if info is None or not getattr(info, "hwnd", 0):
            return ""
        try:
            dwm = getattr(info, "dwm_rect", None)
            top = int(dwm.top) if dwm is not None else 0
            dpi = float(getattr(info, "dpi_scale", 1.0) or 1.0)
            uia_data = self._desktop_url_reader.read_url_and_tabs(info.hwnd, max_y_px=top + round(190 * dpi))
            return str((uia_data or {}).get("url") or "")
        except Exception:
            return ""

    def _perceive_desktop(
        self,
        info: ChromeWindowInfo,
        raw_img: Image.Image,
        max_elements: int = 60,
        max_chars: int = 2400,
    ) -> dict[str, Any]:
        if getattr(self, "target_mode", "managed") == "os":
            ocr_data = self._desktop_ocr.recognize(raw_img, max_elements=max_elements, max_chars=max_chars)
            ocr_els = list(ocr_data.get("ocr_elements") or [])
            self._last_ocr_elements = ocr_els
            self.active_url = ""
            if info.title:
                self.active_title = info.title
            proc = get_window_process_name(info.hwnd) if info.hwnd else "desktop"
            return {
                "url": "",
                "title": info.title,
                "process": proc,
                "page_text": str(ocr_data.get("page_text", "")),
                "ocr_elements": ocr_els,
                "ocr_ms": float(ocr_data.get("ocr_ms", 0.0)),
                "tabs": [],
            }
        uia_max_y = info.dwm_rect.top + round(190 * info.dpi_scale)
        uia_data = self._desktop_url_reader.read_url_and_tabs(info.hwnd, max_y_px=uia_max_y)
        ocr_data = self._desktop_ocr.recognize(raw_img, max_elements=max_elements, max_chars=max_chars)
        url = str(uia_data.get("url") or ocr_data.get("inferred_url") or "")
        ocr_els = list(ocr_data.get("ocr_elements") or [])
        self._last_ocr_elements = ocr_els
        if url:
            self.active_url = url
        if info.title:
            self.active_title = info.title
        return {
            "url": url,
            "title": info.title,
            "page_text": str(ocr_data.get("page_text", "")),
            "ocr_elements": ocr_els,
            "ocr_ms": float(ocr_data.get("ocr_ms", 0.0)),
            "tabs": list(uia_data.get("tabs") or []),
        }

    @contextlib.contextmanager
    def _desktop_io_guard(self) -> Any:
        if self.desktop_hide_hook is not None:
            try:
                self.desktop_hide_hook(True)
            except Exception:
                pass
        try:
            yield
        finally:
            if self.desktop_hide_hook is not None:
                try:
                    self.desktop_hide_hook(False)
                except Exception:
                    pass

    @staticmethod
    def _confirm_target_sig(element_meta: dict[str, Any] | None, matched_rule: str = "") -> str:
        meta = element_meta or {}
        label = str(meta.get("text") or meta.get("aria") or meta.get("placeholder") or "").strip()[:120]
        el_type = str(meta.get("type") or "").strip().lower()
        return hashlib.sha256(f"{el_type}|{label}|{matched_rule}".encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _find_ocr_element_at_coord(
        ocr_elements: list[dict[str, Any]],
        coord: tuple[int, int] | list[int] | None,
        margin: int = 12,
    ) -> dict[str, Any] | None:
        if not coord or len(coord) < 2 or not ocr_elements:
            return None
        cx, cy = int(coord[0]), int(coord[1])
        best_el: dict[str, Any] | None = None
        best_dist: int | None = None
        for el in ocr_elements:
            rn = el.get("rect_norm") or [0, 0, 0, 0]
            if len(rn) != 4:
                continue
            if (rn[0] - margin) <= cx <= (rn[2] + margin) and (rn[1] - margin) <= cy <= (rn[3] + margin):
                cn = el.get("center_norm") or [(rn[0] + rn[2]) // 2, (rn[1] + rn[3]) // 2]
                dist = (int(cn[0]) - cx) ** 2 + (int(cn[1]) - cy) ** 2
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_el = el
        return best_el

    def evaluate_desktop_action_policy(
        self,
        instruction: str,
        info: ChromeWindowInfo,
        frame: Image.Image,
        parsed: ParsedAction | None,
        *,
        resolved_ocr_el: dict[str, Any] | None = None,
        gate_reason: str | None = None,
    ) -> tuple[PolicyDecision, dict[str, Any] | None]:
        """Evaluate policy on instruction + OCR element at the resolved coordinate (or title/page gate)."""
        hit_el = resolved_ocr_el
        if (
            hit_el is None
            and parsed is not None
            and parsed.action in {"left_click", "double_click", "right_click", "type"}
            and parsed.coordinate is not None
        ):
            ocr_reader = getattr(self, "_desktop_ocr", None)
            if ocr_reader is not None:
                try:
                    perc = ocr_reader.recognize(frame, max_elements=60, max_chars=2400)
                    hit_el = self._find_ocr_element_at_coord(list(perc.get("ocr_elements") or []), parsed.coordinate)
                except Exception:
                    hit_el = None
        return evaluate_policy(instruction, element_meta=hit_el, gate_reason=gate_reason), hit_el

    @staticmethod
    def _token_base_sig(instruction: str, element_id: int | None, ocr_id: int | None = None) -> str:
        return hashlib.sha256(f"{instruction}|{element_id}|{ocr_id}".encode("utf-8")).hexdigest()[:16]

    def _make_confirm_token(
        self,
        instruction: str,
        element_id: int | None,
        *,
        ocr_id: int | None = None,
        target_sig: str = "",
        matched_rule: str = "",
    ) -> str:
        sig = self._token_base_sig(instruction, element_id, ocr_id=ocr_id)
        token = secrets.token_hex(6)
        self._pending_confirms[token] = (time.monotonic(), sig, str(target_sig or ""), str(matched_rule or ""))
        return token

    def _peek_confirm_token(
        self,
        token: str | None,
        instruction: str,
        element_id: int | None,
        *,
        ocr_id: int | None = None,
    ) -> tuple[str, str] | None:
        if not token or token not in self._pending_confirms:
            return None
        entry = self._pending_confirms[token]
        if len(entry) >= 4:
            ts, sig, target_sig, matched_rule = entry[0], entry[1], entry[2], entry[3]
        else:
            ts, sig = entry[0], entry[1]
            target_sig, matched_rule = "", ""
        if time.monotonic() - ts > 600.0:
            self._pending_confirms.pop(token, None)
            return None
        expected = self._token_base_sig(instruction, element_id, ocr_id=ocr_id)
        if sig != expected:
            return None
        return str(target_sig), str(matched_rule)

    def _consume_confirm_token(
        self,
        token: str | None,
        instruction: str,
        element_id: int | None,
        *,
        ocr_id: int | None = None,
        required_target_sig: str = "",
    ) -> bool:
        peeked = self._peek_confirm_token(token, instruction, element_id, ocr_id=ocr_id)
        if peeked is None:
            return False
        tok_target_sig, _tok_rule = peeked
        if required_target_sig and tok_target_sig != required_target_sig:
            return False
        assert token is not None
        self._pending_confirms.pop(token, None)
        return True

    def _verify_resolved_target_policy(
        self,
        *,
        policy_inst: str,
        element_id: int | None,
        ocr_id: int | None,
        confirm_token: str | None,
        pre_confirmed: tuple[str, str] | None,
        resolved_meta: dict[str, Any] | None,
        coordinate: tuple[int, int] | list[int] | None,
        mode_tag: str | None = None,
        local_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Verify resolved target element against policy; require target-bound confirmation if Red-tier."""
        el_policy = evaluate_policy("", element_meta=resolved_meta)
        if el_policy.tier == "red":
            req_sig = self._confirm_target_sig(resolved_meta, el_policy.matched_rule)
            matched = pre_confirmed is not None and (
                pre_confirmed[0] == req_sig
                or (pre_confirmed[0] == "" and pre_confirmed[1] == el_policy.matched_rule)
            )
            if matched:
                if confirm_token:
                    self._pending_confirms.pop(confirm_token, None)
                return None
            tok = self._make_confirm_token(
                policy_inst,
                element_id,
                ocr_id=ocr_id,
                target_sig=req_sig,
                matched_rule=el_policy.matched_rule,
            )
            audit_rec: dict[str, Any] = {
                "op": "act",
                "blocked": "red_tier_post_grounding",
                "rule": el_policy.matched_rule,
                "instruction": policy_inst,
            }
            if mode_tag:
                audit_rec["mode"] = mode_tag
            append_audit(audit_rec)
            self.task_state = "confirm"
            self.task_step_label = el_policy.reason[:60]
            self._emit_status("confirm", el_policy.reason, event_kind="task_alert", play_sound=True)
            out: dict[str, Any] = {
                "status": "needs_human_confirmation",
                "confirm_token": tok,
                "reason": el_policy.reason,
                "matched_rule": el_policy.matched_rule,
            }
            if mode_tag:
                out["mode"] = mode_tag
            if coordinate is not None:
                out["coordinate"] = list(coordinate)
            if local_usage is not None:
                out["local_usage"] = local_usage
            return out
        if pre_confirmed is not None and confirm_token:
            self._pending_confirms.pop(confirm_token, None)
        return None

    def get_status(self, light: bool = False) -> dict[str, Any]:
        """Full status; light=True (per-event UI telemetry) reuses the last GPU/window/tab probe instead of
        spawning nvidia-smi and enumerating windows on every status emission."""
        now = time.monotonic()
        if not light and now - self._vram_cache[0] > self.VRAM_TTL_S:
            try:
                self._vram_cache = (now, query_gpu_memory_mib())
            except Exception:
                self._vram_cache = (now, {})
        vram = dict(self._vram_cache[1])
        if not light:
            tabs_count = 0
            if self.chrome.is_running():
                try:
                    tabs_count = len(self.chrome._list_targets())
                except Exception:
                    pass
            self._status_heavy = {"wins": self._list_desktop_windows(), "tabs_count": tabs_count}
        wins = self._status_heavy["wins"]
        tabs_count = self._status_heavy["tabs_count"]
        chosen = next((w for w in wins if w.hwnd == self.desktop_hwnd), wins[0] if wins else None)
        if self.target_mode == "os":
            reported_hwnd = self.desktop_hwnd if (self.desktop_hwnd == 0 or win32gui.IsWindow(self.desktop_hwnd)) else 0
            reported_title = win32gui.GetWindowText(reported_hwnd) if reported_hwnd else "Primary Screen"
        else:
            reported_hwnd = self.desktop_hwnd or (chosen.hwnd if chosen else 0)
            reported_title = chosen.title if chosen else ""
        if self.task_started_at > 0.0:
            end_t = time.monotonic() if self.task_state == "running" else self.task_last_activity
            task_elapsed = round(max(0.0, end_t - self.task_started_at), 2)
        else:
            task_elapsed = 0.0
        connected_harnesses = [
            {"name": meta["name"], "version": meta["version"]}
            for meta in self._mcp_sessions.values()
        ]
        harness_mode = "none" if not connected_harnesses else ("single" if len(connected_harnesses) == 1 else "multi")
        return {
            "state": self.state,
            "last_message": self.last_message,
            "target_mode": self.target_mode,
            "desktop_hwnd": reported_hwnd,
            "desktop_title": reported_title,
            "desktop_windows": self._serialize_windows(wins),
            "model": self.grounder.model_label,
            "vlm_running": self.grounder.is_running(),
            "vlm_port": self.grounder.port,
            "chrome_running": self.chrome.is_running(),
            "active_url": self.active_url,
            "active_title": self.active_title,
            "tabs_count": tabs_count,
            "step_count": self.step_count,
            "mcp_port": self.mcp_port,
            "mcp_clients": self.mcp_clients,
            "active_harness": self.active_harness,
            "connected_harnesses": connected_harnesses,
            "harness_mode": harness_mode,
            "task_state": self.task_state,
            "task_explicit": bool(getattr(self, "task_explicit", False)),
            "task_title": self.task_title,
            "task_step": self.task_step,
            "task_total_steps": self.task_total_steps,
            "task_step_label": self.task_step_label,
            "task_elapsed_s": task_elapsed,
            "recent_steps": list(self.recent_steps),
            "vram_mib": vram,
        }

    def observe(
        self,
        include_screenshot: bool = False,
        annotate_som: bool = False,
        max_elements: int = 60,
        screenshot_format: str = "jpeg",
        detail: str = "full",
    ) -> dict[str, Any]:
        brief = str(detail).lower() == "brief"
        with self._lock:
            if self._is_desktop_like():
                if self.task_state != "running":
                    self._emit_status("running", "正在识别本机画面…")
                info, raw_img = self._capture_desktop_window(focus=False)
                w_dwm, h_dwm = raw_img.size
                perc = self._perceive_desktop(info, raw_img, max_elements=max_elements, max_chars=2400)
                gate = self._check_desktop_title_gate(info.title, perc["page_text"])
                next_state = "running" if self.task_state == "running" else "idle"
                msg = (
                    f"{self.task_step_label} · OCR {len(perc['ocr_elements'])} 项"
                    if self.task_state == "running" and self.task_step_label
                    else f"已识别本机画面 · {len(perc['ocr_elements'])} 项 · {info.title[:22]}"
                )
                self._emit_status(next_state, msg)
                out: dict[str, Any] = {
                    "status": "ok",
                    "mode": "desktop_vision" if self.target_mode == "desktop" else "os",
                    "hwnd": info.hwnd,
                    "url": perc["url"],
                    "title": info.title,
                    "frame_size": [w_dwm, h_dwm],
                    "page_text": perc["page_text"],
                    "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                    "ocr_ms": perc["ocr_ms"],
                    "popup_detected": False,
                    "auth_or_captcha_gate": gate,
                }
                if self.target_mode == "os":
                    out["process"] = perc.get("process", "")
                self._attach_screenshot(out, raw_img, include_screenshot, screenshot_format)
                return _brief_observation(out) if brief else out

            if self.task_state != "running":
                self._emit_status("running", "正在获取 Chrome 画面与 DOM…")
            frame: Image.Image | None = None
            if include_screenshot:
                info, frame = self.chrome.capture_full_window()
                w_dwm, h_dwm = frame.size
            else:
                # DOM geometry only needs the frame size; skip the ~20 ms PrintWindow capture
                info, (w_dwm, h_dwm) = self.chrome.window_geometry()
            snap = self.chrome.extract_dom_snapshot(info, w_dwm, h_dwm, max_elements=max_elements)
            self.active_url = snap.get("url", "")
            self.active_title = snap.get("title", "")
            next_state = "running" if self.task_state == "running" else "idle"
            msg = (
                f"{self.task_step_label} · {len(snap.get('elements', []))} 个元素"
                if self.task_state == "running" and self.task_step_label
                else f"已获取画面 · {len(snap.get('elements', []))} 个交互元素"
            )
            self._emit_status(next_state, msg)
            compact_els = _compact_dom_elements(snap.get("elements", []))
            out = {
                "status": "ok",
                "mode": "managed",
                "frame_size": [w_dwm, h_dwm],
                **snap,
                "elements": compact_els,
            }
            if frame is not None:
                out_img = _draw_som_badges(frame, snap.get("elements", [])) if annotate_som else frame
                self._attach_screenshot(out, out_img, True, screenshot_format)
            return _brief_observation(out) if brief else out

    def _wait_desktop_frame_settle(
        self,
        hwnd: int,
        before_img: Image.Image | None = None,
        old_title: str = "",
        min_wait_s: float = 0.28,
        max_wait_s: float = 4.0,
        until_text: str | list[str] | None = None,
        meta_out: dict[str, Any] | None = None,
    ) -> tuple[ChromeWindowInfo, Image.Image, dict[str, Any] | None]:
        targets = self._resolve_until_texts(until_text)
        time.sleep(max(0.0, min_wait_s))
        deadline = time.monotonic() + max(0.3, max_wait_s)
        placeholder_prefixes = ("新标签页", "New Tab", "about:blank", "Untitled")

        def _snap_cur() -> tuple[ChromeWindowInfo, Image.Image]:
            if hwnd == 0 and self.target_mode == "os":
                ci = inspect_primary_screen()
                return ci, capture_roi(ci.dwm_rect)
            ci = inspect_chrome_window(hwnd) if win32gui.IsWindow(hwnd) else self._resolve_desktop_window(focus=False)
            if ci.hwnd == 0:
                return ci, capture_roi(ci.dwm_rect)
            try:
                im = print_window_dwm(ci)
                if self.target_mode == "os" and int(np.asarray(im).max()) == 0:
                    raise RuntimeError("print_window_blank")
            except Exception:
                if self.target_mode == "os":
                    im = capture_roi(ci.dwm_rect)
                else:
                    im = capture_chrome_window(ci)
            return ci, im

        cur_info, cur_img = _snap_cur()
        saw_change = (
            before_img is None
            or (cur_info.title != old_title)
            or (_frame_changed_fraction(before_img, cur_img) > 0.006)
        )
        change_wait_limit = min(deadline, time.monotonic() + 1.4)
        prev_img: Image.Image | None = None
        cached_perc: dict[str, Any] | None = None
        settled = False
        until_matched = not bool(targets)

        while time.monotonic() < deadline:
            if before_img is not None and not saw_change:
                if (
                    (cur_info.title and cur_info.title != old_title and not any(cur_info.title.startswith(p) for p in placeholder_prefixes))
                    or _frame_changed_fraction(before_img, cur_img) > 0.006
                ):
                    saw_change = True

            ready_to_check = saw_change or (time.monotonic() >= change_wait_limit)
            if ready_to_check and prev_img is not None and _frame_changed_fraction(prev_img, cur_img) < 0.004:
                title_ok = not (
                    old_title
                    and cur_info.title != old_title
                    and any(cur_info.title.startswith(p) for p in placeholder_prefixes)
                )
                if title_ok:
                    if not targets:
                        settled = True
                        until_matched = True
                        break
                    cached_perc = self._perceive_desktop(cur_info, cur_img, max_elements=60, max_chars=2400)
                    if self._text_matches_any(f"{cur_info.title}\n{cached_perc.get('page_text', '')}", targets):
                        settled = True
                        until_matched = True
                        break

            time.sleep(0.22)
            prev_img = cur_img
            cur_info, cur_img = _snap_cur()
            cached_perc = None

        if not settled and targets:
            if cached_perc is None:
                cached_perc = self._perceive_desktop(cur_info, cur_img, max_elements=60, max_chars=2400)
            until_matched = self._text_matches_any(f"{cur_info.title}\n{cached_perc.get('page_text', '')}", targets)
        if meta_out is not None:
            meta_out["settled"] = settled
            meta_out["timed_out"] = not settled
            meta_out["until_matched"] = bool(until_matched)

        return cur_info, cur_img, cached_perc

    def _wait_desktop_page_settle(self, hwnd: int, old_title: str, max_wait_s: float = 3.5) -> ChromeWindowInfo:
        cur_info, _cur_img, _perc = self._wait_desktop_frame_settle(
            hwnd, before_img=None, old_title=old_title, min_wait_s=0.28, max_wait_s=max_wait_s
        )
        return cur_info

    def _capture_and_snap_managed(self, max_elements: int = 50, capture: bool = True) -> tuple[ChromeWindowInfo, Any, dict[str, Any]]:
        """Window info, frame (None when capture=False), and DOM snapshot."""
        if capture:
            info, raw_img = self.chrome.capture_full_window()
            size = raw_img.size
        else:
            raw_img = None
            info, size = self.chrome.window_geometry()
        snap = self.chrome.extract_dom_snapshot(info, size[0], size[1], max_elements=max_elements)
        self.active_url = str(snap.get("url", ""))
        self.active_title = str(snap.get("title", ""))
        return info, raw_img, snap

    def _wait_managed_settle(
        self,
        prev_sig: str = "",
        max_wait_s: float = 3.5,
        until_text: str | list[str] | None = None,
    ) -> dict[str, Any]:
        targets = self._resolve_until_texts(until_text)
        deadline = time.monotonic() + max(0.3, max_wait_s)
        time.sleep(0.2)
        last_sig: str | None = None
        saw_change = not bool(prev_sig)
        change_limit = min(deadline, time.monotonic() + 1.2)
        probe: dict[str, Any] = {}
        settled = False
        until_matched = not bool(targets)

        while time.monotonic() < deadline:
            try:
                assert self.chrome.cdp is not None
                probe = self.chrome.cdp.evaluate(
                    "(() => {"
                    "const txt = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();"
                    "const n = document.querySelectorAll('a,button,input,textarea,select,[role=\"button\"],[role=\"tab\"]').length;"
                    "return {ready: document.readyState, url: location.href, title: document.title, "
                    "sig: location.href + '|' + n + '|' + txt.length + '|' + txt.slice(0, 260) + '|' + txt.slice(-260), "
                    "text: txt.slice(0, 4000)};"
                    "})()",
                    timeout=5.0,
                )
            except Exception:
                probe = {}

            sig = str(probe.get("sig", ""))
            if prev_sig and sig and sig != prev_sig:
                saw_change = True
            ready_ok = probe.get("ready") == "complete"
            if ready_ok and (saw_change or time.monotonic() >= change_limit) and last_sig is not None and sig == last_sig:
                if not targets or self._text_matches_any(f"{probe.get('title', '')}\n{probe.get('text', '')}", targets):
                    settled = True
                    until_matched = True
                    break
            last_sig = sig
            time.sleep(0.2)
        if not settled and targets and probe:
            until_matched = self._text_matches_any(f"{probe.get('title', '')}\n{probe.get('text', '')}", targets)
        if isinstance(probe, dict):
            probe["settled"] = settled
            probe["timed_out"] = not settled
            probe["until_matched"] = bool(until_matched)
        return probe

    @staticmethod
    def _clean_desktop_tab_title(title: str) -> str:
        s = re.sub(r"\s+", " ", str(title or "")).strip()
        for suffix in (" - Google Chrome",):
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip()
        for marker in (" - 内存用量", " - Memory usage"):
            if marker in s:
                s = s.split(marker, 1)[0].strip()
        return s.lower()

    @classmethod
    def _desktop_tab_is_active(
        cls,
        chosen_tab: dict[str, Any],
        window_title: str,
        refreshed_tabs: list[dict[str, Any]] | None = None,
    ) -> bool:
        chosen_clean = cls._clean_desktop_tab_title(str(chosen_tab.get("title", "")))
        if refreshed_tabs:
            has_sel = any("selected" in t for t in refreshed_tabs)
            if has_sel:
                selected_tabs = [t for t in refreshed_tabs if t.get("selected") is True]
                if len(selected_tabs) != 1:
                    return False
                sel = selected_tabs[0]
                sel_clean = cls._clean_desktop_tab_title(str(sel.get("title", "")))
                if not chosen_clean or sel_clean != chosen_clean:
                    return False
                same_name_count = sum(
                    1 for t in refreshed_tabs if cls._clean_desktop_tab_title(str(t.get("title", ""))) == chosen_clean
                )
                if same_name_count > 1 and chosen_tab.get("index") is not None and sel.get("index") is not None:
                    return int(sel["index"]) == int(chosen_tab["index"])
                return True
        win_clean = cls._clean_desktop_tab_title(window_title)
        if not chosen_clean or not win_clean:
            return False
        return win_clean == chosen_clean or chosen_clean.startswith(win_clean) or win_clean.startswith(chosen_clean)

    def _navigate_desktop(
        self,
        action: str,
        url: str | None,
        tab_id: str | int | None,
        include_screenshot: bool = False,
        screenshot_format: str = "jpeg",
        hwnd: int | None = None,
    ) -> dict[str, Any]:
        if action == "focus_window":
            os_wins = self._list_os_windows()
            matched_pair: tuple[ChromeWindowInfo, str] | None = None
            target_id = hwnd if (hwnd is not None and int(hwnd) > 0) else (
                int(tab_id) if (isinstance(tab_id, int) or (isinstance(tab_id, str) and tab_id.isdigit())) else None
            )
            if target_id is not None:
                matched_pair = next((pair for pair in os_wins if pair[0].hwnd == target_id), None)
                if matched_pair is None and win32gui.IsWindow(target_id):
                    matched_pair = (inspect_chrome_window(target_id), get_window_process_name(target_id))
            elif tab_id is not None or url:
                ref_low = str(tab_id or url).strip().lower()
                matched_pair = next(
                    (pair for pair in os_wins if ref_low in pair[0].title.lower() or ref_low in pair[1].lower()),
                    None,
                )
            if matched_pair is None:
                raise ValueError(f"Desktop window not found: {hwnd or tab_id or url!r}")
            matched_win, matched_proc = matched_pair
            self.target_mode = "os"
            self.desktop_hwnd = matched_win.hwnd
            with self._desktop_io_guard():
                focused = focus_chrome_window(matched_win, settle_s=0.18)
            new_info, shot_img = self._capture_desktop_window(focus=False)
            perc = self._perceive_desktop(new_info, shot_img, max_elements=60, max_chars=2400)
            self.active_title = focused.title
            out: dict[str, Any] = {
                "mode": "os",
                "hwnd": focused.hwnd,
                "process": matched_proc,
                "url": "",
                "title": focused.title,
                "page_text": perc["page_text"],
                "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                "ocr_ms": perc["ocr_ms"],
            }
            self._attach_screenshot(out, shot_img, include_screenshot, screenshot_format)
            return out

        if getattr(self, "target_mode", "desktop") == "os" and action in {"goto", "new_tab", "back", "forward", "reload", "list_tabs", "switch_tab", "close_tab"}:
            raise ValueError(
                f"Action {action!r} requires mode='desktop' or 'managed' (Chrome); "
                "in mode='os' use list_windows, focus_window, set_mode, or cua_act."
            )

        hotkey_map = {
            "back": ["alt", "left"],
            "forward": ["alt", "right"],
            "reload": ["ctrl", "r"],
        }

        def _try_dwm_shot(win_info: ChromeWindowInfo) -> Image.Image | None:
            try:
                return print_window_dwm(win_info)
            except Exception:
                return None

        if action == "goto":
            if not url:
                raise ValueError("url is required for navigate(action='goto')")
            if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError(f"Only http/https URLs are supported, got: {url!r}")
            info = self._resolve_desktop_window(focus=False)
            old_title = info.title
            before_img = _try_dwm_shot(info)
            with self._desktop_io_guard():
                if check_corner_failsafe():
                    raise RuntimeError("failsafe_corner_triggered")
                info = self._focus_and_verify_desktop(info, settle_s=0.16)
                send_hotkey(["ctrl", "l"])
                time.sleep(0.08)
                paste_text_via_clipboard(url)
                time.sleep(0.06)
                send_hotkey(["enter"])
            new_info, settled_img, cached_perc = self._wait_desktop_frame_settle(
                info.hwnd, before_img=before_img, old_title=old_title, min_wait_s=0.35, max_wait_s=4.0
            )
            perc = cached_perc or self._perceive_desktop(new_info, settled_img, max_elements=45, max_chars=1800)
            self.active_url = perc["url"] or url
            self.active_title = new_info.title
            out: dict[str, Any] = {
                "mode": "desktop",
                "requested_url": url,
                "url": self.active_url,
                "title": new_info.title,
                "hwnd": new_info.hwnd,
                "page_text": perc["page_text"],
                "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                "ocr_ms": perc["ocr_ms"],
                "auth_or_captcha_gate": self._check_desktop_title_gate(new_info.title, perc["page_text"]),
            }
            self._attach_screenshot(out, settled_img, include_screenshot, screenshot_format)
            return out

        if action == "new_tab":
            if url and url != "about:blank" and urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError(f"Invalid URL scheme: {url!r}")
            info = self._resolve_desktop_window(focus=False)
            old_title = info.title
            before_img = _try_dwm_shot(info)
            with self._desktop_io_guard():
                if check_corner_failsafe():
                    raise RuntimeError("failsafe_corner_triggered")
                info = self._focus_and_verify_desktop(info, settle_s=0.16)
                send_hotkey(["ctrl", "t"])
                if url and url != "about:blank":
                    time.sleep(0.15)
                    paste_text_via_clipboard(url)
                    time.sleep(0.06)
                    send_hotkey(["enter"])
            has_url = bool(url and url != "about:blank")
            new_info, settled_img, cached_perc = self._wait_desktop_frame_settle(
                info.hwnd,
                before_img=before_img,
                old_title=old_title,
                min_wait_s=0.35 if has_url else 0.28,
                max_wait_s=4.0 if has_url else 0.9,
            )
            perc = cached_perc or self._perceive_desktop(new_info, settled_img, max_elements=45, max_chars=1800)
            self.active_url = perc["url"] or (url or "")
            self.active_title = new_info.title
            out = {
                "mode": "desktop",
                "requested_url": url or "chrome://newtab",
                "url": self.active_url,
                "title": new_info.title,
                "hwnd": new_info.hwnd,
                "page_text": perc["page_text"],
                "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                "ocr_ms": perc["ocr_ms"],
            }
            self._attach_screenshot(out, settled_img, include_screenshot, screenshot_format)
            return out

        if action == "close_tab":
            info = self._resolve_desktop_window(focus=False)
            if tab_id is None:
                with self._desktop_io_guard():
                    if check_corner_failsafe():
                        raise RuntimeError("failsafe_corner_triggered")
                    info = self._focus_and_verify_desktop(info, settle_s=0.16)
                    send_hotkey(["ctrl", "w"])
                time.sleep(0.35)
                new_info = inspect_chrome_window(info.hwnd) if win32gui.IsWindow(info.hwnd) else None
                title_now = new_info.title if new_info else ""
                self.active_title = title_now
                return {"mode": "desktop", "action": "close_tab", "title": title_now, "hwnd": info.hwnd}

            tab_data = self._navigate_desktop("list_tabs", None, None, include_screenshot=False)
            tabs = list(tab_data.get("tabs") or [])
            chosen_tab = None
            if isinstance(tab_id, int) or (isinstance(tab_id, str) and tab_id.isdigit()):
                idx = int(tab_id)
                if 0 <= idx < len(tabs):
                    chosen_tab = tabs[idx]
            if chosen_tab is None:
                ref_low = str(tab_id).lower()
                chosen_tab = next((t for t in tabs if ref_low in str(t.get("title", "")).lower()), None)
            if chosen_tab is None:
                raise ValueError(f"Desktop tab not found: {tab_id!r}")

            if not self._desktop_tab_is_active(chosen_tab, info.title, tabs):
                b = chosen_tab["box_px"]
                with self._desktop_io_guard():
                    if check_corner_failsafe():
                        raise RuntimeError("failsafe_corner_triggered")
                    focused = self._focus_and_verify_desktop(info, settle_s=0.16)
                    abs_x = focused.dwm_rect.left + (b[0] + b[2]) // 2
                    abs_y = focused.dwm_rect.top + (b[1] + b[3]) // 2
                    click_abs(abs_x, abs_y, button="left", count=1)
                time.sleep(0.30)

            if not win32gui.IsWindow(info.hwnd):
                raise RuntimeError(f"target_window_closed: Chrome 窗口 hwnd={info.hwnd} 已关闭，已拒绝关闭标签页")
            cur_info = inspect_chrome_window(info.hwnd)
            after_tabs: list[dict[str, Any]] | None = None
            try:
                after_data = self._navigate_desktop("list_tabs", None, None, include_screenshot=False)
                after_tabs = list(after_data.get("tabs") or []) or None
            except Exception:
                after_tabs = None
            if not self._desktop_tab_is_active(chosen_tab, cur_info.title, after_tabs):
                raise RuntimeError(
                    f"desktop_close_tab_not_active: 目标标签页 {chosen_tab['title']!r} 未成为活动页（当前：{cur_info.title!r}），已拒绝关闭"
                )

            with self._desktop_io_guard():
                if check_corner_failsafe():
                    raise RuntimeError("failsafe_corner_triggered")
                cur_info = self._focus_and_verify_desktop(cur_info, settle_s=0.12)
                send_hotkey(["ctrl", "w"])
            time.sleep(0.35)
            new_info = inspect_chrome_window(info.hwnd) if win32gui.IsWindow(info.hwnd) else None
            title_now = new_info.title if new_info else ""
            self.active_title = title_now
            return {
                "mode": "desktop",
                "action": "close_tab",
                "closed_tab": chosen_tab["title"],
                "title": title_now,
                "hwnd": info.hwnd,
            }

        if action in hotkey_map:
            info = self._resolve_desktop_window(focus=False)
            old_title = info.title
            before_img = _try_dwm_shot(info)
            with self._desktop_io_guard():
                if check_corner_failsafe():
                    raise RuntimeError("failsafe_corner_triggered")
                info = self._focus_and_verify_desktop(info, settle_s=0.16)
                send_hotkey(hotkey_map[action])
            new_info, settled_img, cached_perc = self._wait_desktop_frame_settle(
                info.hwnd, before_img=before_img, old_title=old_title, min_wait_s=0.3, max_wait_s=3.5
            )
            perc = cached_perc or self._perceive_desktop(new_info, settled_img, max_elements=45, max_chars=1800)
            self.active_url = perc["url"]
            self.active_title = new_info.title
            out = {
                "mode": "desktop",
                "action": action,
                "url": self.active_url,
                "title": new_info.title,
                "hwnd": new_info.hwnd,
                "page_text": perc["page_text"],
                "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                "ocr_ms": perc["ocr_ms"],
            }
            self._attach_screenshot(out, settled_img, include_screenshot, screenshot_format)
            return out

        if action == "list_tabs":
            info = self._resolve_desktop_window(focus=False)
            try:
                if self._desktop_uia is None:
                    self._desktop_uia = UIAHelper()
                max_y = info.dwm_rect.top + round(140 * info.dpi_scale)
                raw_items = self._desktop_uia.query(info.hwnd, max_y)
                dwm = info.dwm_rect
                tabs = []
                for item in raw_items:
                    if item.get("type") == "TabItem":
                        clean = str(item.get("name", "")).split(" - 内存用量")[0].strip()
                        tab_entry: dict[str, Any] = {
                            "index": len(tabs),
                            "title": clean,
                            "box_px": [
                                item["left"] - dwm.left,
                                item["top"] - dwm.top,
                                item["right"] - dwm.left,
                                item["bottom"] - dwm.top,
                            ],
                        }
                        if item.get("selected") is not None:
                            tab_entry["selected"] = bool(item["selected"])
                        tabs.append(tab_entry)
                return {"mode": "desktop", "hwnd": info.hwnd, "tabs": tabs}
            except Exception as exc:
                wins = self._list_desktop_windows()
                return {
                    "mode": "desktop",
                    "hwnd": info.hwnd,
                    "tabs": [],
                    "desktop_windows": self._serialize_windows(wins),
                    "note": f"UIA 不可用，已返回窗口列表 ({exc})",
                }

        if action == "switch_tab":
            if tab_id is None:
                raise ValueError("tab_id is required for switch_tab")
            info = self._resolve_desktop_window(focus=False)
            try:
                tab_data = self._navigate_desktop("list_tabs", None, None, include_screenshot=False)
                tabs = tab_data.get("tabs", [])
            except Exception:
                tabs = []
            chosen_tab = None
            if isinstance(tab_id, int) or (isinstance(tab_id, str) and tab_id.isdigit()):
                idx = int(tab_id)
                if 0 <= idx < len(tabs):
                    chosen_tab = tabs[idx]
            if chosen_tab is None:
                ref_low = str(tab_id).lower()
                chosen_tab = next((t for t in tabs if ref_low in t["title"].lower()), None)
            if chosen_tab is not None:
                b = chosen_tab["box_px"]
                with self._desktop_io_guard():
                    if check_corner_failsafe():
                        raise RuntimeError("failsafe_corner_triggered")
                    focused = self._focus_and_verify_desktop(info, settle_s=0.16)
                    abs_x = focused.dwm_rect.left + (b[0] + b[2]) // 2
                    abs_y = focused.dwm_rect.top + (b[1] + b[3]) // 2
                    click_abs(abs_x, abs_y, button="left", count=1)
                time.sleep(0.35)
                new_info, shot_img = self._capture_desktop_window(focus=False)
                perc = self._perceive_desktop(new_info, shot_img, max_elements=45, max_chars=1800)
                self.active_title = new_info.title
                out = {
                    "mode": "desktop",
                    "switched_tab": chosen_tab["title"],
                    "url": perc["url"],
                    "title": new_info.title,
                    "hwnd": new_info.hwnd,
                    "page_text": perc["page_text"],
                    "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                    "ocr_ms": perc["ocr_ms"],
                }
                self._attach_screenshot(out, shot_img, include_screenshot, screenshot_format)
                return out

            wins = self._list_desktop_windows()
            matched_win = None
            if isinstance(tab_id, int) or (isinstance(tab_id, str) and tab_id.isdigit()):
                matched_win = next((w for w in wins if w.hwnd == int(tab_id)), None)
            if matched_win is None:
                ref_low = str(tab_id).lower()
                matched_win = next((w for w in wins if ref_low in w.title.lower()), None)
            if matched_win is None:
                raise ValueError(f"Desktop tab/window not found: {tab_id!r}")
            self.desktop_hwnd = matched_win.hwnd
            with self._desktop_io_guard():
                focused = focus_chrome_window(matched_win, settle_s=0.18)
            new_info, shot_img = self._capture_desktop_window(focus=False)
            perc = self._perceive_desktop(new_info, shot_img, max_elements=45, max_chars=1800)
            self.active_title = focused.title
            out = {
                "mode": "desktop",
                "hwnd": focused.hwnd,
                "url": perc["url"],
                "title": focused.title,
                "page_text": perc["page_text"],
                "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                "ocr_ms": perc["ocr_ms"],
            }
            self._attach_screenshot(out, shot_img, include_screenshot, screenshot_format)
            return out

        raise ValueError(f"Unsupported navigate action: {action!r}")

    def navigate(
        self,
        action: str = "goto",
        url: str | None = None,
        tab_id: str | int | None = None,
        mode: str | None = None,
        hwnd: int | None = None,
        include_screenshot: bool = False,
        screenshot_format: str = "jpeg",
        max_elements: int = 45,
        max_text_chars: int = 2500,
        detail: str = "full",
        all_apps: bool = False,
    ) -> dict[str, Any]:
        brief = str(detail).lower() == "brief"
        with self._lock:
            if action == "list_windows":
                wins = self._list_desktop_windows()
                chosen = next((w for w in wins if w.hwnd == self.desktop_hwnd), wins[0] if wins else None)
                out_wins: dict[str, Any] = {
                    "status": "ok",
                    "target_mode": self.target_mode,
                    "desktop_hwnd": self.desktop_hwnd if self.target_mode == "os" else (self.desktop_hwnd or (chosen.hwnd if chosen else 0)),
                    "desktop_windows": self._serialize_windows(wins),
                }
                if all_apps or self.target_mode == "os":
                    out_wins["os_windows"] = self._serialize_os_windows(self._list_os_windows())
                return out_wins
            if action == "set_mode":
                resolved_mode = str(mode or tab_id or url or "managed").strip().lower()
                res = self.set_target_mode(resolved_mode, hwnd=hwnd)
                append_audit({"op": "navigate", "action": "set_mode", "mode": resolved_mode, "hwnd": hwnd})
                return {"status": "ok", **res}
            if action == "focus_window":
                self._begin_auto_step(f"切换窗口: {str(hwnd or tab_id or url or '')[:32]}")
                res = self._navigate_desktop(
                    "focus_window",
                    url,
                    tab_id,
                    include_screenshot=include_screenshot,
                    screenshot_format=screenshot_format,
                    hwnd=hwnd,
                )
                append_audit({"op": "navigate", "mode": "os", "action": "focus_window", "hwnd": res.get("hwnd"), "title": res.get("title")})
                self._emit_status("running" if self.task_state == "running" else "idle", f"已切换窗口 · {res.get('title', '')[:28]}")
                return {"status": "ok", **res}

            if action in {"goto", "new_tab", "back", "forward", "reload", "switch_tab", "close_tab"}:
                step_lbl = f"打开 {url[:42]}" if action in {"goto", "new_tab"} and url else f"导航: {action}"
                self._begin_auto_step(step_lbl)
            else:
                self._emit_status("running", f"导航中: {action}")

            if self._is_desktop_like():
                res = self._navigate_desktop(
                    action,
                    url,
                    tab_id,
                    include_screenshot=include_screenshot,
                    screenshot_format=screenshot_format,
                    hwnd=hwnd,
                )
                append_audit({"op": "navigate", "mode": self.target_mode, "action": action, "url": url, "tab_id": tab_id})
                self._emit_status("running" if self.task_state == "running" else "idle", f"导航完成 · {res.get('title', '')[:28]}")
                return {"status": "ok", **res}

            self.chrome.ensure_started()
            assert self.chrome.cdp is not None
            if action == "goto":
                if not url:
                    raise ValueError("url is required for navigate(action='goto')")
                res = self.chrome.navigate(url)
            elif action == "back":
                self.chrome.cdp.evaluate("history.back()")
                probe = self._wait_managed_settle(max_wait_s=3.0)
                res = {"url": probe.get("url", ""), "title": probe.get("title", "")}
            elif action == "forward":
                self.chrome.cdp.evaluate("history.forward()")
                probe = self._wait_managed_settle(max_wait_s=3.0)
                res = {"url": probe.get("url", ""), "title": probe.get("title", "")}
            elif action == "reload":
                self.chrome.cdp.send("Page.reload")
                probe = self._wait_managed_settle(max_wait_s=3.5)
                res = {"url": probe.get("url", ""), "title": probe.get("title", "")}
            elif action == "list_tabs":
                res = {"tabs": self.chrome.list_tabs()}
            elif action == "new_tab":
                res = self.chrome.new_tab(url or "about:blank")
            elif action == "switch_tab":
                if tab_id is None:
                    raise ValueError("tab_id is required for switch_tab")
                res = self.chrome.switch_tab(tab_id)
            elif action == "close_tab":
                res = self.chrome.close_tab(tab_id)
            else:
                raise ValueError(f"Unsupported navigate action: {action!r}")

            if isinstance(res, dict) and "url" in res:
                self.active_url = str(res.get("url", ""))
                self.active_title = str(res.get("title", ""))
            if action in {"goto", "new_tab", "back", "forward", "reload", "switch_tab"}:
                try:
                    _, raw_img, snap = self._capture_and_snap_managed(
                        max_elements=min(150, max(1, int(max_elements))), capture=bool(include_screenshot)
                    )
                    res["url"] = snap.get("url", res.get("url", ""))
                    res["title"] = snap.get("title", res.get("title", ""))
                    res["text_excerpt"] = str(snap.get("text_excerpt", ""))[: max(0, int(max_text_chars))]
                    res["elements"] = _compact_dom_elements(snap.get("elements", []))
                    res["popup_detected"] = snap.get("popup_detected", False)
                    res["auth_or_captcha_gate"] = snap.get("auth_or_captcha_gate")
                    self._attach_screenshot(res, raw_img, include_screenshot, screenshot_format)
                except Exception:
                    pass
            append_audit({"op": "navigate", "mode": "managed", "action": action, "url": url, "tab_id": tab_id})
            self._emit_status("running" if self.task_state == "running" else "idle", "导航完成")
            out_nav = {"status": "ok", **res}
            return _brief_observation(out_nav) if brief else out_nav

    def locate(self, instruction: str, zoom: bool = False) -> dict[str, Any]:
        with self._lock:
            self._emit_status("running", "正在视觉定位目标…")
            if self._is_desktop_like():
                info, raw_img = self._capture_desktop_window(focus=False)
            else:
                info, raw_img = self.chrome.capture_full_window()
            local_usage = _new_local_usage()
            infer_s = 0.0
            zoomed_used = False
            raw_text = ""
            parsed: Optional[ParsedAction] = None
            usage: dict[str, Any] | None = None
            try:
                raw_text, parsed, infer_s, usage = self.grounder.predict(raw_img, instruction)
                _accumulate_usage(local_usage, usage)
            except VLMCallError as exc:
                _accumulate_usage(local_usage, None)
                self._emit_status("error", "视觉定位模型调用失败")
                err_out: dict[str, Any] = {
                    "status": "vlm_error",
                    "reason": str(exc),
                    "raw_prediction": raw_text,
                    "action": None,
                    "coordinate": None,
                    "screen_px": None,
                    "zoomed": False,
                    "infer_s": 0.0,
                    "usage": None,
                    "local_usage": local_usage,
                }
                if self._is_desktop_like():
                    err_out["mode"] = self.target_mode
                return err_out

            if zoom and parsed is not None and parsed.coordinate is not None:
                try:
                    z_raw, z_parsed, z_s, z_usage = self.grounder.predict_zoomed(raw_img, instruction, parsed.coordinate)
                    _accumulate_usage(local_usage, z_usage)
                    raw_text, parsed, usage = z_raw, z_parsed, z_usage
                    infer_s += z_s
                    zoomed_used = True
                except VLMCallError as exc:
                    _accumulate_usage(local_usage, None)
                    self._emit_status("error", "视觉缩放定位模型调用失败")
                    screen_pt = norm_to_screen(*parsed.coordinate, info.dwm_rect) if (parsed and parsed.coordinate) else None
                    err_out = {
                        "status": "vlm_error",
                        "reason": str(exc),
                        "raw_prediction": raw_text,
                        "action": parsed.action if parsed else None,
                        "coordinate": list(parsed.coordinate) if (parsed and parsed.coordinate) else None,
                        "screen_px": list(screen_pt) if screen_pt else None,
                        "zoomed": False,
                        "infer_s": round(infer_s, 4),
                        "usage": usage,
                        "local_usage": local_usage,
                    }
                    if self._is_desktop_like():
                        err_out["mode"] = self.target_mode
                    return err_out

            screen_pt = norm_to_screen(*parsed.coordinate, info.dwm_rect) if (parsed and parsed.coordinate) else None
            self._emit_status("idle", f"定位完成 · {infer_s:.2f}s")
            out: dict[str, Any] = {
                "status": "ok" if parsed else "invalid_json",
                "raw_prediction": raw_text,
                "action": parsed.action if parsed else None,
                "coordinate": list(parsed.coordinate) if (parsed and parsed.coordinate) else None,
                "screen_px": list(screen_pt) if screen_pt else None,
                "reason": parsed.reason if parsed else None,
                "zoomed": zoomed_used,
                "infer_s": round(infer_s, 4),
                "usage": usage,
                "local_usage": local_usage,
            }
            if self._is_desktop_like():
                out["mode"] = self.target_mode
            return out

    def dismiss_popup(self) -> dict[str, Any]:
        with self._lock:
            self._begin_auto_step("尝试关闭弹窗/横幅")
            local_usage = _new_local_usage()
            vision_action: str | None = None

            if self._is_desktop_like():
                mode_tag = getattr(self, "target_mode", "desktop")
                info, raw_img = self._capture_desktop_window(focus=False)
                method = "none"
                try:
                    _raw, parsed, _inf, usage = self.grounder.predict(raw_img, "点击弹窗、广告或提示横幅的关闭按钮。")
                    _accumulate_usage(local_usage, usage)
                except VLMCallError as exc:
                    _accumulate_usage(local_usage, None)
                    append_audit({"op": "dismiss_popup", "mode": mode_tag, "method": "vision_error", "error": str(exc), "local_usage": local_usage})
                    self._emit_status("error", "弹窗识别模型调用失败")
                    return {
                        "status": "vlm_error",
                        "mode": mode_tag,
                        "method": "vision_error",
                        "reason": str(exc),
                        "popup_detected": True,
                        "local_usage": local_usage,
                    }
                except Exception as exc:
                    append_audit({"op": "dismiss_popup", "mode": mode_tag, "method": "vision_error", "error": str(exc), "local_usage": local_usage})
                    self._emit_status("error", "弹窗关闭尝试失败")
                    return {
                        "status": "exec_failed",
                        "mode": mode_tag,
                        "method": "vision_error",
                        "reason": str(exc),
                        "popup_detected": True,
                        "local_usage": local_usage,
                    }

                vision_action = parsed.action if parsed is not None else "invalid_json"
                if parsed is not None and parsed.action == "left_click":
                    try:
                        with self._desktop_io_guard():
                            if check_corner_failsafe():
                                raise RuntimeError("failsafe_corner_triggered")
                            focused = self._focus_and_verify_desktop(info, settle_s=0.16)
                            ok, _ = execute_action(parsed, focused.hwnd, focused.dwm_rect)
                    except RuntimeError as exc:
                        append_audit({"op": "dismiss_popup", "mode": mode_tag, "method": "vision_exec_failed", "error": str(exc), "local_usage": local_usage})
                        self._emit_status("error", "关闭弹窗前窗口聚焦失败")
                        return {
                            "status": "exec_failed",
                            "mode": mode_tag,
                            "method": "vision_exec_failed",
                            "vision_action": vision_action,
                            "reason": str(exc),
                            "popup_detected": True,
                            "local_usage": local_usage,
                        }
                    if ok:
                        method = "vision_close_click"
                        time.sleep(0.3)

                after_info, after_img = self._capture_desktop_window(focus=False)
                if after_info.title == info.title and _frame_changed_fraction(raw_img, after_img) <= 0.002:
                    try:
                        with self._desktop_io_guard():
                            if check_corner_failsafe():
                                raise RuntimeError("failsafe_corner_triggered")
                            self._focus_and_verify_desktop(after_info, settle_s=0.14)
                            send_hotkey(["escape"])
                        method = f"{method}+escape" if method != "none" else "escape"
                        time.sleep(0.25)
                    except RuntimeError as exc:
                        append_audit({"op": "dismiss_popup", "mode": mode_tag, "method": f"{method}+escape_failed", "error": str(exc), "local_usage": local_usage})
                        self._emit_status("error", "Escape 关闭弹窗前聚焦失败")
                        return {
                            "status": "exec_failed",
                            "mode": mode_tag,
                            "method": f"{method}+escape_failed" if method != "none" else "escape_failed",
                            "vision_action": vision_action,
                            "reason": str(exc),
                            "popup_detected": True,
                            "local_usage": local_usage,
                        }

                append_audit({"op": "dismiss_popup", "mode": mode_tag, "method": method, "vision_action": vision_action, "local_usage": local_usage})
                self._emit_status("running" if self.task_state == "running" else "idle", "已尝试关闭弹窗")
                return {
                    "status": "ok",
                    "mode": mode_tag,
                    "method": method,
                    "vision_action": vision_action,
                    "popup_detected": False,
                    "local_usage": local_usage,
                }

            info, raw_img = self.chrome.capture_full_window()
            w_dwm, h_dwm = raw_img.size
            snap = self.chrome.extract_dom_snapshot(info, w_dwm, h_dwm, max_elements=60)
            method = "none"
            close_ids = snap.get("popup_close_ids", [])
            if close_ids:
                target_el = next((e for e in snap["elements"] if e["id"] == close_ids[0]), None)
                if target_el is not None:
                    self.chrome.click_dom_element(target_el["rect_css"])
                    method = f"dom_close_id:{close_ids[0]}"
                    time.sleep(0.3)
            if method == "none":
                try:
                    _raw, parsed, _inf, usage = self.grounder.predict(raw_img, "点击弹窗、广告或提示横幅的关闭按钮。")
                    _accumulate_usage(local_usage, usage)
                except VLMCallError as exc:
                    _accumulate_usage(local_usage, None)
                    append_audit({"op": "dismiss_popup", "mode": "managed", "method": "vision_error", "error": str(exc), "local_usage": local_usage})
                    self._emit_status("error", "弹窗识别模型调用失败")
                    return {
                        "status": "vlm_error",
                        "method": "vision_error",
                        "reason": str(exc),
                        "popup_detected": bool(snap.get("popup_detected")),
                        "local_usage": local_usage,
                    }
                except Exception as exc:
                    append_audit({"op": "dismiss_popup", "mode": "managed", "method": "vision_error", "error": str(exc), "local_usage": local_usage})
                    self._emit_status("error", "弹窗识别前准备失败")
                    return {
                        "status": "exec_failed",
                        "method": "vision_error",
                        "reason": str(exc),
                        "popup_detected": bool(snap.get("popup_detected")),
                        "local_usage": local_usage,
                    }
                vision_action = parsed.action if parsed is not None else "invalid_json"
                if parsed is not None and parsed.action == "left_click":
                    ok, _ = self.chrome.dispatch_viewport_action(parsed, info, raw_img.size)
                    if ok:
                        method = "vision_close_click"
                        time.sleep(0.3)
            after_info, after_img = self.chrome.capture_full_window()
            after_snap = self.chrome.extract_dom_snapshot(after_info, *after_img.size, max_elements=40)
            if after_snap.get("popup_detected"):
                self.chrome.send_key(["escape"])
                method += "+escape"
                time.sleep(0.25)
                after_info, after_img = self.chrome.capture_full_window()
                after_snap = self.chrome.extract_dom_snapshot(after_info, *after_img.size, max_elements=40)

            cleared = not bool(after_snap.get("popup_detected"))
            append_audit({"op": "dismiss_popup", "mode": "managed", "method": method, "cleared": cleared, "vision_action": vision_action, "local_usage": local_usage})
            self._emit_status("running" if self.task_state == "running" else "idle", "已关闭弹窗" if cleared else "弹窗尝试关闭后仍存在")
            out_dis: dict[str, Any] = {
                "status": "ok" if cleared else "still_present",
                "method": method,
                "popup_detected": not cleared,
                "local_usage": local_usage,
            }
            if vision_action is not None:
                out_dis["vision_action"] = vision_action
            return out_dis

    def extract(self, selector: str | None = None, max_chars: int = 4000) -> dict[str, Any]:
        with self._lock:
            if self._is_desktop_like():
                info, raw_img = self._capture_desktop_window(focus=False)
                perc = self._perceive_desktop(info, raw_img, max_elements=80, max_chars=max_chars)
                out_ext: dict[str, Any] = {
                    "status": "ok",
                    "mode": "desktop_vision" if self.target_mode == "desktop" else "os",
                    "hwnd": info.hwnd,
                    "url": perc["url"],
                    "title": info.title,
                    "selector_found": False,
                    "text": perc["page_text"],
                    "ocr_elements": _compact_ocr_elements(perc["ocr_elements"]),
                    "ocr_ms": perc["ocr_ms"],
                    "links": [],
                    "note": (
                        "桌面 Chrome 视觉模式已通过本地 WinRT OCR 提取页面文字与元素坐标；如需图像细节判断可调用 cua_ask_screen，或切至 managed 模式提取 DOM。"
                        if self.target_mode == "desktop"
                        else "全桌面 OS 视觉模式已通过本地 WinRT OCR 提取当前画面文字与元素坐标；如需图像细节判断可调用 cua_ask_screen。"
                    ),
                }
                if self.target_mode == "os":
                    out_ext["process"] = perc.get("process", "")
                return out_ext

            self.chrome.ensure_started()
            assert self.chrome.cdp is not None
            sel_json = json.dumps(selector)
            data = self.chrome.cdp.evaluate(
                f"""(() => {{
                  const sel = {sel_json};
                  function cleanText(s) {{ return (s || '').replace(/\\s+/g, ' ').trim(); }}
                  const allRoots = [document];
                  const seenRoots = new Set([document]);
                  function collectRoots(r, depth = 0) {{
                    if (!r || depth > 8 || !r.querySelectorAll) return;
                    for (const el of r.querySelectorAll('*')) {{
                      if (el.shadowRoot && !seenRoots.has(el.shadowRoot)) {{
                        seenRoots.add(el.shadowRoot);
                        allRoots.push(el.shadowRoot);
                        collectRoots(el.shadowRoot, depth + 1);
                      }}
                    }}
                  }}
                  collectRoots(document, 0);
                  let root = sel ? document.querySelector(sel) : document.body;
                  if (sel && !root) {{
                    for (const r of allRoots) {{
                      root = r.querySelector(sel);
                      if (root) break;
                    }}
                  }}
                  const chunks = [];
                  if (root && root.innerText) chunks.push(root.innerText);
                  for (const r of allRoots) {{
                    if (r === document) continue;
                    if (!sel || (root && r.host && (root === r.host || root.contains(r.host)))) {{
                      for (const child of r.children) {{
                        if (child.tagName === 'STYLE' || child.tagName === 'SCRIPT' || child.tagName === 'LINK') continue;
                        const ct = cleanText(child.innerText || child.textContent || '');
                        if (ct) chunks.push(ct);
                      }}
                    }}
                  }}
                  const text = cleanText(chunks.join(' '));
                  const links = [];
                  const linkRoots = sel ? [root].filter(Boolean) : allRoots;
                  for (const lr of linkRoots) {{
                    for (const a of lr.querySelectorAll('a[href]')) {{
                      if (links.length >= 25) break;
                      const t = cleanText(a.innerText || '');
                      if (t) links.push({{ text: t.slice(0, 60), href: a.href }});
                    }}
                    if (links.length >= 25) break;
                  }}
                  return {{
                    url: location.href,
                    title: document.title,
                    selector_found: Boolean(root),
                    text: text.slice(0, {int(max_chars)}),
                    links,
                  }};
                }})()"""
            )
            self.active_url = str(data.get("url", ""))
            self.active_title = str(data.get("title", ""))
            return {"status": "ok", **data}

    def ask_screen(
        self,
        question: str,
        around_last_click: bool = False,
        include_screenshot: bool = False,
        screenshot_format: str = "jpeg",
    ) -> dict[str, Any]:
        """Ask the local M3.5 VLM to read/verify something on screen and return pure text."""
        if not question or not str(question).strip():
            raise ValueError("question is required for ask_screen")
        with self._lock:
            self._begin_auto_step(f"本地读屏: {str(question).strip()[:28]}")
            if self._is_desktop_like():
                info, raw_img = self._capture_desktop_window(focus=False)
                perc = self._perceive_desktop(info, raw_img, max_elements=60, max_chars=1200)
                ocr_ctx = perc["page_text"]
                url = perc["url"]
                title = info.title
            else:
                info, raw_img, snap = self._capture_and_snap_managed(max_elements=40)
                ocr_ctx = str(snap.get("text_excerpt", ""))[:1200]
                url = str(snap.get("url", ""))
                title = str(snap.get("title", ""))

            crop_center = self._last_click_coord if (around_last_click and self._last_click_coord is not None) else None
            local_usage = _new_local_usage()
            try:
                answer, infer_s, usage = self.grounder.ask_screen(
                    raw_img,
                    str(question),
                    ocr_context=ocr_ctx,
                    crop_center=crop_center,
                )
                _accumulate_usage(local_usage, usage)
            except VLMCallError as exc:
                _accumulate_usage(local_usage, None)
                append_audit({
                    "op": "ask_screen",
                    "mode": self.target_mode,
                    "used_zoom_crop": bool(crop_center),
                    "error": str(exc),
                    "local_usage": local_usage,
                })
                self._emit_status("error", "读屏模型调用失败")
                return {
                    "status": "vlm_error",
                    "mode": self.target_mode,
                    "url": url,
                    "title": title,
                    "used_zoom_crop": bool(crop_center),
                    "reason": str(exc),
                    "answer": "",
                    "infer_s": 0.0,
                    "local_usage": local_usage,
                }
            append_audit({
                "op": "ask_screen",
                "mode": self.target_mode,
                "used_zoom_crop": bool(crop_center),
                "infer_s": round(infer_s, 4),
                "local_usage": local_usage,
            })
            self._emit_status("running" if self.task_state == "running" else "idle", f"读屏完成 · {infer_s:.2f}s")
            out: dict[str, Any] = {
                "status": "ok",
                "mode": self.target_mode,
                "url": url,
                "title": title,
                "used_zoom_crop": bool(crop_center),
                "answer": answer,
                "infer_s": round(infer_s, 4),
                "local_usage": local_usage,
            }
            if self._last_click_effect is not None:
                out["last_click_effect"] = self._last_click_effect
            self._attach_screenshot(out, raw_img, include_screenshot, screenshot_format)
            return out

    def _act_desktop(
        self,
        instruction: str | None,
        element_id: int | None,
        action_json: dict[str, Any] | str | None,
        strategy: str,
        auto_heal: bool,
        confirm_token: str | None,
        include_screenshot: bool = False,
        screenshot_format: str = "jpeg",
        ocr_id: int | None = None,
        text_hint: str | None = None,
        wait_settle: bool = False,
        include_text: bool = False,
        _extra_policy_text: str = "",
    ) -> dict[str, Any]:
        mode_tag = getattr(self, "target_mode", "desktop")
        if element_id is not None and ocr_id is None and not text_hint and not instruction and action_json is None:
            return {
                "status": "not_found",
                "mode": mode_tag,
                "reason": f"{mode_tag} 模式无 DOM element_id，请提供 ocr_id、text_hint、instruction 或 action_json",
            }
        t_start = time.perf_counter()
        info, raw_img = self._capture_desktop_window(focus=False)
        self.active_title = info.title
        gate = self._check_desktop_title_gate(info.title)
        base_inst = instruction or text_hint or (json.dumps(action_json, ensure_ascii=False) if action_json else "")
        inst_str = " ".join(x for x in (base_inst, _extra_policy_text) if x).strip()
        policy = evaluate_policy(inst_str, element_meta=None, gate_reason=gate)
        if policy.tier == "takeover":
            append_audit({"op": "act", "mode": mode_tag, "blocked": "takeover", "reason": policy.matched_rule, "title": info.title})
            self.task_state = "confirm"
            self.task_step_label = policy.reason[:60]
            self._emit_status("confirm", policy.reason, event_kind="task_alert", play_sound=True)
            return {
                "status": "needs_human_takeover",
                "mode": mode_tag,
                "reason": policy.reason,
                "gate": policy.matched_rule,
                "url": self.active_url,
                "title": info.title,
            }
        pre_confirmed = self._peek_confirm_token(confirm_token, inst_str, element_id, ocr_id=ocr_id)
        if policy.tier == "red" and pre_confirmed is None:
            tok = self._make_confirm_token(inst_str, element_id, ocr_id=ocr_id, matched_rule=policy.matched_rule)
            append_audit({"op": "act", "mode": mode_tag, "blocked": "red_tier", "rule": policy.matched_rule, "instruction": inst_str})
            self.task_state = "confirm"
            self.task_step_label = policy.reason[:60]
            self._emit_status("confirm", policy.reason, event_kind="task_alert", play_sound=True)
            return {
                "status": "needs_human_confirmation",
                "mode": mode_tag,
                "confirm_token": tok,
                "reason": policy.reason,
                "matched_rule": policy.matched_rule,
            }

        infer_s = 0.0
        auto_healed = False
        raw_text = ""
        grounding_source = "m35_vision"
        local_usage = _new_local_usage()
        parsed: Optional[ParsedAction] = None
        resolved_ocr_el: dict[str, Any] | None = None

        if action_json is not None:
            grounding_source = "action_json"
            raw_text = _normalize_action_json(action_json)
            parsed = parse_and_validate_action(raw_text)
        else:
            if ocr_id is not None:
                hit = next((e for e in self._last_ocr_elements if int(e.get("id", -1)) == int(ocr_id)), None)
                if hit is None:
                    perc_now = self._perceive_desktop(info, raw_img, max_elements=60, max_chars=2400)
                    hit = next((e for e in perc_now["ocr_elements"] if int(e.get("id", -1)) == int(ocr_id)), None)
                if hit is not None:
                    cx, cy = int(hit["center_norm"][0]), int(hit["center_norm"][1])
                    raw_text = json.dumps({"action": "left_click", "coordinate": [cx, cy], "ocr_id": ocr_id}, ensure_ascii=False)
                    parsed = ParsedAction(action="left_click", coordinate=(cx, cy), raw=raw_text)
                    grounding_source = "ocr_id"
                    resolved_ocr_el = hit
                elif not instruction:
                    return {
                        "status": "not_found",
                        "mode": mode_tag,
                        "reason": f"未在当前画面找到 ocr_id={ocr_id}",
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }

            if parsed is None and (text_hint or (strategy == "ocr" and instruction)) and strategy in {"hybrid", "ocr"}:
                query_lbl = str(text_hint or instruction or "")
                perc_now = self._perceive_desktop(info, raw_img, max_elements=60, max_chars=2400)
                gate = self._check_desktop_title_gate(info.title, perc_now["page_text"])
                if gate:
                    return {
                        "status": "needs_human_takeover",
                        "mode": mode_tag,
                        "reason": f"检测到人机验证或登录拦截（{gate}），需人工接管。",
                        "gate": gate,
                        "url": perc_now["url"],
                        "title": info.title,
                    }
                hit_match = match_ocr_target(query_lbl, perc_now["ocr_elements"])
                if hit_match is not None:
                    el_ocr = hit_match["element"]
                    cx, cy = int(el_ocr["center_norm"][0]), int(el_ocr["center_norm"][1])
                    raw_text = json.dumps(
                        {"action": "left_click", "coordinate": [cx, cy], "ocr_text": el_ocr["text"], "score": hit_match["score"]},
                        ensure_ascii=False,
                    )
                    parsed = ParsedAction(action="left_click", coordinate=(cx, cy), raw=raw_text)
                    grounding_source = "ocr_hint"
                    resolved_ocr_el = el_ocr
                elif strategy == "ocr" or not instruction:
                    return {
                        "status": "not_found",
                        "mode": mode_tag,
                        "reason": f"OCR 未找到唯一匹配目标「{query_lbl}」",
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }

            if parsed is None:
                if not instruction:
                    raise ValueError("Provide instruction, text_hint, ocr_id, or action_json in desktop/os mode")
                grounding_source = "m35_vision"
                try:
                    raw_text, parsed, infer_s, usage = self.grounder.predict(raw_img, instruction)
                    _accumulate_usage(local_usage, usage)
                except VLMCallError as exc:
                    _accumulate_usage(local_usage, None)
                    self._emit_status("error", "本地模型调用失败", event_kind="status", play_sound=False)
                    return {
                        "status": "vlm_error",
                        "mode": mode_tag,
                        "reason": str(exc),
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }
                if self.abort_event.is_set():
                    self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                    return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                if parsed is None:
                    self._emit_status("error", "模型输出格式无效", event_kind="status", play_sound=False)
                    return {"status": "invalid_json", "mode": mode_tag, "raw_prediction": raw_text, "local_usage": local_usage, "elapsed_s": round(time.perf_counter() - t_start, 4)}
                if parsed.action == "not_found":
                    self.step_count += 1
                    append_audit({"op": "act", "mode": mode_tag, "action": "not_found", "instruction": instruction, "reason": parsed.reason})
                    self._emit_status("running" if self.task_state == "running" else "idle", "未在当前画面找到目标")
                    return {
                        "status": "not_found",
                        "mode": mode_tag,
                        "reason": parsed.reason,
                        "raw_prediction": raw_text,
                        "infer_s": round(infer_s, 4),
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }

        _el_pol, hit_ocr = self.evaluate_desktop_action_policy(
            base_inst, info, raw_img, parsed, resolved_ocr_el=resolved_ocr_el
        )
        blocked_post = self._verify_resolved_target_policy(
            policy_inst=inst_str,
            element_id=element_id,
            ocr_id=ocr_id,
            confirm_token=confirm_token,
            pre_confirmed=pre_confirmed,
            resolved_meta=hit_ocr,
            coordinate=parsed.coordinate if parsed is not None else None,
            mode_tag=mode_tag,
            local_usage=local_usage,
        )
        if blocked_post is not None:
            return blocked_post
        token_consumed = True

        try:
            with self._desktop_io_guard():
                if self.abort_event.is_set():
                    self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                    return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                if check_corner_failsafe():
                    self.task_state = "error"
                    self._emit_status("error", "已急停（光标在左上角安全区）", event_kind="task_error", play_sound=True)
                    return {
                        "status": "exec_failed",
                        "mode": mode_tag,
                        "exec_status": "failsafe_corner_triggered",
                        "action": parsed.action,
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }
                focused = self._focus_and_verify_desktop(info, settle_s=0.16)
                if focused.dwm_rect != info.dwm_rect:
                    if focused.hwnd == 0:
                        raw_img = capture_roi(focused.dwm_rect)
                    else:
                        try:
                            raw_img = print_window_dwm(focused)
                            if self.target_mode == "os" and int(np.asarray(raw_img).max()) == 0:
                                raise RuntimeError("print_window_blank")
                        except Exception:
                            raw_img = capture_roi(focused.dwm_rect) if self.target_mode == "os" else capture_chrome_window(focused)
                    info = focused
                    if instruction and action_json is None and grounding_source == "m35_vision":
                        try:
                            raw_text, parsed, re_inf, re_usage = self.grounder.predict(raw_img, instruction)
                            _accumulate_usage(local_usage, re_usage)
                            infer_s += re_inf
                        except VLMCallError as exc:
                            _accumulate_usage(local_usage, None)
                            self._emit_status("error", "重拍后本地模型调用失败")
                            return {
                                "status": "vlm_error",
                                "mode": mode_tag,
                                "reason": str(exc),
                                "local_usage": local_usage,
                                "elapsed_s": round(time.perf_counter() - t_start, 4),
                            }
                        if self.abort_event.is_set():
                            self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                            return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                        if parsed is None:
                            self._emit_status("error", "模型输出格式无效")
                            return {"status": "invalid_json", "mode": mode_tag, "raw_prediction": raw_text, "local_usage": local_usage, "elapsed_s": round(time.perf_counter() - t_start, 4)}
                        if parsed.action == "not_found":
                            self.step_count += 1
                            self._emit_status("running" if self.task_state == "running" else "idle", "未在当前画面找到目标")
                            return {
                                "status": "not_found",
                                "mode": mode_tag,
                                "reason": parsed.reason,
                                "raw_prediction": raw_text,
                                "infer_s": round(infer_s, 4),
                                "local_usage": local_usage,
                                "elapsed_s": round(time.perf_counter() - t_start, 4),
                            }
                        _re_pol, re_hit_ocr = self.evaluate_desktop_action_policy(base_inst, info, raw_img, parsed)
                        blocked_re = self._verify_resolved_target_policy(
                            policy_inst=inst_str,
                            element_id=element_id,
                            ocr_id=ocr_id,
                            confirm_token=None,
                            pre_confirmed=pre_confirmed,
                            resolved_meta=re_hit_ocr,
                            coordinate=parsed.coordinate,
                            mode_tag=mode_tag,
                            local_usage=local_usage,
                        )
                        if blocked_re is not None:
                            return blocked_re
                if self.abort_event.is_set():
                    self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                    return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                exec_ok, exec_status = execute_action(parsed, focused.hwnd, focused.dwm_rect)
        except RuntimeError as exc:
            self._emit_status("error", "目标窗口前景聚焦失败" if mode_tag == "os" else "桌面 Chrome 前景聚焦失败")
            return {
                "status": "exec_failed",
                "mode": mode_tag,
                "exec_status": str(exc),
                "reason": str(exc),
                "action": parsed.action if parsed else None,
                "local_usage": local_usage,
                "elapsed_s": round(time.perf_counter() - t_start, 4),
            }

        click_effect: dict[str, Any] | None = None
        after_info: ChromeWindowInfo | None = None
        after_img: Image.Image | None = None
        if exec_ok and parsed.action == "left_click" and parsed.coordinate is not None:
            time.sleep(0.22)
            after_info, after_img = self._capture_desktop_window(focus=False)
            click_effect = analyze_click_effect(
                raw_img,
                after_img,
                parsed.coordinate,
                title_before=info.title,
                title_after=after_info.title,
            )
            if (not click_effect["effective"]) and auto_heal and instruction and action_json is None:
                try:
                    raw_z, parsed_z, inf_z, uz = self.grounder.predict_zoomed(raw_img, instruction, parsed.coordinate)
                    _accumulate_usage(local_usage, uz)
                    infer_s += inf_z
                except VLMCallError:
                    _accumulate_usage(local_usage, None)
                    parsed_z = None
                if self.abort_event.is_set():
                    self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                    return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                if parsed_z is not None and parsed_z.action == "left_click" and parsed_z.coordinate is not None:
                    _z_pol, z_hit_ocr = self.evaluate_desktop_action_policy(base_inst, after_info, after_img, parsed_z)
                    blocked_z = self._verify_resolved_target_policy(
                        policy_inst=inst_str,
                        element_id=element_id,
                        ocr_id=ocr_id,
                        confirm_token=None,
                        pre_confirmed=None,
                        resolved_meta=z_hit_ocr,
                        coordinate=parsed_z.coordinate,
                        mode_tag=mode_tag,
                        local_usage=local_usage,
                    )
                    if blocked_z is not None:
                        return blocked_z
                    try:
                        with self._desktop_io_guard():
                            if self.abort_event.is_set():
                                self._emit_status("idle", "已中止执行", event_kind="status", play_sound=False)
                                return {"status": "aborted", "mode": mode_tag, "reason": "aborted_by_user", "local_usage": local_usage}
                            if not check_corner_failsafe():
                                focused2 = self._focus_and_verify_desktop(after_info, settle_s=0.16)
                                exec_ok, exec_status = execute_action(parsed_z, focused2.hwnd, focused2.dwm_rect)
                                parsed = parsed_z
                                raw_text = raw_z
                                auto_healed = True
                        time.sleep(0.22)
                        after_info, after_img = self._capture_desktop_window(focus=False)
                        click_effect = analyze_click_effect(
                            raw_img,
                            after_img,
                            parsed.coordinate,
                            title_before=info.title,
                            title_after=after_info.title,
                        )
                    except RuntimeError:
                        pass
            self._last_click_coord = (int(parsed.coordinate[0]), int(parsed.coordinate[1]))
            self._last_click_effect = click_effect

        settle_perc: dict[str, Any] | None = None
        if wait_settle:
            post_info, post_img, settle_perc = self._wait_desktop_frame_settle(
                focused.hwnd,
                before_img=raw_img,
                old_title=info.title,
                min_wait_s=0.25,
                max_wait_s=4.0,
            )
        elif after_info is not None and after_img is not None:
            post_info, post_img = after_info, after_img
            time.sleep(0.05)
        elif include_screenshot or include_text:
            time.sleep(0.18)
            post_info, post_img = self._capture_desktop_window(focus=False)
        else:
            time.sleep(0.12)
            post_info = self._resolve_desktop_window(focus=False)
            post_img = None

        perc_post: dict[str, Any] | None = None
        if (include_text or wait_settle) and post_img is not None:
            perc_post = settle_perc or self._perceive_desktop(post_info, post_img, max_elements=50, max_chars=2000)

        self.active_title = post_info.title
        self.step_count += 1
        elapsed = time.perf_counter() - t_start
        append_audit({
            "op": "act",
            "mode": mode_tag,
            "instruction": instruction,
            "grounding_source": grounding_source,
            "action": parsed.action if parsed else exec_status,
            "exec_ok": exec_ok,
            "auto_healed": auto_healed,
            "elapsed_s": round(elapsed, 4),
        })
        step_msg = f"步骤#{self.task_step} {'已执行' if exec_ok else '未生效'} · {elapsed:.2f}s"
        self._emit_status("running" if exec_ok and self.task_state == "running" else ("idle" if exec_ok else "error"), step_msg)
        out: dict[str, Any] = {
            "status": "ok" if exec_ok else "exec_failed",
            "mode": mode_tag,
            "hwnd": post_info.hwnd,
            "grounding_source": grounding_source,
            "exec_status": exec_status,
            "action": parsed.action if parsed else exec_status,
            "coordinate": list(parsed.coordinate) if (parsed and parsed.coordinate) else None,
            "auto_healed": auto_healed,
            "url": perc_post["url"] if perc_post else self.active_url,
            "title": self.active_title,
            "popup_detected": False,
            "infer_s": round(infer_s, 4),
            "local_usage": local_usage,
            "elapsed_s": round(elapsed, 4),
        }
        if raw_text:
            out["raw_prediction"] = raw_text
        if click_effect is not None:
            out["click_effect"] = click_effect
        if perc_post is not None:
            out["page_text"] = perc_post["page_text"]
            out["ocr_elements"] = _compact_ocr_elements(perc_post["ocr_elements"])
        self._attach_screenshot(out, post_img, include_screenshot, screenshot_format)
        return out

    def act(
        self,
        instruction: str | None = None,
        element_id: int | None = None,
        action_json: dict[str, Any] | str | None = None,
        strategy: str = "hybrid",
        auto_heal: bool = True,
        confirm_token: str | None = None,
        include_screenshot: bool = False,
        screenshot_format: str = "jpeg",
        ocr_id: int | None = None,
        text_hint: str | None = None,
        wait_settle: bool = False,
        include_text: bool = False,
        scroll_retry: int = 0,
        _extra_policy_text: str = "",
    ) -> dict[str, Any]:
        if self.abort_event.is_set():
            return {"status": "aborted", "reason": "aborted_by_user"}
        with self._lock:
            if self.abort_event.is_set():
                return {"status": "aborted", "reason": "aborted_by_user"}
            step_lbl = (
                instruction
                or text_hint
                or (f"OCR#{ocr_id}" if ocr_id is not None else None)
                or (f"元素#{element_id}" if element_id is not None else f"动作:{(action_json or {}).get('action', '') if isinstance(action_json, dict) else 'exec'}")
            )
            self._begin_auto_step(step_lbl)
            if self._is_desktop_like():
                return self._act_desktop(
                    instruction,
                    element_id,
                    action_json,
                    strategy,
                    auto_heal,
                    confirm_token,
                    include_screenshot=include_screenshot,
                    screenshot_format=screenshot_format,
                    ocr_id=ocr_id,
                    text_hint=text_hint,
                    wait_settle=wait_settle,
                    include_text=include_text,
                    _extra_policy_text=_extra_policy_text,
                )

            t_start = time.perf_counter()
            info, raw_img = self.chrome.capture_full_window()
            w_dwm, h_dwm = raw_img.size
            snap = self.chrome.extract_dom_snapshot(info, w_dwm, h_dwm, max_elements=60)
            self.active_url = snap.get("url", "")
            self.active_title = snap.get("title", "")

            target_el = next((e for e in snap["elements"] if e["id"] == element_id), None) if element_id is not None else None
            if target_el is None and element_id is None and text_hint and strategy in {"hybrid", "dom"}:
                dom_candidates = [
                    {
                        "id": e["id"],
                        "text": e.get("text") or e.get("aria") or e.get("placeholder") or "",
                        "el": e,
                    }
                    for e in snap.get("elements", [])
                    if not e.get("disabled")
                ]
                dom_hit = match_ocr_target(text_hint, dom_candidates)
                if dom_hit is not None:
                    target_el = dom_hit["element"]["el"]

            base_inst = instruction or text_hint or (json.dumps(action_json, ensure_ascii=False) if action_json else "")
            inst_for_policy = " ".join(x for x in (base_inst, _extra_policy_text) if x).strip()
            policy = evaluate_policy(
                inst_for_policy,
                element_meta=target_el,
                gate_reason=snap.get("auth_or_captcha_gate"),
            )
            if policy.tier == "takeover":
                append_audit({"op": "act", "blocked": "takeover", "reason": policy.matched_rule, "url": self.active_url})
                self.task_state = "confirm"
                self.task_step_label = policy.reason[:60]
                self._emit_status("confirm", policy.reason, event_kind="task_alert", play_sound=True)
                return {
                    "status": "needs_human_takeover",
                    "reason": policy.reason,
                    "gate": policy.matched_rule,
                    "url": self.active_url,
                }
            pre_confirmed = self._peek_confirm_token(confirm_token, inst_for_policy, element_id, ocr_id=ocr_id)
            if policy.tier == "red":
                if target_el is not None:
                    req_sig = self._confirm_target_sig(target_el, policy.matched_rule)
                    matched = pre_confirmed is not None and (
                        pre_confirmed[0] == req_sig
                        or (pre_confirmed[0] == "" and pre_confirmed[1] == policy.matched_rule)
                    )
                    if matched and confirm_token:
                        self._pending_confirms.pop(confirm_token, None)
                    else:
                        tok = self._make_confirm_token(
                            inst_for_policy,
                            element_id,
                            ocr_id=ocr_id,
                            target_sig=req_sig,
                            matched_rule=policy.matched_rule,
                        )
                        append_audit({"op": "act", "blocked": "red_tier", "rule": policy.matched_rule, "instruction": inst_for_policy})
                        self.task_state = "confirm"
                        self.task_step_label = policy.reason[:60]
                        self._emit_status("confirm", policy.reason, event_kind="task_alert", play_sound=True)
                        return {
                            "status": "needs_human_confirmation",
                            "confirm_token": tok,
                            "reason": policy.reason,
                            "matched_rule": policy.matched_rule,
                        }
                elif pre_confirmed is None:
                    tok = self._make_confirm_token(
                        inst_for_policy,
                        element_id,
                        ocr_id=ocr_id,
                        matched_rule=policy.matched_rule,
                    )
                    append_audit({"op": "act", "blocked": "red_tier", "rule": policy.matched_rule, "instruction": inst_for_policy})
                    self.task_state = "confirm"
                    self.task_step_label = policy.reason[:60]
                    self._emit_status("confirm", policy.reason, event_kind="task_alert", play_sound=True)
                    return {
                        "status": "needs_human_confirmation",
                        "confirm_token": tok,
                        "reason": policy.reason,
                        "matched_rule": policy.matched_rule,
                    }

            infer_s = 0.0
            auto_healed = False
            raw_text = ""
            grounding_source = "m35_vision"
            local_usage = _new_local_usage()
            parsed: Optional[ParsedAction] = None

            if target_el is not None:
                if pre_confirmed is not None and confirm_token:
                    self._pending_confirms.pop(confirm_token, None)
                if self.abort_event.is_set():
                    return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                grounding_source = "dom_id" if element_id is not None else "dom_hint"
                text_to_type = None
                if isinstance(action_json, dict) and action_json.get("text"):
                    text_to_type = str(action_json["text"])
                elif instruction and any(k in instruction for k in ("输入", "填写", "type")):
                    m = re.search(r"[「『\"']([^」』\"']+)[」』\"']\s*$", instruction)
                    if m:
                        text_to_type = m.group(1)
                rn = target_el.get("rect_norm") or [0, 0, 0, 0]
                self._last_click_coord = ((rn[0] + rn[2]) // 2, (rn[1] + rn[3]) // 2)
                exec_ok, exec_status = self.chrome.click_dom_element(target_el["rect_css"], text_to_type=text_to_type)
            elif element_id is not None:
                self._emit_status("running" if self.task_state == "running" else "idle", f"未找到元素 #{element_id}")
                return {"status": "not_found", "reason": f"Element id {element_id} not visible in current frame", "local_usage": local_usage}
            elif action_json is not None:
                if self.abort_event.is_set():
                    return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                grounding_source = "action_json"
                raw_text = _normalize_action_json(action_json)
                parsed = parse_and_validate_action(raw_text)
                hit_el = None
                if parsed.action in {"left_click", "double_click", "right_click", "type"} and parsed.coordinate is not None:
                    cx, cy = parsed.coordinate
                    hit_el = next(
                        (
                            e
                            for e in snap.get("elements", [])
                            if e["rect_norm"][0] <= cx <= e["rect_norm"][2] and e["rect_norm"][1] <= cy <= e["rect_norm"][3]
                        ),
                        None,
                    )
                blocked_aj = self._verify_resolved_target_policy(
                    policy_inst=inst_for_policy,
                    element_id=element_id,
                    ocr_id=ocr_id,
                    confirm_token=confirm_token,
                    pre_confirmed=pre_confirmed,
                    resolved_meta=hit_el,
                    coordinate=parsed.coordinate,
                    local_usage=local_usage,
                )
                if blocked_aj is not None:
                    return blocked_aj
                exec_ok, exec_status = self.chrome.dispatch_viewport_action(parsed, info, raw_img.size)
            else:
                if not instruction:
                    if text_hint:
                        return {"status": "not_found", "reason": f"未在当前页面找到「{text_hint}」", "local_usage": local_usage}
                    raise ValueError("Provide at least one of instruction, element_id, text_hint, or action_json")
                scroll_find_attempts = 0
                scroll_sem = bool(instruction and re.search(r"滚动|下滑|上滑|翻页|scroll|swipe", instruction, re.I))
                find_sem = bool(instruction and re.search(r"找|搜|滚动|翻|scroll|find|search", instruction, re.I))
                remaining_scroll = max(0, int(scroll_retry))
                if remaining_scroll == 0 and find_sem and not scroll_sem:
                    remaining_scroll = 2  # instruction implies searching — allow scroll-and-retry
                chain_budget = 2  # model-initiated scroll chaining (M4 web_scroll_find loop)

                def _scroll_state_y() -> tuple[float, float]:
                    try:
                        st_raw = self.chrome.cdp.evaluate(
                            "JSON.stringify({y:window.scrollY||document.documentElement.scrollTop||0,max:Math.max(0,(document.documentElement.scrollHeight||0)-window.innerHeight)})"
                        )
                        st = json.loads(st_raw) if isinstance(st_raw, str) else {}
                        return float(st.get("y", 0.0)), float(st.get("max", 0.0))
                    except Exception:
                        return 0.0, 0.0

                def _scroll_and_recapture(scroll_action) -> bool:
                    nonlocal info, raw_img, snap, w_dwm, h_dwm
                    self.chrome.dispatch_viewport_action(scroll_action, info, raw_img.size)
                    time.sleep(0.35)
                    info, raw_img = self.chrome.capture_full_window()
                    w_dwm, h_dwm = raw_img.size
                    snap = self.chrome.extract_dom_snapshot(info, w_dwm, h_dwm, max_elements=60)

                while True:
                    if self.abort_event.is_set():
                        return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                    try:
                        raw_text, parsed, infer_s, usage = self.grounder.predict(raw_img, instruction)
                        _accumulate_usage(local_usage, usage)
                    except VLMCallError as exc:
                        _accumulate_usage(local_usage, None)
                        self._emit_status("error", "本地模型调用失败")
                        return {
                            "status": "vlm_error",
                            "reason": str(exc),
                            "local_usage": local_usage,
                            "elapsed_s": round(time.perf_counter() - t_start, 4),
                        }
                    if self.abort_event.is_set():
                        return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                    if parsed is None:
                        break
                    if parsed.action == "not_found":
                        if remaining_scroll <= 0:
                            break
                        y_before, y_max = _scroll_state_y()
                        if y_max <= y_before + 4:
                            break  # page not scrollable or already at bottom
                        remaining_scroll -= 1
                        scroll_find_attempts += 1
                        self._emit_status("running", "未找到目标，滚动后继续查找")
                        sp = ParsedAction(action="scroll", coordinate=[500, 720], direction="down", amount=4)
                        _scroll_and_recapture(sp)
                        y_after, _ = _scroll_state_y()
                        if abs(y_after - y_before) <= 2:
                            break  # scroll had no effect — stop retrying
                        continue
                    if parsed.action == "scroll" and not scroll_sem and chain_budget > 0:
                        # model itself chose to scroll to reveal the target — execute and re-ground in-loop
                        chain_budget -= 1
                        scroll_find_attempts += 1
                        y_before, _ = _scroll_state_y()
                        self._emit_status("running", "滚动查找目标中")
                        _scroll_and_recapture(parsed)
                        y_after, _ = _scroll_state_y()
                        if abs(y_after - y_before) <= 2:
                            break  # cannot scroll further — let final prediction stand
                        continue
                    break
                if parsed is None:
                    self._emit_status("error", "模型输出格式无效")
                    return {"status": "invalid_json", "raw_prediction": raw_text, "local_usage": local_usage, "elapsed_s": round(time.perf_counter() - t_start, 4)}
                if parsed.action == "not_found":
                    self.step_count += 1
                    append_audit({"op": "act", "action": "not_found", "instruction": instruction, "reason": parsed.reason})
                    self._emit_status("running" if self.task_state == "running" else "idle", "未在当前画面找到目标")
                    return {
                        "status": "not_found",
                        "reason": parsed.reason,
                        "raw_prediction": raw_text,
                        "infer_s": round(infer_s, 4),
                        "scroll_find_attempts": scroll_find_attempts,
                        "local_usage": local_usage,
                        "elapsed_s": round(time.perf_counter() - t_start, 4),
                    }
                hit_el = None
                if parsed.coordinate is not None:
                    cx, cy = parsed.coordinate
                    self._last_click_coord = (int(cx), int(cy))
                    hit_el = next(
                        (
                            e
                            for e in snap.get("elements", [])
                            if e["rect_norm"][0] <= cx <= e["rect_norm"][2] and e["rect_norm"][1] <= cy <= e["rect_norm"][3]
                        ),
                        None,
                    )
                blocked_post = self._verify_resolved_target_policy(
                    policy_inst=inst_for_policy,
                    element_id=element_id,
                    ocr_id=ocr_id,
                    confirm_token=confirm_token,
                    pre_confirmed=pre_confirmed,
                    resolved_meta=hit_el,
                    coordinate=parsed.coordinate,
                    local_usage=local_usage,
                )
                if blocked_post is not None:
                    return blocked_post
                if self.abort_event.is_set():
                    return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                exec_ok, exec_status = self.chrome.dispatch_viewport_action(parsed, info, raw_img.size)
                if exec_ok and parsed.action == "left_click" and strategy == "hybrid" and auto_heal and parsed.coordinate is not None:
                    time.sleep(0.18)
                    after_info, after_img = self.chrome.capture_full_window()
                    after_snap = self.chrome.extract_dom_snapshot(after_info, *after_img.size, max_elements=60)
                    effect = analyze_click_effect(
                        raw_img,
                        after_img,
                        parsed.coordinate,
                        title_before=self.active_title,
                        title_after=str(after_snap.get("title", "")),
                        url_before=self.active_url,
                        url_after=str(after_snap.get("url", "")),
                    )
                    changed = (after_snap.get("dom_signature") != snap.get("dom_signature")) or effect["effective"]
                    if not changed:
                        if self.abort_event.is_set():
                            return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                        try:
                            raw_z, parsed_z, inf_z, uz = self.grounder.predict_zoomed(raw_img, instruction, parsed.coordinate)
                            _accumulate_usage(local_usage, uz)
                            infer_s += inf_z
                        except VLMCallError:
                            _accumulate_usage(local_usage, None)
                            parsed_z = None
                        if self.abort_event.is_set():
                            return {"status": "aborted", "reason": "aborted_by_user", "local_usage": local_usage}
                        if parsed_z is not None and parsed_z.action == "left_click":
                            hit_el_z = None
                            if parsed_z.coordinate is not None:
                                zx, zy = parsed_z.coordinate
                                hit_el_z = next(
                                    (
                                        e
                                        for e in after_snap.get("elements", []) or snap.get("elements", [])
                                        if e["rect_norm"][0] <= zx <= e["rect_norm"][2] and e["rect_norm"][1] <= zy <= e["rect_norm"][3]
                                    ),
                                    None,
                                )
                            blocked_z = self._verify_resolved_target_policy(
                                policy_inst=inst_for_policy,
                                element_id=element_id,
                                ocr_id=ocr_id,
                                confirm_token=None,
                                pre_confirmed=None,
                                resolved_meta=hit_el_z,
                                coordinate=parsed_z.coordinate,
                                local_usage=local_usage,
                            )
                            if blocked_z is not None:
                                return blocked_z
                            exec_ok, exec_status = self.chrome.dispatch_viewport_action(parsed_z, info, raw_img.size)
                            parsed = parsed_z
                            raw_text = raw_z
                            auto_healed = True
                            if parsed_z.coordinate is not None:
                                self._last_click_coord = (int(parsed_z.coordinate[0]), int(parsed_z.coordinate[1]))

            if wait_settle:
                self._wait_managed_settle(prev_sig=str(snap.get("dom_signature", "")), max_wait_s=3.5)
            else:
                time.sleep(0.18)

            post_info, post_img, post_snap = self._capture_and_snap_managed(max_elements=50)
            click_effect = None
            if self._last_click_coord is not None and (parsed is None or parsed.action == "left_click"):
                click_effect = analyze_click_effect(
                    raw_img,
                    post_img,
                    self._last_click_coord,
                    title_before=str(snap.get("title", "")),
                    title_after=str(post_snap.get("title", "")),
                    url_before=str(snap.get("url", "")),
                    url_after=str(post_snap.get("url", "")),
                )
                if post_snap.get("dom_signature") != snap.get("dom_signature"):
                    click_effect["effective"] = True
                self._last_click_effect = click_effect

            self.step_count += 1
            elapsed = time.perf_counter() - t_start
            append_audit({
                "op": "act",
                "instruction": instruction,
                "grounding_source": grounding_source,
                "action": parsed.action if parsed else exec_status,
                "exec_ok": exec_ok,
                "auto_healed": auto_healed,
                "elapsed_s": round(elapsed, 4),
            })
            step_msg = f"步骤#{self.task_step} {'已执行' if exec_ok else '未生效'} · {elapsed:.2f}s"
            self._emit_status("running" if exec_ok and self.task_state == "running" else ("idle" if exec_ok else "error"), step_msg)
            out = {
                "status": "ok" if exec_ok else "exec_failed",
                "grounding_source": grounding_source,
                "exec_status": exec_status,
                "action": parsed.action if parsed else exec_status,
                "coordinate": list(parsed.coordinate) if (parsed and parsed.coordinate) else (list(self._last_click_coord) if self._last_click_coord else None),
                "auto_healed": auto_healed,
                "url": self.active_url,
                "title": self.active_title,
                "popup_detected": post_snap.get("popup_detected", False),
                "infer_s": round(infer_s, 4),
                "local_usage": local_usage,
                "elapsed_s": round(elapsed, 4),
            }
            if raw_text:
                out["raw_prediction"] = raw_text
            if click_effect is not None:
                out["click_effect"] = click_effect
            if include_text or wait_settle:
                out["text_excerpt"] = post_snap.get("text_excerpt", "")
                out["elements"] = _compact_dom_elements(post_snap.get("elements", [])[:40])
            self._attach_screenshot(out, post_img, include_screenshot, screenshot_format)
            return out

    def _run_atomic_click_and_type(self, spec: dict[str, Any]) -> dict[str, Any]:
        text_val = str(spec.get("text", ""))
        clear_first = bool(spec.get("clear_first", True))
        press_enter = bool(spec.get("press_enter", False))
        wait_settle = bool(spec.get("wait_settle", press_enter))
        require_submit = bool(spec.get("require_submit", False))
        expect_submit_raw = spec.get("expect_submit", None)
        expect_submit = None if expect_submit_raw is None else bool(expect_submit_raw)
        if require_submit and not press_enter:
            raise ValueError("click_and_type: require_submit=True requires press_enter=True")
        if require_submit and expect_submit is False:
            raise ValueError("click_and_type: require_submit=True conflicts with expect_submit=False")

        target_inst = spec.get("instruction") or spec.get("target")
        hint = spec.get("text_hint")

        click_res = self.act(
            instruction=target_inst,
            element_id=spec.get("element_id"),
            ocr_id=spec.get("ocr_id"),
            text_hint=hint,
            strategy=str(spec.get("strategy", "hybrid")),
            auto_heal=bool(spec.get("auto_heal", True)),
            confirm_token=spec.get("confirm_token"),
            include_screenshot=False,
            wait_settle=False,
            _extra_policy_text=text_val,
        )
        if click_res.get("status") != "ok":
            return click_res
        if self.abort_event.is_set():
            return {"status": "aborted", "reason": "aborted_by_user"}

        submitted: bool | None = None
        submission_state = "not_requested"
        submission_evidence = "not_requested"
        if self._is_desktop_like():
            info_now = self._resolve_desktop_window(focus=False)
            old_title = info_now.title
            before_img: Image.Image | None = None
            if wait_settle and not press_enter:
                try:
                    before_img = capture_roi(info_now.dwm_rect) if info_now.hwnd == 0 else print_window_dwm(info_now)
                except Exception:
                    before_img = None
            pre_enter_info: ChromeWindowInfo | None = None
            pre_enter_img: Image.Image | None = None
            pre_enter_url = self.active_url
            try:
                with self._desktop_io_guard():
                    if check_corner_failsafe():
                        err_res: dict[str, Any] = {
                            "status": "exec_failed",
                            "mode": self.target_mode,
                            "exec_status": "failsafe_corner_triggered",
                        }
                        if isinstance(click_res.get("local_usage"), dict):
                            err_res["local_usage"] = click_res["local_usage"]
                        return err_res
                    focused = self._focus_and_verify_desktop(info_now, settle_s=0.1)
                    if clear_first:
                        send_hotkey(["ctrl", "a"])
                        time.sleep(0.04)
                        send_hotkey(["backspace"])
                        time.sleep(0.04)
                    if text_val:
                        paste_text_via_clipboard(text_val)
                        time.sleep(0.05)
                    if press_enter:
                        try:
                            if focused.hwnd == 0:
                                pre_enter_info = inspect_primary_screen()
                                pre_enter_img = capture_roi(pre_enter_info.dwm_rect)
                            else:
                                pre_enter_info = inspect_chrome_window(focused.hwnd)
                                pre_enter_img = print_window_dwm(pre_enter_info)
                        except Exception:
                            pre_enter_info = focused
                            pre_enter_img = None
                        pre_enter_url = (
                            (self._read_desktop_url_safe(pre_enter_info) or self.active_url)
                            if self.target_mode == "desktop"
                            else ""
                        )
                        send_hotkey(["enter"])
            except RuntimeError as exc:
                err_res = {
                    "status": "exec_failed",
                    "mode": self.target_mode,
                    "exec_status": str(exc),
                    "reason": str(exc),
                }
                if isinstance(click_res.get("local_usage"), dict):
                    err_res["local_usage"] = click_res["local_usage"]
                return err_res

            until_targets = self._resolve_until_texts(spec.get("until_text"))
            until_preexisted = False
            if press_enter and until_targets and pre_enter_info is not None:
                pre_txt = ""
                if pre_enter_img is not None:
                    try:
                        pre_ocr = self._desktop_ocr.recognize(pre_enter_img, max_elements=40, max_chars=2400)
                        pre_txt = str((pre_ocr or {}).get("page_text") or "")
                    except Exception:
                        pre_txt = ""
                until_preexisted = self._text_matches_any(f"{pre_enter_info.title}\n{pre_txt}", until_targets)

            if wait_settle:
                before_img_ref = pre_enter_img if press_enter else before_img
                before_title_ref = pre_enter_info.title if (press_enter and pre_enter_info is not None) else old_title
                settle_meta: dict[str, Any] = {}
                post_info, post_img, cached_perc = self._wait_desktop_frame_settle(
                    focused.hwnd,
                    before_img=before_img_ref,
                    old_title=before_title_ref,
                    min_wait_s=0.35,
                    max_wait_s=float(spec.get("max_wait_s", 4.0)),
                    until_text=spec.get("until_text"),
                    meta_out=settle_meta,
                )
                self.active_title = post_info.title
                post_url = (
                    str((cached_perc or {}).get("url") or self._read_desktop_url_safe(post_info) or self.active_url)
                    if self.target_mode == "desktop"
                    else ""
                )
                if post_url:
                    self.active_url = post_url
                if press_enter:
                    pixel_frac = (
                        _frame_changed_fraction(pre_enter_img, post_img)
                        if (pre_enter_img is not None and post_img is not None)
                        else None
                    )
                    submission_state, submitted, submission_evidence = _classify_desktop_submission(
                        before_title=before_title_ref,
                        after_title=post_info.title,
                        before_url=pre_enter_url,
                        after_url=post_url,
                        pixel_change_frac=pixel_frac,
                        expect_submit=expect_submit,
                        wait_settle=True,
                        until_text=spec.get("until_text"),
                        until_matched=bool(settle_meta.get("until_matched", True)),
                        timed_out=bool(settle_meta.get("timed_out", False)),
                        until_preexisted=until_preexisted,
                    )
            elif press_enter:
                time.sleep(0.15)
                try:
                    if focused.hwnd == 0:
                        post_info = inspect_primary_screen()
                    else:
                        post_info = inspect_chrome_window(focused.hwnd) if win32gui.IsWindow(focused.hwnd) else focused
                except Exception:
                    post_info = focused
                self.active_title = post_info.title
                post_url = (
                    (self._read_desktop_url_safe(post_info) or self.active_url)
                    if self.target_mode == "desktop"
                    else ""
                )
                if post_url:
                    self.active_url = post_url
                until_matched_now = True
                if until_targets:
                    post_txt = ""
                    try:
                        post_shot = capture_roi(post_info.dwm_rect) if post_info.hwnd == 0 else print_window_dwm(post_info)
                        post_ocr = self._desktop_ocr.recognize(post_shot, max_elements=40, max_chars=2400)
                        post_txt = str((post_ocr or {}).get("page_text") or "")
                    except Exception:
                        post_txt = ""
                    until_matched_now = self._text_matches_any(f"{post_info.title}\n{post_txt}", until_targets)
                submission_state, submitted, submission_evidence = _classify_desktop_submission(
                    before_title=pre_enter_info.title if pre_enter_info is not None else old_title,
                    after_title=post_info.title,
                    before_url=pre_enter_url,
                    after_url=post_url,
                    pixel_change_frac=None,
                    expect_submit=expect_submit,
                    wait_settle=False,
                    until_text=spec.get("until_text"),
                    until_matched=until_matched_now,
                    timed_out=False,
                    until_preexisted=until_preexisted,
                )
        else:
            assert self.chrome.cdp is not None
            if clear_first:
                self.chrome.send_key(["ctrl", "a"])
                time.sleep(0.03)
                self.chrome.send_key(["backspace"])
                time.sleep(0.03)
            if text_val:
                self.chrome.cdp.send("Input.insertText", {"text": text_val})
                time.sleep(0.04)

            before_state: dict[str, Any] = {}
            if press_enter:
                try:
                    before_state = self.chrome.cdp.evaluate(
                        """(() => {
                          if (typeof window.__cua_submit_cleanup === 'function') {
                            try { window.__cua_submit_cleanup(); } catch (e) {}
                          }
                          const id = 'cua_' + Math.random().toString(36).slice(2);
                          const state = { id, submitted: false };
                          const handler = () => { state.submitted = true; };
                          document.addEventListener('submit', handler, true);
                          window.__cua_submit_probe = state;
                          window.__cua_submit_cleanup = () => {
                            document.removeEventListener('submit', handler, true);
                            if (window.__cua_submit_probe === state) delete window.__cua_submit_probe;
                          };
                          const ae = document.activeElement;
                          const tag = ae && ae.tagName ? ae.tagName.toLowerCase() : '';
                          const isCE = Boolean(ae && ae.isContentEditable);
                          const isMultiline = tag === 'textarea' || isCE;
                          const rawVal = ae ? ((tag === 'textarea' || tag === 'input') ? String(ae.value || '') : (isCE ? String(ae.innerText || '') : '')) : '';
                          const newlines = (rawVal.match(/\\r?\\n/g) || []).length;
                          const txt = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();
                          const n = document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="tab"]').length;
                          return {
                            probe_id: id,
                            url: location.href,
                            title: document.title || '',
                            active_tag: tag,
                            is_multiline: isMultiline,
                            active_newlines: newlines,
                            el_count: n,
                            sig: location.href + '|' + n + '|' + txt.length + '|' + txt.slice(0, 260) + '|' + txt.slice(-260),
                            body_text: txt.slice(0, 4000)
                          };
                        })()""",
                        timeout=3.0,
                    )
                except Exception:
                    before_state = {}
            elif wait_settle:
                try:
                    before_state = self.chrome.cdp.evaluate(
                        "(() => { const txt = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim(); const n = document.querySelectorAll('a,button,input,textarea,select,[role=\"button\"],[role=\"tab\"]').length; return { url: location.href, sig: location.href + '|' + n + '|' + txt.length + '|' + txt.slice(0, 260) + '|' + txt.slice(-260) }; })()",
                        timeout=3.0,
                    )
                except Exception:
                    before_state = {}

            before_sig = str(before_state.get("sig") or "")
            if press_enter:
                self.chrome.send_key(["enter"])

            probe: dict[str, Any] = {}
            if wait_settle:
                probe = self._wait_managed_settle(
                    prev_sig=before_sig,
                    max_wait_s=float(spec.get("max_wait_s", 3.5)),
                    until_text=spec.get("until_text"),
                )
            elif press_enter:
                time.sleep(0.15)

            if press_enter:
                post_state: dict[str, Any] = {}
                probe_failed = False
                try:
                    post_state = self.chrome.cdp.evaluate(
                        """(() => {
                          const p = window.__cua_submit_probe || null;
                          const probe_id = p ? p.id : null;
                          const form_submitted = Boolean(p && p.submitted);
                          if (typeof window.__cua_submit_cleanup === 'function') {
                            try { window.__cua_submit_cleanup(); } catch (e) {}
                            delete window.__cua_submit_cleanup;
                          }
                          const ae = document.activeElement;
                          const tag = ae && ae.tagName ? ae.tagName.toLowerCase() : '';
                          const isCE = Boolean(ae && ae.isContentEditable);
                          const rawVal = ae ? ((tag === 'textarea' || tag === 'input') ? String(ae.value || '') : (isCE ? String(ae.innerText || '') : '')) : '';
                          const newlines = (rawVal.match(/\\r?\\n/g) || []).length;
                          const txt = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();
                          const n = document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="tab"]').length;
                          return {
                            probe_id,
                            form_submitted,
                            url: location.href,
                            title: document.title || '',
                            active_newlines: newlines,
                            el_count: n,
                            body_text: txt.slice(0, 4000)
                          };
                        })()""",
                        timeout=3.0,
                    )
                except Exception:
                    probe_failed = True
                    post_state = {}
                if not isinstance(post_state, dict):
                    probe_failed = True
                    post_state = {}
                if not post_state.get("url") and probe.get("url"):
                    post_state["url"] = probe.get("url")
                if not post_state.get("title") and probe.get("title"):
                    post_state["title"] = probe.get("title")

                after_url = str(post_state.get("url") or "")
                if after_url:
                    self.active_url = after_url
                if post_state.get("title"):
                    self.active_title = str(post_state["title"])

                until_targets = self._resolve_until_texts(spec.get("until_text"))
                until_preexisted = bool(until_targets) and self._text_matches_any(
                    f"{before_state.get('title', '')}\n{before_state.get('body_text', '')}",
                    until_targets,
                )
                post_haystack = f"{post_state.get('title', '')}\n{post_state.get('body_text', '')}"
                if until_targets:
                    if wait_settle:
                        until_matched_now = bool(probe.get("until_matched", False)) or (
                            not probe_failed and self._text_matches_any(post_haystack, until_targets)
                        )
                    else:
                        until_matched_now = not probe_failed and self._text_matches_any(post_haystack, until_targets)
                else:
                    until_matched_now = True

                submission_state, submitted, submission_evidence = _classify_managed_submission(
                    before_state,
                    post_state,
                    text_val=text_val,
                    expect_submit=expect_submit,
                    wait_settle=wait_settle,
                    until_text=spec.get("until_text"),
                    until_matched=until_matched_now,
                    timed_out=bool(probe.get("timed_out", False)) if wait_settle else False,
                    probe_failed=probe_failed,
                    until_preexisted=until_preexisted,
                )
            elif wait_settle and isinstance(probe, dict):
                if probe.get("url"):
                    self.active_url = str(probe["url"])
                if probe.get("title"):
                    self.active_title = str(probe["title"])

        self.step_count += 1
        step_status = "ok"
        step_reason = ""
        if require_submit and submission_state != "observed":
            step_status = "submit_not_observed"
            step_reason = f"按下回车后未观察到提交信号 ({submission_state}: {submission_evidence})"

        out_cat: dict[str, Any] = {
            "status": step_status,
            "mode": self.target_mode,
            "kind": "click_and_type",
            "grounding_source": click_res.get("grounding_source", ""),
            "coordinate": click_res.get("coordinate"),
            "typed_len": len(text_val),
            "pressed_enter": press_enter,
            "submission_state": submission_state,
            "submission_evidence": submission_evidence,
            "require_submit": require_submit,
            "url": self.active_url,
            "title": self.active_title,
        }
        if step_reason:
            out_cat["reason"] = step_reason
        if press_enter and submission_state != "not_expected":
            out_cat["submitted"] = submitted
        if isinstance(click_res.get("local_usage"), dict):
            out_cat["local_usage"] = click_res["local_usage"]
        return out_cat

    def _run_scroll_until(self, spec: dict[str, Any]) -> dict[str, Any]:
        targets = self._resolve_until_texts(spec.get("until_text"))
        direction = str(spec.get("direction", "down"))
        amount = int(spec.get("amount", 6))
        max_scrolls = min(max(1, int(spec.get("max_scrolls", 5))), 10)

        prev_img: Image.Image | None = None
        same_streak = 0
        for idx in range(0, max_scrolls + 1):
            if self.abort_event.is_set():
                return {"status": "aborted", "reason": "aborted_by_user"}
            obs_now = self.observe(include_screenshot=False, max_elements=50)
            if obs_now.get("auth_or_captcha_gate"):
                return {
                    "status": "needs_human_takeover",
                    "reason": str(obs_now["auth_or_captcha_gate"]),
                    "scrolls_done": idx,
                }
            if self._is_desktop_like():
                haystack = f"{obs_now.get('title', '')}\n{obs_now.get('page_text') or obs_now.get('text_excerpt') or ''}"
            else:
                vp_text = ""
                if getattr(self.chrome, "cdp", None) is not None:
                    try:
                        vp_text = self.chrome.extract_viewport_text()
                    except Exception:
                        vp_text = ""
                el_labels = " ".join(
                    str(e.get(k) or "")
                    for e in (obs_now.get("elements") or [])
                    if isinstance(e, dict)
                    for k in ("text", "aria", "placeholder")
                    if e.get(k)
                )
                haystack = f"{obs_now.get('title', '')}\n{vp_text}\n{el_labels}"
            if targets and self._text_matches_any(haystack, targets):
                return {"status": "ok", "kind": "scroll_until", "matched": True, "scrolls_done": idx, "url": obs_now.get("url", ""), "title": obs_now.get("title", "")}
            if idx == max_scrolls:
                break

            if self._is_desktop_like():
                _, cur_img = self._capture_desktop_window(focus=False)
            else:
                _, cur_img = self.chrome.capture_full_window()
            if prev_img is not None and _frame_changed_fraction(prev_img, cur_img) < 0.002:
                same_streak += 1
                if same_streak >= 2:
                    return {
                        "status": "not_found" if targets else "ok",
                        "kind": "scroll_until",
                        "matched": False,
                        "scrolls_done": idx,
                        "reason": "页面已滚动到底或内容无变化",
                    }
            else:
                same_streak = 0
            prev_img = cur_img

            s_res = self.act(
                action_json={
                    "action": "scroll",
                    "coordinate": list(spec.get("coordinate") or [500, 550]),
                    "direction": direction,
                    "amount": amount,
                },
                include_screenshot=False,
                wait_settle=False,
            )
            if s_res.get("status") != "ok":
                return s_res
            time.sleep(0.32)

        if targets:
            return {
                "status": "not_found",
                "kind": "scroll_until",
                "matched": False,
                "scrolls_done": max_scrolls,
                "reason": f"滚动 {max_scrolls} 次后仍未出现目标文本: {targets}",
            }
        return {"status": "ok", "kind": "scroll_until", "matched": True, "scrolls_done": max_scrolls}

    def _run_wait_until(self, spec: dict[str, Any]) -> dict[str, Any]:
        until_text = spec.get("until_text")
        targets = self._resolve_until_texts(until_text)
        max_wait_s = min(max(0.3, float(spec.get("max_wait_s", 4.0))), 12.0)
        if self._is_desktop_like():
            info = self._resolve_desktop_window(focus=False)
            cur_info, cur_img, cached_perc = self._wait_desktop_frame_settle(
                info.hwnd,
                before_img=None,
                old_title=info.title,
                min_wait_s=0.25,
                max_wait_s=max_wait_s,
                until_text=until_text,
            )
            perc = cached_perc or self._perceive_desktop(cur_info, cur_img, max_elements=50, max_chars=2000)
            matched = self._text_matches_any(f"{cur_info.title}\n{perc.get('page_text', '')}", targets) if targets else True
            return {
                "status": "ok" if matched else "not_found",
                "kind": "wait_until",
                "matched": matched,
                "url": perc.get("url", ""),
                "title": cur_info.title,
                "reason": "" if matched else f"等待 {max_wait_s:.1f}s 后仍未出现目标文本: {targets}",
            }
        probe = self._wait_managed_settle(max_wait_s=max_wait_s, until_text=until_text)
        matched = self._text_matches_any(f"{probe.get('title', '')}\n{probe.get('text', '')}", targets) if targets else True
        return {
            "status": "ok" if matched else "not_found",
            "kind": "wait_until",
            "matched": matched,
            "url": probe.get("url", self.active_url),
            "title": probe.get("title", self.active_title),
            "reason": "" if matched else f"等待 {max_wait_s:.1f}s 后仍未出现目标文本: {targets}",
        }

    def run_steps(
        self,
        steps: list[dict[str, Any] | str],
        stop_on_popup: bool = True,
        auto_dismiss_popup: bool = True,
        max_steps: int = 12,
        include_screenshot: bool = False,
        screenshot_on_error: bool = False,
        screenshot_format: str = "jpeg",
        verify_question: str | None = None,
        extract_text: bool = True,
        detail: str = "brief",
    ) -> dict[str, Any]:
        self.reset_abort()
        limit = min(len(steps), max(1, int(max_steps)))
        parsed_specs: list[tuple[int, dict[str, Any], str]] = []
        for idx, item in enumerate(steps[:limit], start=1):
            spec = {"kind": "act", "instruction": item} if isinstance(item, str) else dict(item)
            kind = str(spec.get("kind") or spec.get("action") or "act").strip().lower()
            if kind == "click_and_type":
                pe = bool(spec.get("press_enter", False))
                rs = bool(spec.get("require_submit", False))
                es_raw = spec.get("expect_submit", None)
                es = None if es_raw is None else bool(es_raw)
                if rs and not pe:
                    raise ValueError(f"Step {idx} (click_and_type): require_submit=True requires press_enter=True")
                if rs and es is False:
                    raise ValueError(f"Step {idx} (click_and_type): require_submit=True conflicts with expect_submit=False")
            parsed_specs.append((idx, spec, kind))

        self.update_task("start", title=f"微计划（共 {limit} 步）", step=0, total_steps=limit, message="开始执行微计划")
        trace: list[dict[str, Any]] = []
        agg_usage = _new_local_usage()
        stagnant_streak = 0
        final_status = "completed"
        extra_note = ""
        observed_submit_steps: list[int] = []
        unverified_submit_steps: list[int] = []
        unverified_submit_states: list[str] = []

        for idx, spec, kind in parsed_specs:
            if self.abort_event.is_set():
                final_status = "aborted"
                break
            step_desc = str(
                spec.get("instruction")
                or spec.get("text_hint")
                or spec.get("url")
                or spec.get("question")
                or spec.get("until_text")
                or kind
            )[:42]
            self.update_task("progress", step=idx, total_steps=limit, message=step_desc)

            pre_obs: dict[str, Any] | None = None
            pre_img: Image.Image | None = None
            pre_info: ChromeWindowInfo | None = None

            if self._is_desktop_like():
                pre_info, pre_img = self._capture_desktop_window(focus=False)
                gate = self._check_desktop_title_gate(pre_info.title)
                if gate:
                    final_status = "needs_human_takeover"
                    extra_note = gate
                    break
            elif kind in {"act", "click_and_type"}:
                pre_obs = self.observe(include_screenshot=False, max_elements=45)
                if pre_obs.get("auth_or_captcha_gate"):
                    final_status = "needs_human_takeover"
                    extra_note = str(pre_obs["auth_or_captcha_gate"])
                    break
                if pre_obs.get("popup_detected"):
                    if auto_dismiss_popup:
                        dis = self.dismiss_popup()
                        _merge_local_usage(agg_usage, dis.get("local_usage"))
                        trace.append({"step": f"{idx}_popup_heal", **dis})
                        if dis.get("status") != "ok" and stop_on_popup:
                            final_status = f"halted:{dis.get('status')}" if dis.get("status") == "vlm_error" else "halted_on_popup"
                            extra_note = str(dis.get("reason") or "")
                            break
                    elif stop_on_popup:
                        final_status = "halted_on_popup"
                        break

            try:
                if kind == "goto":
                    nav_url = str(spec.get("url") or "")
                    res = self.navigate("goto", url=nav_url, include_screenshot=False)
                    if spec.get("until_text"):
                        w_res = self._run_wait_until(spec)
                        if w_res.get("status") != "ok":
                            res = w_res
                elif kind == "click_and_type":
                    res = self._run_atomic_click_and_type(spec)
                elif kind == "scroll_until":
                    res = self._run_scroll_until(spec)
                elif kind in {"wait_until", "wait_settle"}:
                    res = self._run_wait_until(spec)
                elif kind == "dismiss_popup":
                    res = self.dismiss_popup()
                elif kind == "ask":
                    res = self.ask_screen(
                        question=str(spec.get("question", "")),
                        around_last_click=bool(spec.get("around_last_click", False)),
                        include_screenshot=False,
                    )
                elif kind == "hotkey":
                    res = self.act(
                        action_json={"action": "hotkey", "keys": list(spec.get("keys") or [])},
                        confirm_token=spec.get("confirm_token"),
                        wait_settle=bool(spec.get("wait_settle", False)),
                        include_screenshot=False,
                    )
                elif kind == "type":
                    res = self.act(
                        action_json={"action": "type", "text": str(spec.get("text", ""))},
                        confirm_token=spec.get("confirm_token"),
                        wait_settle=bool(spec.get("wait_settle", False)),
                        include_screenshot=False,
                    )
                elif kind == "scroll":
                    res = self.act(
                        action_json={
                            "action": "scroll",
                            "coordinate": list(spec.get("coordinate") or [500, 550]),
                            "direction": str(spec.get("direction", "down")),
                            "amount": int(spec.get("amount", 5)),
                        },
                        confirm_token=spec.get("confirm_token"),
                        wait_settle=bool(spec.get("wait_settle", False)),
                        include_screenshot=False,
                    )
                elif kind == "drag":
                    res = self.act(
                        action_json={
                            "action": "drag",
                            "coordinate": list(spec.get("coordinate") or [0, 0]),
                            "end_coordinate": list(spec.get("end_coordinate") or spec.get("to") or [0, 0]),
                        },
                        confirm_token=spec.get("confirm_token"),
                        wait_settle=bool(spec.get("wait_settle", False)),
                        include_screenshot=False,
                    )
                else:
                    res = self.act(
                        instruction=spec.get("instruction") or spec.get("target"),
                        element_id=spec.get("element_id"),
                        ocr_id=spec.get("ocr_id"),
                        text_hint=spec.get("text_hint"),
                        action_json=spec.get("action_json"),
                        strategy=str(spec.get("strategy", "hybrid")),
                        auto_heal=bool(spec.get("auto_heal", True)),
                        confirm_token=spec.get("confirm_token"),
                        include_screenshot=False,
                        wait_settle=bool(spec.get("wait_settle", False)),
                    )
            except Exception as exc:
                res = {"status": "exec_failed", "kind": kind, "reason": str(exc)}

            _merge_local_usage(agg_usage, res.get("local_usage"))
            trace_entry = {"step": idx, "kind": kind, "input": spec, **{k: v for k, v in res.items() if k not in {"ocr_elements", "elements", "page_text", "text_excerpt"}}}
            trace.append(trace_entry)
            st = res.get("status")
            if kind == "click_and_type" and bool(res.get("pressed_enter")):
                sub_state = str(res.get("submission_state") or "")
                if sub_state == "observed":
                    observed_submit_steps.append(idx)
                elif sub_state in {"not_observed", "unknown"}:
                    unverified_submit_steps.append(idx)
                    unverified_submit_states.append(sub_state)
            if st in {
                "needs_human_confirmation",
                "needs_human_takeover",
                "aborted",
                "not_found",
                "invalid_json",
                "exec_failed",
                "vlm_error",
                "submit_not_observed",
            }:
                final_status = f"halted:{st}"
                extra_note = str(res.get("reason") or res.get("exec_status") or "")
                break

            if kind in {"ask", "wait_until", "wait_settle", "scroll_until", "goto", "dismiss_popup", "click_and_type"}:
                stagnant_streak = 0
                continue

            if self._is_desktop_like():
                eff = res.get("click_effect") if isinstance(res.get("click_effect"), dict) else None
                if eff and eff.get("effective"):
                    stagnant_streak = 0
                else:
                    post_info, post_img = self._capture_desktop_window(focus=False)
                    if pre_info is not None and pre_img is not None and post_info.title == pre_info.title and _frame_changed_fraction(pre_img, post_img) <= 0.002:
                        stagnant_streak += 1
                    else:
                        stagnant_streak = 0
            else:
                eff = res.get("click_effect") if isinstance(res.get("click_effect"), dict) else None
                if eff and eff.get("effective"):
                    stagnant_streak = 0
                elif pre_obs is not None:
                    post_obs = self.observe(include_screenshot=False, max_elements=40)
                    if post_obs.get("dom_signature") == pre_obs.get("dom_signature") and post_obs.get("scroll_y") == pre_obs.get("scroll_y"):
                        stagnant_streak += 1
                    else:
                        stagnant_streak = 0

            if stagnant_streak >= 2:
                final_status = "halted:stagnant_screen"
                extra_note = "连续两步画面无变化，已停止微计划避免空转"
                break

        actions_completed = (final_status == "completed")
        if final_status == "completed" and unverified_submit_steps:
            final_status = "completed_unverified_submit"
            extra_note = (
                f"动作已全部执行，但第 {unverified_submit_steps} 步按下回车后未观察到提交信号"
                f"（{', '.join(unverified_submit_states)}）；请勿据此直接判定业务提交成功"
            )

        if unverified_submit_steps:
            submission_summary = "not_observed" if "not_observed" in unverified_submit_states else "unknown"
        elif observed_submit_steps:
            submission_summary = "observed"
        else:
            submission_summary = "not_requested"

        should_shot = bool(include_screenshot) or (bool(screenshot_on_error) and final_status not in {"completed", "completed_unverified_submit"})
        try:
            final_obs = self.observe(
                include_screenshot=should_shot,
                annotate_som=False,
                max_elements=50,
                screenshot_format=screenshot_format,
            )
        except Exception:
            final_obs = {"url": self.active_url, "title": self.active_title}

        verification_answer = ""
        if verify_question and str(verify_question).strip():
            v_res = self.ask_screen(str(verify_question), around_last_click=True, include_screenshot=False)
            verification_answer = str(v_res.get("answer", ""))
            _merge_local_usage(agg_usage, v_res.get("local_usage"))

        if final_status == "completed":
            self.task_state = "done"
            self.task_step_label = f"微计划完成（共 {len(trace)} 步）"
            self._emit_status("idle", self.task_step_label, event_kind="task_done", play_sound=True)
        elif final_status == "completed_unverified_submit":
            self.task_state = "done"
            self.task_step_label = f"动作已执行（未验证提交 · {len(trace)} 步）"
            self._emit_status("idle", self.task_step_label, event_kind="status", play_sound=False)
        elif final_status in {
            "needs_human_takeover",
            "halted:needs_human_confirmation",
            "halted:needs_human_takeover",
            "halted_on_popup",
        }:
            self.task_state = "confirm"
            self.task_step_label = (extra_note or "等待人工确认")[:60]
            self._emit_status("confirm", self.task_step_label, event_kind="task_alert", play_sound=True)
        else:
            self.task_state = "error"
            self.task_step_label = (extra_note or final_status)[:60]
            self._emit_status("error", self.task_step_label, event_kind="task_error", play_sound=True)

        executed = len(trace)
        if str(detail).lower() != "full":
            brief_trace: list[dict[str, Any]] = []
            page: tuple[Any, Any] = (None, None)
            for entry in trace:
                compact, page = _brief_trace_entry(entry, page)
                brief_trace.append(compact)
            trace = brief_trace

        out: dict[str, Any] = {
            "status": final_status,
            "actions_completed": actions_completed,
            "submission_summary": submission_summary,
            "note": extra_note,
            "executed_steps": executed,
            "trace": trace,
            "final_url": final_obs.get("url", ""),
            "final_title": final_obs.get("title", ""),
            "local_usage": agg_usage,
        }
        if unverified_submit_steps:
            out["unverified_submit_steps"] = unverified_submit_steps
        if self._last_click_effect is not None:
            out["last_click_effect"] = self._last_click_effect
        if extract_text:
            out["page_text"] = final_obs.get("page_text") or final_obs.get("text_excerpt") or ""
            out["elements"] = (final_obs.get("elements") or final_obs.get("ocr_elements") or [])[:40]
        if verification_answer:
            out["verification_answer"] = verification_answer
        if should_shot and final_obs.get("screenshot_b64"):
            out["screenshot_b64"] = final_obs["screenshot_b64"]
            out["screenshot_mime"] = final_obs.get("screenshot_mime", "image/jpeg")
        return out

    # -------- Desktop Foreground Chrome Single-Step Support (SendInput) --------
    def predict_desktop_window(
        self,
        hwnd: int,
        instruction: str,
        model_label: str | None = None,
    ) -> dict[str, Any]:
        if self.abort_event.is_set():
            return {"aborted": True, "info": None, "frame": None, "raw": "", "parsed": None, "screen_pt": None, "preview_crop": None, "elapsed_s": 0.0}
        with self._lock:
            if self.abort_event.is_set():
                return {"aborted": True, "info": None, "frame": None, "raw": "", "parsed": None, "screen_pt": None, "preview_crop": None, "elapsed_s": 0.0}
            if model_label:
                self.grounder.set_model(model_label)
            t0 = time.perf_counter()
            with self._desktop_io_guard():
                info = focus_chrome_window(inspect_chrome_window(hwnd), settle_s=0.18)
                frame = capture_chrome_window(info)
            if self.abort_event.is_set():
                return {"aborted": True, "info": info, "frame": frame, "raw": "", "parsed": None, "screen_pt": None, "preview_crop": None, "elapsed_s": time.perf_counter() - t0}
            raw_text, parsed, infer_s, _ = self.grounder.predict(frame, instruction)
            if self.abort_event.is_set():
                return {"aborted": True, "info": info, "frame": frame, "raw": raw_text, "parsed": parsed, "screen_pt": None, "preview_crop": None, "elapsed_s": time.perf_counter() - t0}
            screen_pt = norm_to_screen(*parsed.coordinate, info.dwm_rect) if (parsed and parsed.coordinate) else None
            preview_crop: Optional[Image.Image] = None
            if parsed is not None and parsed.coordinate is not None:
                vis = frame.copy()
                draw = ImageDraw.Draw(vis)
                w, h = vis.size
                px = round(parsed.coordinate[0] / 1000.0 * max(1, w - 1))
                py = round(parsed.coordinate[1] / 1000.0 * max(1, h - 1))
                for radius, color, width in ((19, (0, 0, 0), 5), (18, (255, 255, 255), 2)):
                    draw.ellipse([(px - radius, py - radius), (px + radius, py + radius)], outline=color, width=width)
                cw, ch = 260, 140
                left = max(0, min(px - cw // 2, w - cw))
                top = max(0, min(py - ch // 2, h - ch))
                preview_crop = vis.crop((left, top, min(w, left + cw), min(h, top + ch)))
            return {
                "info": info,
                "frame": frame,
                "raw": raw_text,
                "parsed": parsed,
                "screen_pt": screen_pt,
                "preview_crop": preview_crop,
                "elapsed_s": time.perf_counter() - t0,
            }

    def execute_desktop_pending(self, info: ChromeWindowInfo, frame: Image.Image, parsed: ParsedAction) -> tuple[bool, str]:
        if self.abort_event.is_set():
            return False, "aborted_by_user"
        with self._lock:
            if self.abort_event.is_set():
                return False, "aborted_by_user"
            with self._desktop_io_guard():
                if self.abort_event.is_set():
                    return False, "aborted_by_user"
                if check_corner_failsafe():
                    return False, "failsafe_corner_triggered"
                try:
                    focused = self._focus_and_verify_desktop(info, settle_s=0.15)
                except RuntimeError:
                    return False, "chrome_not_foreground"
                if self.abort_event.is_set():
                    return False, "aborted_by_user"
                return execute_confirmed_action(focused, frame, parsed)

    def close(self) -> None:
        self.request_abort()
        with self._lock:
            if self._desktop_uia is not None:
                self._desktop_uia.close()
                self._desktop_uia = None
            self._desktop_ocr.close()
            self._desktop_url_reader.close()
            self.chrome.close()
            self.grounder.shutdown()
