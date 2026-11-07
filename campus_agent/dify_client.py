"""Server-only Dify Workflow proxy, with no automatic retry or rule fallback.

Only the notice and explicit time context leave this process. The caller owns
access control and request budgets; this module never accepts a URL or API key
from an HTTP request. No Dify response/error body is exposed on failures.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .candidates import normalize_extraction, validate_extraction_input


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_API_BASE = "https://api.dify.ai/v1"


class ExtractionServiceError(Exception):
    """A fixed public message, HTTP status and code; never an upstream detail."""

    def __init__(self, message: str, status: int = 502, code: str = "model_service_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True, repr=False)
class _Config:
    # repr=False is intentional: configuration must not accidentally log a key.
    endpoint: str
    key: str
    timeout: float


def _enabled() -> bool:
    return os.environ.get("SIXU_MODEL_EXTRACTION_ENABLED", "false").strip().lower() == "true"


def _config() -> _Config:
    if not _enabled():
        raise ExtractionServiceError("模型提取尚未启用，请先完成服务端配置；可主动选择规则提取。", 503, "model_disabled")
    key = os.environ.get("DIFY_API_KEY", "").strip()
    base = os.environ.get("DIFY_API_BASE", DEFAULT_API_BASE).strip()
    try:
        parsed = urllib.parse.urlsplit(base)
        port = parsed.port
        valid_base = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and not any(ord(character) < 33 or ord(character) > 126 for character in base)
            and not any(character in base for character in "?#\\")
            and (port is None or 1 <= port <= 65535)
        )
        # urllib's URL and header serialization require ASCII. An operator may
        # use a punycode host/path, never a browser-supplied redirect target.
        base.encode("ascii")
        timeout = float(os.environ.get("DIFY_TIMEOUT_SECONDS", "60"))
        valid_timeout = math.isfinite(timeout) and 5 <= timeout <= 90
    except (ValueError, UnicodeError):
        valid_base = valid_timeout = False
        timeout = 60.0
    if not valid_base or not valid_timeout or not re.fullmatch(r"[!-~]{1,1024}", key):
        raise ExtractionServiceError("模型服务配置不完整或不合规，请在服务端检查启用开关、地址、密钥和超时设置。", 503, "model_not_configured")
    return _Config(base.rstrip("/") + "/workflows/run", key, timeout)


def extraction_service_status() -> dict[str, bool]:
    """No credentials, addresses, provider output, or environment values."""
    enabled = _enabled()
    try:
        _config()
    except ExtractionServiceError:
        return {"configured": False, "enabled": enabled}
    return {"configured": True, "enabled": enabled}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        # urllib would otherwise forward a Bearer header to the redirect host.
        return None


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _strict_object(raw: str | bytes) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                       parse_float=_finite_float)
    if not isinstance(value, dict):
        raise ValueError("JSON output must be an object")
    return value


def _reject_secret_echo(value: object, key: str) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and key in item:
            raise ExtractionServiceError("模型服务响应包含受保护内容，本次候选未被采纳。", 502, "model_protected_content")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _fetch(config: _Config, request_body: bytes) -> dict:
    request = urllib.request.Request(config.endpoint, data=request_body,
                                     headers={"Authorization": "Bearer " + config.key,
                                              "Content-Type": "application/json", "Accept": "application/json"},
                                     method="POST")
    # Do not inherit ambient HTTP_PROXY settings that could receive the key.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=config.timeout) as response:
            if response.status != 200:
                raise ExtractionServiceError("模型服务返回异常状态，请稍后重试或检查服务端配置。", 502, "model_upstream_error")
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise ExtractionServiceError("模型服务响应超过大小上限，请缩短通知后重试。", 502, "model_response_too_large")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        upstream_status = exc.code
        # Do not read or preserve the upstream error body (it may echo secrets).
        exc.close()
        if 300 <= upstream_status < 400:
            raise ExtractionServiceError("模型服务返回重定向，已阻止转发密钥；请检查服务端地址。", 502, "model_redirect_blocked") from None
        if upstream_status in {401, 403}:
            raise ExtractionServiceError("模型服务拒绝凭据，请在服务端检查工作流密钥和发布状态。", 502, "model_auth_rejected") from None
        if upstream_status == 402:
            raise ExtractionServiceError("模型服务余额不足，请在服务商处充值后重试。", 502, "model_balance_insufficient") from None
        if upstream_status == 429:
            raise ExtractionServiceError("模型服务达到调用限制，请稍后再试。", 503, "model_upstream_rate_limited") from None
        raise ExtractionServiceError("模型服务暂时不可用，请稍后重试。", 502, "model_upstream_error") from None
    except (socket.timeout, TimeoutError):
        raise ExtractionServiceError("模型提取等待超时，本次未取得候选；请检查工作流运行记录后再决定是否重试。", 504, "model_timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ExtractionServiceError("模型提取等待超时，本次未取得候选；请检查工作流运行记录后再决定是否重试。", 504, "model_timeout") from None
        raise ExtractionServiceError("无法连接模型服务，请检查服务端网络和配置。", 502, "model_connection_error") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise ExtractionServiceError("模型服务连接或响应异常，请检查服务端配置后重试。", 502, "model_connection_error") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ExtractionServiceError("模型服务响应超过大小上限，请缩短通知后重试。", 502, "model_response_too_large")
    try:
        return _strict_object(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ExtractionServiceError("模型服务未返回合规JSON对象，本次候选未被采纳。", 502, "model_invalid_response") from None


def extract_notice(payload: object) -> dict:
    """Run once, then strictly validate quotations before returning draft data.

    The workflow's End node must publish `extraction` as a JSON string, without
    prose or Markdown fences. A timeout does not imply Dify cancelled its run;
    there is deliberately no automatic retry, fallback, or second model call.
    """
    text, reference_date, timezone = validate_extraction_input(payload)
    config = _config()
    body = json.dumps({"inputs": {"notice_text": text, "reference_date": reference_date or "", "timezone": timezone},
                       "response_mode": "blocking", "user": "sixu-local-workbench"}, ensure_ascii=False).encode("utf-8")
    response = _fetch(config, body)
    _reject_secret_echo(response, config.key)
    try:
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError
        if data.get("status") != "succeeded":
            raise ExtractionServiceError("工作流未成功完成，本次候选未被采纳；请检查Dify运行记录。", 502, "model_workflow_failed")
        outputs = data.get("outputs")
        extraction = outputs.get("extraction") if isinstance(outputs, dict) else None
        if not isinstance(extraction, str):
            raise ValueError
        raw = _strict_object(extraction)
        _reject_secret_echo(raw, config.key)
        return normalize_extraction(raw, text, reference_date, timezone)
    except ExtractionServiceError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ExtractionServiceError("模型输出不符合候选契约或原文引文校验，本次候选未被采纳，请核对工作流。", 502, "model_invalid_extraction") from None
