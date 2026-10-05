"""OpenAI-compatible HTTP transport for HanGauge.

Stdlib-only. Candidate answering and model-based grading both use this module so
request validation, transport errors, and secret redaction stay in one place.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


RESPONSE_TRANSPORT = "openai_chat_completions"
TOOL_AUDIT_STATUS = "not_applicable"
ALLOWED_SCOPES = {"all", "per_task", "one"}
KNOWN_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
KNOWN_USAGE_DETAIL_KEYS = {
    "prompt_tokens_details": ("cached_tokens", "audio_tokens"),
    "completion_tokens_details": (
        "reasoning_tokens",
        "audio_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
    ),
}
SYSTEM_PROMPT = (
    "You are evaluating one frozen benchmark input. Use only the material in the "
    "user JSON. Return only the final answer as JSON required by the task. Do not "
    "include explanations, markdown, hidden reasoning, or extra text."
)
BEARER_RE = re.compile(r"Bearer\s+[^\s,;]+", re.IGNORECASE)
SECRET_TOKEN_RE = re.compile(r"\b(?:sk|sess|pat)-[A-Za-z0-9_\-]{8,}\b")


class EnvelopeError(ValueError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAIChatClient:
    """Single stateless OpenAI chat-completions caller."""

    def __init__(self, config: dict, api_key: str = ""):
        self.config = validate_openai_config(config)
        self.api_key = normalize_api_key(api_key)
        self.url = chat_completions_url(self.config["base_url"])

    def request(self, messages: list[dict], *, json_mode: bool = False) -> dict:
        started_at = utc_now()
        started_mono = time.monotonic()
        http_status = None
        try:
            request_body = {
                "model": self.config["model"],
                "messages": messages,
                "temperature": self.config["temperature"],
                "max_tokens": self.config["max_tokens"],
                "stream": False,
            }
            if json_mode:
                request_body["response_format"] = {"type": "json_object"}
            if "enable_thinking" in self.config:
                request_body["chat_template_kwargs"] = {"enable_thinking": self.config["enable_thinking"]}
            response_payload, http_status = post_json(self.url, request_body, self.api_key)
            raw, finish_reason = extract_raw_output(response_payload)
            if self.api_key and self.api_key in raw:
                return failure_record(
                    started_at,
                    started_mono,
                    "credential-leak: model output contained the supplied API key; raw output was discarded",
                    http_status=http_status,
                    api_key=self.api_key,
                )
            finished_at = utc_now()
            call = base_call("completed", started_at, finished_at, started_mono)
            call.update(
                {
                    "model_used": clean_metadata_string(response_payload.get("model"), self.api_key),
                    "finish_reason": clean_metadata_string(finish_reason, self.api_key),
                    "usage": extract_usage(response_payload.get("usage")),
                }
            )
            if http_status is not None:
                call["http_status"] = http_status
            return {"raw": raw, "call": call}
        except urllib.error.HTTPError as exc:
            http_status = getattr(exc, "code", None)
            if http_status is not None and 300 <= http_status < 400:
                error = "redirect response rejected; redirects are not followed"
            else:
                error = http_error_message(exc, self.api_key)
            return failure_record(started_at, started_mono, error, http_status=http_status, api_key=self.api_key)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return failure_record(
                started_at,
                started_mono,
                f"network error: {sanitize_text(str(reason), self.api_key)}",
                http_status=http_status,
                api_key=self.api_key,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return failure_record(
                started_at,
                started_mono,
                "invalid response: response body is not valid UTF-8 JSON",
                http_status=http_status,
                api_key=self.api_key,
            )
        except EnvelopeError as exc:
            return failure_record(
                started_at,
                started_mono,
                f"invalid OpenAI response envelope: {sanitize_text(str(exc), self.api_key)}",
                http_status=http_status,
                api_key=self.api_key,
            )
        except ValueError as exc:
            return failure_record(
                started_at,
                started_mono,
                f"invalid config: {sanitize_text(str(exc), self.api_key)}",
                http_status=http_status,
                api_key=self.api_key,
            )


CANDIDATE_DEFAULTS = {"temperature": 0, "max_tokens": 4096, "scope": "all"}
GRADER_DEFAULTS = {"temperature": 0, "max_tokens": 768}


def default_settings() -> dict:
    return {
        "candidate": {"base_url": "", "model": "", **CANDIDATE_DEFAULTS},
        "grader": {"base_url": "", "model": "", **GRADER_DEFAULTS},
    }


def validate_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings must be an object")
    reject_secret_fields(payload, allow_api_key=False)
    candidate = normalize_public_config(
        payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {},
        defaults=CANDIDATE_DEFAULTS,
        require_base_model=False,
        include_scope=True,
        allow_api_key=False,
    )
    grader = normalize_public_config(
        payload.get("grader") if isinstance(payload.get("grader"), dict) else {},
        defaults=GRADER_DEFAULTS,
        require_base_model=False,
        include_scope=False,
        allow_api_key=False,
    )
    return {"candidate": candidate, "grader": grader}



def validate_candidate_config(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("config must be an object")
    return normalize_public_config(payload, defaults=CANDIDATE_DEFAULTS, require_base_model=True, include_scope=True, allow_api_key=True)


def validate_grader_config(payload: dict, *, require_base_model: bool = True) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("grader config must be an object")
    return normalize_public_config(payload, defaults=GRADER_DEFAULTS, require_base_model=require_base_model, include_scope=False, allow_api_key=True)


def validate_openai_config(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("config must be an object")
    return normalize_public_config(payload, defaults=GRADER_DEFAULTS, require_base_model=True, include_scope=False, allow_api_key=True)


def public_grading_provider(config: dict) -> dict:
    public = validate_grader_config(config, require_base_model=True)
    return {"type": RESPONSE_TRANSPORT, **public}


def evaluate_case(input_obj: dict, config: dict, api_key: str) -> dict:
    """Evaluate one frozen public input with one OpenAI chat-completions request."""
    client = OpenAIChatClient(validate_candidate_config(config), api_key)
    return client.request(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(input_obj, ensure_ascii=False)},
        ]
    )


def normalize_public_config(
    payload: dict,
    *,
    defaults: dict,
    require_base_model: bool,
    include_scope: bool,
    allow_api_key: bool,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("config must be an object")
    reject_secret_fields(payload, allow_api_key=allow_api_key)
    base_url = payload.get("base_url", "")
    model = payload.get("model", "")
    if require_base_model:
        base_url = normalize_base_url(base_url)
        model = normalize_model(model)
    else:
        base_url = "" if base_url in (None, "") else normalize_base_url(base_url)
        model = "" if model in (None, "") else normalize_model(model)
    config = {
        "base_url": base_url,
        "model": model,
        "temperature": normalize_temperature(payload.get("temperature", defaults["temperature"])),
        "max_tokens": normalize_max_tokens(payload.get("max_tokens", defaults["max_tokens"])),
    }
    if include_scope:
        config["scope"] = normalize_scope(payload.get("scope", defaults.get("scope", "all")))
    if "enable_thinking" in payload and payload.get("enable_thinking") is not None and payload.get("enable_thinking") != "":
        config["enable_thinking"] = normalize_bool(payload.get("enable_thinking"), "enable_thinking")
    return config


def reject_secret_fields(value: Any, *, allow_api_key: bool) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = key.lower() if isinstance(key, str) else ""
            if lowered in {"authorization", "secret", "token"} or (lowered == "api_key" and not allow_api_key):
                raise ValueError("public config must not include secrets")
            if lowered == "api_key" and allow_api_key:
                continue
            reject_secret_fields(nested, allow_api_key=allow_api_key)
    elif isinstance(value, list):
        for item in value:
            reject_secret_fields(item, allow_api_key=allow_api_key)


def normalize_api_key(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ValueError("API Key must be a single-line string")
    return value.strip()


def normalize_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("base_url is required")
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    try:
        parsed.port
    except ValueError:
        raise ValueError("base_url port is invalid") from None
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError("base_url must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not include query or fragment")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def normalize_model(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("model is required")
    model = value.strip()
    if not model:
        raise ValueError("model is required")
    return model


def normalize_temperature(value: Any) -> float | int:
    if value == "" or value is None:
        value = 0
    if isinstance(value, bool):
        raise ValueError("temperature must be a number")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise ValueError("temperature must be a number") from exc
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("temperature must be a finite number")
    if value < 0 or value > 2:
        raise ValueError("temperature must be between 0 and 2")
    return int(value) if float(value).is_integer() else float(value)


def normalize_max_tokens(value: Any) -> int:
    if value == "" or value is None:
        value = 4096
    if isinstance(value, bool):
        raise ValueError("max_tokens must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or not re.fullmatch(r"[0-9]+", text):
            raise ValueError("max_tokens must be an integer")
        value = int(text)
    if not isinstance(value, int):
        raise ValueError("max_tokens must be an integer")
    if value < 1 or value > 131072:
        raise ValueError("max_tokens must be between 1 and 131072")
    return value


def normalize_scope(value: Any) -> str:
    if value == "" or value is None:
        value = "all"
    if not isinstance(value, str):
        raise ValueError("scope must be one of all, per_task, one")
    scope = value.strip()
    if scope not in ALLOWED_SCOPES:
        raise ValueError("scope must be one of all, per_task, one")
    return scope


def normalize_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} must be true or false")


def chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def post_json(url: str, payload: dict, api_key: str) -> tuple[dict, int | None]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "hangauge-openai-client/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(NoRedirectHandler)
    with opener.open(request) as response:
        status = getattr(response, "status", None) or response.getcode()
        response_text = response.read().decode("utf-8")
    if status is not None and (status < 200 or status >= 300):
        raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, None)
    parsed = json.loads(response_text)
    if not isinstance(parsed, dict):
        raise EnvelopeError("top-level response is not an object")
    return parsed, status


def extract_raw_output(response_payload: dict) -> tuple[str, Any]:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise EnvelopeError("choices[0].message.content is missing")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise EnvelopeError("choices[0] is not an object")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise EnvelopeError("choices[0].message is missing")
    content = message.get("content")
    if isinstance(content, str):
        return content, first_choice.get("finish_reason")
    if content is None and isinstance(message.get("refusal"), str):
        return message["refusal"], first_choice.get("finish_reason")
    raise EnvelopeError("choices[0].message.content is not a string")


def extract_usage(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    usage = {}
    for key in KNOWN_USAGE_KEYS:
        token_value = value.get(key)
        if isinstance(token_value, int) and not isinstance(token_value, bool):
            usage[key] = token_value
    for details_key, allowed_keys in KNOWN_USAGE_DETAIL_KEYS.items():
        details = value.get(details_key)
        if not isinstance(details, dict):
            continue
        clean_details = {}
        for key in allowed_keys:
            token_value = details.get(key)
            if isinstance(token_value, int) and not isinstance(token_value, bool):
                clean_details[key] = token_value
        if clean_details:
            usage[details_key] = clean_details
    return usage


def base_call(status: str, started_at: str, finished_at: str, started_mono: float) -> dict:
    return {
        "status": status,
        "tool_audit_status": TOOL_AUDIT_STATUS,
        "response_transport": RESPONSE_TRANSPORT,
        "started_at": started_at,
        "finished_at": finished_at,
        "latency_seconds": round(max(0.0, time.monotonic() - started_mono), 6),
    }


def failure_record(
    started_at: str,
    started_mono: float,
    error: str,
    *,
    http_status: int | None = None,
    api_key: str = "",
) -> dict:
    finished_at = utc_now()
    call = base_call("failed", started_at, finished_at, started_mono)
    if http_status is not None:
        call["http_status"] = http_status
    return {"call": call, "error": sanitize_text(error, api_key)}


def http_error_message(exc: urllib.error.HTTPError, api_key: str = "") -> str:
    status = getattr(exc, "code", None)
    message = str(getattr(exc, "reason", None) or getattr(exc, "msg", ""))
    try:
        payload = json.loads(exc.read(65536))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
    except (ValueError, OSError):
        pass
    finally:
        exc.close()
    message = sanitize_text(message, api_key)
    return f"HTTP {status}: {message}" if status is not None else "HTTP error"


def clean_metadata_string(value: Any, api_key: str) -> str | None:
    if not isinstance(value, str):
        return None
    return sanitize_text(value, api_key)


def sanitize_text(text: str, api_key: str = "", *extra_secrets: str) -> str:
    cleaned = text
    for secret in (api_key, *extra_secrets):
        if secret:
            cleaned = cleaned.replace(secret, "[redacted-secret]")
    cleaned = BEARER_RE.sub("Bearer [redacted-secret]", cleaned)
    cleaned = SECRET_TOKEN_RE.sub("[redacted-secret]", cleaned)
    return cleaned


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
