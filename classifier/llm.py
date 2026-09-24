"""Gemini / Claude vision transport: request building, retries, rate limits.

Callers (``classifier.asset_classifier``) own prompts, schemas, model choice,
and result normalization. This module only moves images + text to a model
and returns the parsed JSON reply.

Pacing: every call waits on a process-wide limiter (``GEMINI_RPM`` /
``CLAUDE_RPM``). A 429/503 puts the whole limiter into cooldown, so parallel
classify workers back off together instead of each hammering the quota.
This replaces the old fixed ``GEMINI_DELAY_S`` sleep after every site.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import threading
import time
from typing import Any

import anthropic
from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types
from PIL import Image

from envutil import env_float, env_int, env_str

logger = logging.getLogger(__name__)

UNCLEAR_REPLY: dict[str, Any] = {"site_type": "unclear", "site_confidence": 0.0}

GEMINI_RETRIES = env_int("GEMINI_RETRIES", 6)
GEMINI_RETRY_BASE_S = env_float("GEMINI_RETRY_BASE_S", 20)
CLAUDE_RETRY_BASE_S = 15.0


class RateLimiter:
    """Minimum spacing between calls, shared across threads, with cooldown."""

    def __init__(self, per_minute: float) -> None:
        self.interval_s = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._cooldown_until = 0.0

    def acquire(self) -> float:
        """Block until this caller's slot. Returns seconds waited."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot, self._cooldown_until)
            self._next_slot = slot + self.interval_s
        wait = slot - now
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def cooldown(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` (rate limit / overload seen)."""
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until, time.monotonic() + max(0.0, seconds)
            )


GEMINI_LIMITER = RateLimiter(env_float("GEMINI_RPM", 30))
CLAUDE_LIMITER = RateLimiter(env_float("CLAUDE_RPM", 50))


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _image_block(img: Image.Image) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(_jpeg_bytes(img)).decode("ascii"),
        },
    }


def _gemini_image_part(img: Image.Image) -> genai_types.Part:
    return genai_types.Part.from_bytes(data=_jpeg_bytes(img), mime_type="image/jpeg")


def _views_to_claude_content(views: list[tuple[str, Image.Image]], prompt: str) -> list:
    content = []
    for label, img in views:
        content.append({"type": "text", "text": f"View: {label}"})
        content.append(_image_block(img))
    content.append({"type": "text", "text": prompt})
    return content


def _views_to_gemini_contents(views: list[tuple[str, Image.Image]], prompt: str) -> list:
    contents = []
    for label, img in views:
        contents.append(f"View: {label}")
        contents.append(_gemini_image_part(img))
    contents.append(prompt)
    return contents


def _parse_json_fallback(text: str, default: dict) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {**default, "site_evidence": f"unparseable model reply: {text[:200]}"}


# --------------------------------- Gemini -----------------------------------


def _gemini_http_status(exc: Exception) -> int | None:
    """Extract an HTTP status from a Gemini SDK or transport error."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    message = str(exc)
    if " 429 " in message or "429" in message[:8]:
        return 429
    if " 503 " in message:
        return 503
    return None


def _gemini_retry_after_s(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return None


def _gemini_retry_wait_s(attempt: int, exc: Exception) -> float:
    """Backoff for transient Gemini rate limits (429) and outages (503)."""
    retry_after = _gemini_retry_after_s(exc)
    if retry_after is not None:
        return retry_after + random.uniform(0.0, 2.0)
    wait = GEMINI_RETRY_BASE_S * (2 ** max(0, attempt - 1))
    return min(wait, 120.0) + random.uniform(0.0, 3.0)


def call_gemini_json(
    client: genai.Client,
    contents: list,
    *,
    model: str,
    config: genai_types.GenerateContentConfig,
    retries: int | None = None,
) -> dict:
    """One structured-JSON Gemini call. Raw parsed reply (not normalized)."""
    max_retries = GEMINI_RETRIES if retries is None else retries
    attempt = 0
    while True:
        GEMINI_LIMITER.acquire()
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=config
            )
            break
        except Exception as exc:
            status = _gemini_http_status(exc)
            if status in (429, 503) and attempt < max_retries:
                attempt += 1
                wait = _gemini_retry_wait_s(attempt, exc)
                label = "rate limit" if status == 429 else "service unavailable"
                logger.info(
                    "transient Gemini %s (%s), cooling down %.0fs (%s/%s)",
                    status, label, wait, attempt, max_retries,
                )
                GEMINI_LIMITER.cooldown(wait)
                continue
            raise
    return _parse_json_fallback(resp.text or "", UNCLEAR_REPLY)


# --------------------------------- Claude -----------------------------------

# Primary model first; hops to the next on persistent rate limits or 404.
# claude-sonnet-4-20250514 was retired 2026-06-15; use current IDs from:
# https://docs.anthropic.com/en/docs/about-claude/models/overview
_DEFAULT_MODELS = "claude-sonnet-4-6,claude-haiku-4-5-20251001"
MODELS = [
    m.strip() for m in env_str("CLAUDE_MODELS", _DEFAULT_MODELS).split(",") if m.strip()
]
_model_idx = 0
_model_lock = threading.Lock()


def _current_model() -> str:
    with _model_lock:
        return MODELS[_model_idx]


def _hop_model(from_model: str, reason: str) -> bool:
    """Advance the shared fallback list past ``from_model``. False at the end."""
    global _model_idx
    with _model_lock:
        if MODELS[_model_idx] != from_model:
            return True  # another thread already hopped
        if _model_idx + 1 >= len(MODELS):
            return False
        _model_idx += 1
        logger.info("%s %s -> hopping to %s", from_model, reason, MODELS[_model_idx])
        return True


def _extract_tool_result(resp, tool_name: str, default: dict) -> dict:
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool_name:
            if isinstance(block.input, dict):
                return dict(block.input)
    text_parts = [block.text for block in resp.content if block.type == "text"]
    return _parse_json_fallback("\n".join(text_parts), default)


def call_claude_json(
    client: Anthropic,
    content: list,
    schema: dict,
    tool_name: str,
    *,
    retries: int = 3,
    model: str | None = None,
) -> tuple[dict, str]:
    """Tool-forced JSON Claude call. Returns (raw reply, model used).

    An explicit ``model`` never hops: its rate-limit / API errors raise.
    """
    attempt = 0
    while True:
        use_model = model or _current_model()
        CLAUDE_LIMITER.acquire()
        try:
            resp = client.messages.create(
                model=use_model,
                max_tokens=1000,
                tools=[{
                    "name": tool_name,
                    "description": "Return structured analysis as JSON.",
                    "input_schema": schema,
                }],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": content}],
            )
            return _extract_tool_result(resp, tool_name, UNCLEAR_REPLY), use_model
        except anthropic.RateLimitError:
            if model:
                raise
            if attempt < retries:
                attempt += 1
                CLAUDE_LIMITER.cooldown(CLAUDE_RETRY_BASE_S * attempt)
                continue
            if _hop_model(use_model, "rate limited"):
                attempt = 0
                continue
            raise
        except anthropic.APIStatusError as e:
            if model:
                raise
            if e.status_code == 404:
                if _hop_model(use_model, "not found (404)"):
                    attempt = 0
                    continue
                raise SystemExit(
                    f"\nClaude model '{use_model}' returned 404 (not found). "
                    f"Tried: {', '.join(MODELS)}\n"
                    "Set CLAUDE_MODELS to valid IDs, e.g. "
                    "claude-sonnet-4-6,claude-haiku-4-5-20251001\n"
                    "See https://docs.anthropic.com/en/docs/about-claude/models/overview"
                ) from e
            if e.status_code in (429, 529, 503, 500) and attempt < retries:
                attempt += 1
                CLAUDE_LIMITER.cooldown(CLAUDE_RETRY_BASE_S * attempt)
                continue
            if e.status_code in (429, 529) and _hop_model(use_model, "overloaded"):
                attempt = 0
                continue
            raise
