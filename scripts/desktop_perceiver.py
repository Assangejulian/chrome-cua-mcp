"""Fast local perception for desktop Chrome: WinRT OCR, top-bar UIA URL/tabs, and post-click diff analysis."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageOps

_URL_SCHEME_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_DOMAIN_LIKE_RE = re.compile(
    r"^(?:localhost(?::\d+)?|127\.0\.0\.1(?::\d+)?|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?)(?:/[^\s]*)?$",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_INTER_SPACE_RE = re.compile(
    r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])"
)

_PS_WINRT_OCR_SCRIPT = r"""
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
    if ($langs.Count -gt 0) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langs[0])
    }
}

while ($true) {
    $imgPath = [Console]::In.ReadLine()
    if ($null -eq $imgPath) { break }
    $imgPath = $imgPath.Trim()
    if ($imgPath -eq 'QUIT') { break }
    if (-not $imgPath) { continue }
    $stream = $null
    $bitmap = $null
    try {
        if ($null -eq $engine) {
            [Console]::Out.WriteLine('{"ok":false,"error":"ocr_engine_unavailable","lines":[]}')
            continue
        }
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $pxW = [int]$bitmap.PixelWidth
        $pxH = [int]$bitmap.PixelHeight
        $bitmap.Dispose(); $bitmap = $null
        $stream.Dispose(); $stream = $null
        $decoder = $null
        $file = $null
        $linesOut = New-Object System.Collections.Generic.List[object]
        foreach ($ln in @($res.Lines)) {
            $words = @($ln.Words)
            if ($words.Count -eq 0) { continue }
            $x1 = [double]::MaxValue; $y1 = [double]::MaxValue
            $x2 = 0.0; $y2 = 0.0
            foreach ($w in $words) {
                $r = $w.BoundingRect
                if ($r.X -lt $x1) { $x1 = $r.X }
                if ($r.Y -lt $y1) { $y1 = $r.Y }
                if (($r.X + $r.Width) -gt $x2) { $x2 = $r.X + $r.Width }
                if (($r.Y + $r.Height) -gt $y2) { $y2 = $r.Y + $r.Height }
            }
            $txt = if ($null -ne $ln.Text) { $ln.Text.Trim() } else { '' }
            if ($txt) {
                $linesOut.Add(@{
                    text = $txt
                    box = @([int][Math]::Round($x1), [int][Math]::Round($y1), [int][Math]::Round($x2), [int][Math]::Round($y2))
                })
            }
        }
        $payload = @{
            ok = $true
            width = $pxW
            height = $pxH
            lines = $linesOut.ToArray()
        }
        [Console]::Out.WriteLine((ConvertTo-Json -InputObject $payload -Compress -Depth 5))
    } catch {
        if ($null -ne $bitmap) { try { $bitmap.Dispose() } catch {} }
        if ($null -ne $stream) { try { $stream.Dispose() } catch {} }
        $errJson = ConvertTo-Json $_.Exception.Message -Compress
        [Console]::Out.WriteLine(('{"ok":false,"error":' + $errJson + ',"lines":[]}'))
    }
}
"""

_PS_UIA_URL_SCRIPT = r"""
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit
)
$tabCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::TabItem
)
$topCond = New-Object System.Windows.Automation.OrCondition($editCond, $tabCond)

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
        $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $topCond)
        $edits = New-Object System.Collections.Generic.List[object]
        $tabs = New-Object System.Collections.Generic.List[object]
        for ($i = 0; $i -lt $all.Count; $i++) {
            $el = $all.Item($i)
            $c = $el.Current
            if ($c.IsOffscreen) { continue }
            $br = $c.BoundingRectangle
            if ($br.IsEmpty -or $br.Width -le 1 -or $br.Height -le 1) { continue }
            if ($br.Bottom -gt $maxY) { continue }
            $ct = $c.ControlType.ProgrammaticName
            $name = if ($null -ne $c.Name) { $c.Name.Trim() } else { '' }
            if ($ct -eq 'ControlType.Edit') {
                $val = ''
                try {
                    $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                    if ($null -ne $vp -and $null -ne $vp.Current.Value) {
                        $val = $vp.Current.Value.Trim()
                    }
                } catch {}
                $edits.Add(@{
                    name = $name
                    value = $val
                    left = [int][Math]::Round($br.Left)
                    top = [int][Math]::Round($br.Top)
                    right = [int][Math]::Round($br.Right)
                    bottom = [int][Math]::Round($br.Bottom)
                })
            } elseif ($ct -eq 'ControlType.TabItem' -and $name) {
                $tabs.Add(@{
                    title = $name
                    left = [int][Math]::Round($br.Left)
                    top = [int][Math]::Round($br.Top)
                    right = [int][Math]::Round($br.Right)
                    bottom = [int][Math]::Round($br.Bottom)
                })
            }
        }
        $payload = @{
            ok = $true
            edits = $edits.ToArray()
            tabs = $tabs.ToArray()
        }
        [Console]::Out.WriteLine((ConvertTo-Json -InputObject $payload -Compress -Depth 5))
    } catch {
        $errJson = ConvertTo-Json $_.Exception.Message -Compress
        [Console]::Out.WriteLine(('{"ok":false,"error":' + $errJson + ',"edits":[],"tabs":[]}'))
    }
}
"""


def _normalize_url_candidate(raw: str) -> str:
    s = (raw or "").strip()
    if not s or " " in s:
        return ""
    if _URL_SCHEME_RE.match(s):
        return s
    if _DOMAIN_LIKE_RE.match(s):
        scheme = "http" if s.lower().startswith(("localhost", "127.0.0.1")) else "https"
        return f"{scheme}://{s}"
    return ""


def _should_keep_ocr_line(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if len(t) >= 2:
        return True
    if t.isdigit() or _CJK_RE.search(t):
        return True
    return t in {"×", "+", "-", "✕", "✖"}


def _infer_url_from_lines(items: list[dict[str, Any]]) -> str:
    """Best-effort omnibox URL extraction from top OCR band when UIA is unavailable."""
    for it in items:
        rect = it.get("rect_norm") or [0, 0, 0, 0]
        if rect[1] > 95:
            continue
        raw = str(it.get("text", "")).strip()
        for token in raw.split():
            norm = _normalize_url_candidate(token.strip("<>[]()\"'"))
            if norm:
                parsed = urlsplit(norm)
                if parsed.netloc:
                    return norm
    return ""


class _PersistentPSWorker:
    """Manages a single warm UTF-8 PowerShell worker process with non-blocking line reads."""

    def __init__(self, script_text: str, name: str) -> None:
        self._script_text = script_text
        self._name = name
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._io_lock = threading.Lock()

    def _start(self) -> None:
        self._stop_now()
        self._queue = queue.Queue()
        self._proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                self._script_text,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc = self._proc

        def _pump() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    self._queue.put(line)
            except Exception:
                pass
            finally:
                self._queue.put(None)

        self._reader = threading.Thread(target=_pump, daemon=True, name=f"ps-worker-{self._name}")
        self._reader.start()

    def _stop_now(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
                proc.wait(timeout=1.5)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

    def request(self, line_in: str, timeout_s: float = 8.0) -> dict[str, Any]:
        with self._io_lock:
            for attempt in range(2):
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                assert self._proc is not None and self._proc.stdin is not None
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                try:
                    self._proc.stdin.write(line_in + "\n")
                    self._proc.stdin.flush()
                    raw = self._queue.get(timeout=timeout_s)
                    if raw is None:
                        raise EOFError("worker stdout closed")
                    raw = raw.strip()
                    if not raw:
                        return {"ok": False, "error": "empty_response"}
                    return dict(json.loads(raw))
                except Exception as exc:
                    self._stop_now()
                    if attempt == 1:
                        return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "worker_failed"}

    def close(self) -> None:
        with self._io_lock:
            self._stop_now()


class PersistentWinRTOCR:
    """Warm Windows 11 WinRT OCR engine for zero-dependency desktop text & element extraction."""

    def __init__(self, timeout_s: float = 8.0) -> None:
        self._timeout_s = timeout_s
        self._worker = _PersistentPSWorker(_PS_WINRT_OCR_SCRIPT, name="winrt-ocr")
        self._call_lock = threading.Lock()
        self._seq = 0
        self._created_tmps: list[Path] = []
        self._tmp_path = Path(tempfile.gettempdir()) / f"cua_ocr_{os.getpid()}_{id(self):x}_0.bmp"
        self._last_cache_key: tuple[str, int, int] | None = None
        self._last_cache_val: dict[str, Any] | None = None

    def _cleanup_old_tmps(self, keep_latest: bool = True) -> None:
        limit = len(self._created_tmps) - 1 if keep_latest and self._created_tmps else len(self._created_tmps)
        kept: list[Path] = []
        for idx, p in enumerate(self._created_tmps):
            if idx >= limit:
                kept.append(p)
                continue
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                kept.append(p)
        self._created_tmps = kept

    def recognize(
        self,
        img: Image.Image,
        max_elements: int = 60,
        max_chars: int = 2400,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if img.width <= 1 or img.height <= 1:
            return {"ocr_elements": [], "page_text": "", "ocr_ms": 0.0, "inferred_url": ""}

        with self._call_lock:
            rgb = img if img.mode == "RGB" else img.convert("RGB")
            frame_hash = hashlib.blake2b(rgb.tobytes(), digest_size=16).hexdigest()
            cache_key = (frame_hash, int(max_elements), int(max_chars))
            if self._last_cache_key == cache_key and self._last_cache_val is not None:
                cached = dict(self._last_cache_val)
                cached["ocr_elements"] = [dict(el) for el in cached.get("ocr_elements", [])]
                cached["ocr_ms"] = 0.0
                return cached

            luma_sample = rgb.resize((min(160, rgb.width), min(90, rgb.height)), Image.Resampling.BILINEAR).convert("L")
            mean_luma = float(np.asarray(luma_sample, dtype=np.float32).mean())

            if rgb.width <= 1300:
                work = rgb.resize((rgb.width * 2, rgb.height * 2), Image.Resampling.LANCZOS)
            elif rgb.width < 1950:
                target_w = min(2560, round(rgb.width * 1.4))
                target_h = max(1, round(rgb.height * target_w / rgb.width))
                work = rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)
            else:
                work = rgb
            if mean_luma < 95.0:
                work = ImageOps.invert(work)

            self._seq += 1
            tmp_path = Path(tempfile.gettempdir()) / f"cua_ocr_{os.getpid()}_{id(self):x}_{self._seq}.bmp"
            self._tmp_path = tmp_path
            self._created_tmps.append(tmp_path)
            work.save(tmp_path, format="BMP")
            raw_res = self._worker.request(str(tmp_path), timeout_s=self._timeout_s)
            self._cleanup_old_tmps(keep_latest=True)

        if not raw_res.get("ok"):
            return {
                "ocr_elements": [],
                "page_text": "",
                "ocr_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "inferred_url": "",
                "ocr_error": str(raw_res.get("error", "")),
            }

        work_w = max(1, int(raw_res.get("width") or work.width) - 1)
        work_h = max(1, int(raw_res.get("height") or work.height) - 1)
        candidates: list[dict[str, Any]] = []
        for ln in raw_res.get("lines") or []:
            txt = _CJK_INTER_SPACE_RE.sub("", re.sub(r"\s+", " ", str(ln.get("text", ""))).strip())
            if not _should_keep_ocr_line(txt):
                continue
            box = ln.get("box") or [0, 0, 0, 0]
            if len(box) != 4:
                continue
            nx1 = min(max(round(box[0] / work_w * 1000.0), 0), 1000)
            ny1 = min(max(round(box[1] / work_h * 1000.0), 0), 1000)
            nx2 = min(max(round(box[2] / work_w * 1000.0), nx1), 1000)
            ny2 = min(max(round(box[3] / work_h * 1000.0), ny1), 1000)
            cx = (nx1 + nx2) // 2
            cy = (ny1 + ny2) // 2
            candidates.append(
                {
                    "text": txt,
                    "rect_norm": [nx1, ny1, nx2, ny2],
                    "center_norm": [cx, cy],
                }
            )

        candidates.sort(key=lambda c: (c["center_norm"][1] // 18, c["rect_norm"][0], c["rect_norm"][1]))

        ocr_elements: list[dict[str, Any]] = []
        text_lines: list[str] = []
        seen_text: set[str] = set()
        total_chars = 0
        for item in candidates:
            t = item["text"]
            if len(ocr_elements) < max_elements:
                ocr_elements.append(
                    {
                        "id": len(ocr_elements) + 1,
                        "text": t[:120],
                        "rect_norm": item["rect_norm"],
                        "center_norm": item["center_norm"],
                    }
                )
            low_key = t.lower()
            if low_key not in seen_text and total_chars < max_chars:
                seen_text.add(low_key)
                remaining = max_chars - total_chars
                snippet = t[:remaining]
                text_lines.append(snippet)
                total_chars += len(snippet) + 1

        inferred_url = _infer_url_from_lines(ocr_elements)
        result = {
            "ocr_elements": ocr_elements,
            "page_text": "\n".join(text_lines),
            "ocr_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "inferred_url": inferred_url,
        }
        with self._call_lock:
            self._last_cache_key = cache_key
            self._last_cache_val = {
                **result,
                "ocr_elements": [dict(el) for el in ocr_elements],
            }
        return result

    def close(self) -> None:
        self._worker.close()
        with self._call_lock:
            self._cleanup_old_tmps(keep_latest=False)
            if self._created_tmps:
                time.sleep(0.05)
                self._cleanup_old_tmps(keep_latest=False)


class DesktopChromeURLReader:
    """Reads desktop Chrome's top omnibox URL and visible tabs via bounded UIA walk."""

    def __init__(self, timeout_s: float = 4.0) -> None:
        self._timeout_s = timeout_s
        self._worker = _PersistentPSWorker(_PS_UIA_URL_SCRIPT, name="uia-url")

    def read_url_and_tabs(self, hwnd: int, max_y_px: int = 200) -> dict[str, Any]:
        if not hwnd:
            return {"url": "", "tabs": []}
        res = self._worker.request(f"{int(hwnd)},{int(max_y_px)}", timeout_s=self._timeout_s)
        if not res.get("ok"):
            return {"url": "", "tabs": []}

        edits = res.get("edits") or []
        url = ""
        # Prefer named Chrome address bar controls; fall back to highest URL-like Edit in top strip.
        for e in edits:
            name_low = str(e.get("name", "")).lower()
            val = _normalize_url_candidate(str(e.get("value", "")))
            if val and any(k in name_low for k in ("address", "地址", "omnibox", "搜索或输入")):
                url = val
                break
        if not url:
            for e in sorted(edits, key=lambda x: (x.get("top", 9999), x.get("left", 9999))):
                val = _normalize_url_candidate(str(e.get("value", "")))
                if val:
                    url = val
                    break

        tabs = []
        for idx, t in enumerate(res.get("tabs") or []):
            title = str(t.get("title", "")).split(" - 内存用量")[0].strip()
            tabs.append(
                {
                    "index": idx,
                    "title": title,
                    "box_screen": [t.get("left", 0), t.get("top", 0), t.get("right", 0), t.get("bottom", 0)],
                }
            )
        return {"url": url, "tabs": tabs}

    def close(self) -> None:
        self._worker.close()


