"""Strict JSON parser and validator for Phase 1 closed Chrome GUI action space."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Optional

ALLOWED_ACTIONS = {
    "left_click",
    "double_click",
    "right_click",
    "move",
    "scroll",
    "type",
    "hotkey",
    "wait",
    "done",
    "not_found",
}
COORDINATE_ACTIONS = {"left_click", "double_click", "right_click", "move", "scroll"}
SCROLL_DIRECTIONS = {"up", "down", "left", "right"}

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class ParsedAction:
    """Validated action payload ready for execution or safe no-op."""

    action: str
    coordinate: Optional[tuple[int, int]] = None
    direction: Optional[str] = None
    amount: Optional[int] = None
    text: Optional[str] = None
    keys: Optional[tuple[str, ...]] = None
    seconds: Optional[float] = None
    reason: Optional[str] = None
    raw: dict[str, Any] = None  # type: ignore[assignment]


class ActionValidationError(ValueError):
    """Raised when model output violates the closed Phase 1 action schema."""


def _extract_json_dict(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise ActionValidationError("No JSON object found in model output")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ActionValidationError(f"Invalid JSON syntax: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ActionValidationError("Top-level JSON value must be an object")
    return parsed


def _parse_coordinate(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ActionValidationError(f"coordinate must be [x, y], got {value!r}")
    x, y = value
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise ActionValidationError(f"coordinate values must be numbers, got {value!r}")
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        raise ActionValidationError(f"coordinate out of [0, 1000] bounds: {value!r}")
    return round(float(x)), round(float(y))


def parse_and_validate_action(raw_text: str) -> ParsedAction:
    """Parse raw VLM text and enforce strict Phase 1 schema constraints."""
    data = _extract_json_dict(raw_text)
    action = data.get("action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise ActionValidationError(f"Unsupported action: {action!r}")

    coord: Optional[tuple[int, int]] = None
    if action in COORDINATE_ACTIONS:
        coord = _parse_coordinate(data.get("coordinate"))

    if action == "scroll":
        direction = data.get("direction")
        if not isinstance(direction, str) or direction not in SCROLL_DIRECTIONS:
            raise ActionValidationError(f"Invalid scroll direction: {direction!r}")
        amount = data.get("amount", 3)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not (1 <= amount <= 20):
            raise ActionValidationError(f"scroll amount must be in [1, 20], got {amount!r}")
        return ParsedAction(action=action, coordinate=coord, direction=direction, amount=int(round(amount)), raw=data)

    if action == "type":
        text = data.get("text")
        if not isinstance(text, str) or not text:
            raise ActionValidationError("type action requires non-empty 'text'")
        optional_coord = _parse_coordinate(data["coordinate"]) if data.get("coordinate") is not None else None
        return ParsedAction(action=action, coordinate=optional_coord, text=text, raw=data)

    if action == "hotkey":
        keys = data.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k.strip() for k in keys):
            raise ActionValidationError(f"hotkey action requires non-empty string list 'keys', got {keys!r}")
        return ParsedAction(action=action, keys=tuple(k.strip().lower() for k in keys), raw=data)

    if action == "wait":
        seconds = data.get("seconds", 1.0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not (0.1 <= seconds <= 10.0):
            raise ActionValidationError(f"wait seconds must be in [0.1, 10.0], got {seconds!r}")
        return ParsedAction(action=action, seconds=float(seconds), raw=data)

    if action == "not_found":
        reason = data.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ActionValidationError("not_found reason must be a string if provided")
        return ParsedAction(action=action, reason=reason or "", raw=data)

    return ParsedAction(action=action, coordinate=coord, raw=data)
