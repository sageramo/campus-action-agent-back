"""Server-only DeepSeek chat client for structuring helpers (no retry, no fallback).

The key comes only from this process's environment (entered by the user through
hidden input) and is sent only as the Authorization header. Thinking mode is
turned off: these helpers reformat text and should answer in seconds.
"""
from __future__ import annotations

import json
import math
import os

from .dify_client import ExtractionServiceError, _Config, _fetch, _reject_secret_echo, _strict_object

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


def _config() -> _Config:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base = os.environ.get("DEEPSEEK_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")
    try:
        timeout = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "60"))
    except ValueError:
        timeout = math.nan
    if not key or not all(33 <= ord(character) <= 126 for character in key) or not base.startswith("https://") \
            or not (math.isfinite(timeout) and 5 <= timeout <= 120):
        raise ExtractionServiceError("DeepSeek 尚未配置，请在本机启动服务时输入 API Key。", 503, "model_not_configured")
    return _Config(base + "/chat/completions", key, timeout)


def deepseek_status() -> dict[str, bool]:
    """Booleans only; never the key, address or model."""
    try:
        _config()
    except ExtractionServiceError:
        return {"configured": False}
    return {"configured": True}


def chat_json(system: str, user: str, max_tokens: int = 8000, image: str | None = None) -> dict:
    """One JSON-mode call; blank or non-JSON content is an error, not an empty answer.

    An image (a checked data URL) goes in the user message, the only place the API accepts it.
    """
    config = _config()
    content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": image}}] if image else user
    body = {"model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL), "stream": False,
            "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    response = _fetch(config, json.dumps(body, ensure_ascii=False).encode("utf-8"))
    _reject_secret_echo(response, config.key)
    try:
        choice = response["choices"][0]
        content = choice["message"]["content"]
        if choice.get("finish_reason") == "length":
            raise ExtractionServiceError("模型输出被截断，请把内容分成几段后重试。", 502, "model_output_truncated")
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        return _strict_object(content)
    except ExtractionServiceError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ExtractionServiceError("模型没有返回可用的JSON结果，本次未采用；可以稍后重试。", 502, "model_invalid_extraction") from None