def _clean_match_text(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "")).strip().lower()
    return t.strip("「」『』\"'“”‘’[]()（）")


def _strip_alnum_punct(s: str) -> str:
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)


def match_ocr_target(query: str, ocr_elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match an explicit label query against OCR elements; returns None if absent or ambiguous."""
    q = _clean_match_text(query)
    if not q or not ocr_elements:
        return None
    q_compact = _strip_alnum_punct(q)
    q_tokens = [tok for tok in re.split(r"[\s/|,;·•]+", q) if tok]

    scored: list[tuple[float, dict[str, Any]]] = []
    for el in ocr_elements:
        raw_t = str(el.get("text", ""))
        cand = _clean_match_text(raw_t)
        if not cand:
            continue
        score = 0.0
        if q == cand:
            score = 1.0
        elif len(q_compact) >= 2 and q_compact == _strip_alnum_punct(cand):
            score = 0.97
        elif len(q) >= 2 and q in cand:
            ratio = len(q) / max(1, len(cand))
            has_boundary = bool(
                _CJK_RE.search(q)
                or re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", cand)
            )
            if has_boundary and ratio >= 0.45:
                score = 0.89 + 0.06 * ratio
            else:
                score = 0.72 + 0.12 * ratio
        elif len(q_tokens) >= 2 and all(tok in cand for tok in q_tokens):
            ratio = sum(len(tok) for tok in q_tokens) / max(1, len(cand))
            score = 0.72 + min(0.18, 0.22 * ratio)
        elif len(cand) >= 4 and cand in q and len(cand) / max(1, len(q)) >= 0.6:
            score = 0.78
        if score > 0.0:
            scored.append((score, el))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_el = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.85:
        return None
    unique = (len(scored) == 1) or ((best_score - second_score) >= 0.08)
    if not unique:
        return None
    return {"element": best_el, "score": round(best_score, 4), "unique": True}


def analyze_click_effect(
    before_img: Image.Image,
    after_img: Image.Image,
    click_norm: tuple[int, int],
    before_ocr: list[dict[str, Any]] | None = None,
    after_ocr: list[dict[str, Any]] | None = None,
    title_before: str = "",
    title_after: str = "",
    url_before: str = "",
    url_after: str = "",
) -> dict[str, Any]:
    """Verify whether a click produced a real local/semantic UI change without false retries on small icons."""
    title_changed = bool(title_before != title_after and (title_before or title_after))
    url_changed = bool(url_before and url_after and url_before != url_after)

    if before_img.size != after_img.size:
        return {
            "effective": True,
            "title_changed": title_changed,
            "url_changed": url_changed,
            "global_changed_ratio": 1.0,
            "local_changed_ratio": 1.0,
            "highlight_delta": 0,
            "local_ocr_changes": [],
        }

    arr_b = np.asarray(before_img if before_img.mode == "RGB" else before_img.convert("RGB"), dtype=np.int16)
    arr_a = np.asarray(after_img if after_img.mode == "RGB" else after_img.convert("RGB"), dtype=np.int16)
    diff_max = np.max(np.abs(arr_a - arr_b), axis=2)
    global_changed_ratio = float(np.mean(diff_max >= 12))

    h, w = arr_b.shape[:2]
    cx = min(max(round(click_norm[0] / 1000.0 * max(1, w - 1)), 0), max(0, w - 1))
    cy = min(max(round(click_norm[1] / 1000.0 * max(1, h - 1)), 0), max(0, h - 1))

    half_local = 70  # 140x140 px ROI
    lx1, lx2 = max(0, cx - half_local), min(w, cx + half_local)
    ly1, ly2 = max(0, cy - half_local), min(h, cy + half_local)
    local_patch = diff_max[ly1:ly2, lx1:lx2]
    local_changed_ratio = float(np.mean(local_patch >= 12)) if local_patch.size else 0.0

    half_icon = 18  # 36x36 px ROI around tiny toggle/heart icons
    ix1, ix2 = max(0, cx - half_icon), min(w, cx + half_icon)
    iy1, iy2 = max(0, cy - half_icon), min(h, cy + half_icon)
    patch_b = arr_b[iy1:iy2, ix1:ix2]
    patch_a = arr_a[iy1:iy2, ix1:ix2]
    if patch_b.size and patch_a.size:
        sat_b = int(np.sum((np.max(patch_b, axis=2) - np.min(patch_b, axis=2)) >= 60))
        sat_a = int(np.sum((np.max(patch_a, axis=2) - np.min(patch_a, axis=2)) >= 60))
        highlight_delta = sat_a - sat_b
    else:
        highlight_delta = 0

    local_ocr_changes: list[str] = []
    if before_ocr is not None and after_ocr is not None:
        nx, ny = click_norm
        near_b = [
            str(e.get("text", "")).strip()
            for e in before_ocr
            if abs(e.get("center_norm", [0, 0])[0] - nx) <= 140
            and abs(e.get("center_norm", [0, 0])[1] - ny) <= 50
        ]
        near_a = [
            str(e.get("text", "")).strip()
            for e in after_ocr
            if abs(e.get("center_norm", [0, 0])[0] - nx) <= 140
            and abs(e.get("center_norm", [0, 0])[1] - ny) <= 50
        ]
        if near_b != near_a:
            removed = [t for t in near_b if t not in near_a]
            added = [t for t in near_a if t not in near_b]
            for r_txt, a_txt in zip(removed, added):
                local_ocr_changes.append(f"{r_txt} -> {a_txt}")
            for r_txt in removed[len(added):]:
                local_ocr_changes.append(f"-{r_txt}")
            for a_txt in added[len(removed):]:
                local_ocr_changes.append(f"+{a_txt}")

    effective = bool(
        title_changed
        or url_changed
        or local_changed_ratio > 0.008
        or local_ocr_changes
        or abs(highlight_delta) >= 18
        or global_changed_ratio > 0.015
    )
    return {
        "effective": effective,
        "title_changed": title_changed,
        "url_changed": url_changed,
        "global_changed_ratio": round(global_changed_ratio, 5),
        "local_changed_ratio": round(local_changed_ratio, 5),
        "highlight_delta": int(highlight_delta),
        "local_ocr_changes": local_ocr_changes,
    }
