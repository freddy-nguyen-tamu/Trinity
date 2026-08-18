import json
import os
import re
import time

import requests


GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_MODEL_CACHE_TTL_SECONDS = 15 * 60
GROQ_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_last_groq_model.json")

RETIRED_GROQ_MODELS = {"llama-3.1-8b-instant"}
FALLBACK_GROQ_CHAT_MODELS = [
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "groq/compound-mini",
    "groq/compound",
]
NON_CHAT_MODEL_MARKERS = (
    "whisper",
    "tts",
    "audio",
    "guard",
    "safeguard",
    "playai",
    "orpheus",
)

LAST_SUCCESSFUL_GROQ_MODEL = None
GROQ_MODEL_CACHE = {"expires_at": 0.0, "ids": []}
NEXT_GROQ_KEY_START_INDEX = 0


def load_groq_keys():
    keys = []

    base_key = os.getenv("GROQ_API_KEY")
    if base_key and base_key.strip():
        keys.append(("GROQ_API_KEY", base_key.strip()))

    for index in range(1, 10):
        name = f"GROQ_API_KEY{index}"
        value = os.getenv(name)

        if value and value.strip():
            keys.append((name, value.strip()))

    return keys


def rotate_items(items, start_index):
    if not items:
        return items

    safe_index = start_index % len(items)
    return items[safe_index:] + items[:safe_index]


def configured_model_override():
    model = (os.getenv("GROQ_MODEL") or "").strip()

    if not model or model.casefold() in {"auto", "dynamic"}:
        return None

    if model in RETIRED_GROQ_MODELS:
        print(f"Ignoring retired Groq model override: {model}")
        return None

    return model


def load_saved_successful_model():
    global LAST_SUCCESSFUL_GROQ_MODEL

    if LAST_SUCCESSFUL_GROQ_MODEL:
        return LAST_SUCCESSFUL_GROQ_MODEL

    try:
        with open(GROQ_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    model = str(data.get("model") or "").strip() if isinstance(data, dict) else ""

    if model and is_likely_chat_model(model):
        LAST_SUCCESSFUL_GROQ_MODEL = model
        return model

    return None


def save_successful_model(model):
    global LAST_SUCCESSFUL_GROQ_MODEL

    model = (model or "").strip()

    if not model or not is_likely_chat_model(model):
        return

    LAST_SUCCESSFUL_GROQ_MODEL = model

    try:
        with open(GROQ_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"model": model, "updated_at": time.time()}, f, indent=2)
    except Exception as exc:
        print(f"Could not save last Groq model: {exc}")


def is_likely_chat_model(model_id):
    lower = model_id.casefold()

    if model_id in RETIRED_GROQ_MODELS:
        return False

    return not any(marker in lower for marker in NON_CHAT_MODEL_MARKERS)


def model_preference_rank(model_id):
    try:
        return FALLBACK_GROQ_CHAT_MODELS.index(model_id)
    except ValueError:
        pass

    lower = model_id.casefold()

    if "gpt-oss-20b" in lower:
        return 10
    if "llama-3.3" in lower:
        return 20
    if "gpt-oss-120b" in lower:
        return 30
    if "qwen" in lower:
        return 40
    if "llama" in lower:
        return 50
    if "compound-mini" in lower:
        return 60
    if "compound" in lower:
        return 70

    return 100


def sort_model_ids(model_ids):
    return sorted(model_ids, key=model_preference_rank)


def unique_model_ids(model_ids):
    seen = set()
    unique = []

    for model_id in model_ids:
        model_id = (model_id or "").strip()

        if not model_id or model_id in seen or not is_likely_chat_model(model_id):
            continue

        seen.add(model_id)
        unique.append(model_id)

    return unique


def summarize_groq_error(body):
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
        error = parsed.get("error") if isinstance(parsed, dict) else None
        parts = [
            error.get("code") if isinstance(error, dict) else None,
            error.get("type") if isinstance(error, dict) else None,
            error.get("message") if isinstance(error, dict) else None,
        ]
        parts = [part for part in parts if part]

        if parts:
            return ": ".join(parts)
    except Exception:
        pass

    return str(body)[:800]


def strip_reasoning_blocks(value):
    return re.sub(
        r"<think\b[^>]*>[\s\S]*?(?:</think>|$)",
        "",
        str(value or ""),
        flags=re.I,
    ).replace("</think>", "")


def strip_reasoning_from_response(data):
    try:
        message = data["choices"][0]["message"]
        content = message.get("content")

        if isinstance(content, str):
            message["content"] = strip_reasoning_blocks(content).strip()
    except Exception:
        pass

    return data


def parse_retry_after_seconds(resp, fallback=2.0):
    retry_after = resp.headers.get("retry-after")

    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass

    try:
        message = resp.json().get("error", {}).get("message", "")
        match = re.search(r"try again in\s+([0-9]*\.?[0-9]+)s", message, re.I)

        if match:
            return float(match.group(1))
    except Exception:
        pass

    return fallback


def get_groq_error_message(resp):
    try:
        return str(resp.json().get("error", {}).get("message", ""))
    except Exception:
        return resp.text or ""


def parse_request_token_counts(resp):
    message = get_groq_error_message(resp)

    requested_match = re.search(
        r"\bRequested\s+(\d+)",
        message,
        re.I,
    )
    limit_match = re.search(
        r"\bLimit\s+(\d+)",
        message,
        re.I,
    )

    requested_tokens = int(requested_match.group(1)) if requested_match else None
    limit_tokens = int(limit_match.group(1)) if limit_match else None

    return requested_tokens, limit_tokens


def default_is_request_too_large(resp):
    if resp.status_code == 413:
        return True

    message = get_groq_error_message(resp).casefold()
    size_markers = (
        "request too large",
        "context length",
        "context_length_exceeded",
        "maximum context",
        "too many tokens",
    )

    return resp.status_code in (400, 429) and any(
        marker in message
        for marker in size_markers
    )


def make_default_request_too_large_error(resp, detail, error_cls):
    requested_tokens, limit_tokens = parse_request_token_counts(resp)
    message = f"Groq request too large using {detail}: {resp.status_code} {resp.text}"

    try:
        return error_cls(
            message,
            requested_tokens=requested_tokens,
            limit_tokens=limit_tokens,
        )
    except TypeError:
        return error_cls(message)


def make_groq_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def is_reasoning_model(model_id):
    lower = model_id.casefold()
    return (
        "qwen" in lower
        or "gpt-oss" in lower
        or "deepseek" in lower
        or "minimax" in lower
        or "reasoning" in lower
    )


def reasoning_options(model_id, hide_reasoning):
    if not hide_reasoning or not is_reasoning_model(model_id):
        return {}

    lower = model_id.casefold()

    if "qwen" in lower:
        return {
            "reasoning_format": "hidden",
            "reasoning_effort": "none",
        }

    if "gpt-oss" in lower:
        return {
            "reasoning_format": "hidden",
            "reasoning_effort": "low",
        }

    return {
        "reasoning_format": "hidden",
    }


def is_reasoning_option_error(resp):
    if resp.status_code != 400:
        return False

    message = summarize_groq_error(resp.text).casefold()
    return (
        "reasoning_format" in message
        or "reasoning_effort" in message
        or "unsupported" in message
        or "unrecognized" in message
        or "extra" in message
    )


def discover_groq_model_ids(keys, timeout=12):
    now = time.time()

    if GROQ_MODEL_CACHE["expires_at"] > now:
        return GROQ_MODEL_CACHE["ids"]

    discovered = set()
    last_error = "unknown Groq model-list error"

    for key_name, api_key in rotate_items(keys, NEXT_GROQ_KEY_START_INDEX):
        try:
            resp = requests.get(
                GROQ_MODELS_URL,
                headers=make_groq_headers(api_key),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"{key_name} network error: {exc}"
            print(f"Could not list Groq models with {last_error}")
            continue

        if resp.status_code != 200:
            last_error = f"{key_name} HTTP {resp.status_code}: {summarize_groq_error(resp.text)}"
            print(f"Could not list Groq models with {last_error}")
            continue

        try:
            data = resp.json()
        except ValueError as exc:
            last_error = f"{key_name} invalid model-list JSON: {exc}"
            print(f"Could not list Groq models with {last_error}")
            continue

        for model in data.get("data", []):
            model_id = str(model.get("id") or "").strip()

            if model_id and model.get("active") is not False and is_likely_chat_model(model_id):
                discovered.add(model_id)

    ids = sort_model_ids(discovered)
    GROQ_MODEL_CACHE["expires_at"] = time.time() + GROQ_MODEL_CACHE_TTL_SECONDS
    GROQ_MODEL_CACHE["ids"] = ids

    if not ids:
        print(
            "Groq model discovery returned no chat candidates. "
            f"Falling back to built-in candidates. Last error: {last_error}"
        )

    return ids


def groq_model_candidates(keys):
    return unique_model_ids(
        [
            load_saved_successful_model(),
            configured_model_override(),
            *sort_model_ids(discover_groq_model_ids(keys)),
            *FALLBACK_GROQ_CHAT_MODELS,
        ]
    )


def groq_chat_try_all_models(
    messages,
    *,
    max_tokens=900,
    temperature=0.2,
    timeout=60,
    max_retry_rounds=8,
    json_mode=False,
    hide_reasoning=True,
    request_too_large_error_cls=RuntimeError,
    is_request_too_large_func=None,
    make_request_too_large_error_func=None,
):
    global NEXT_GROQ_KEY_START_INDEX

    keys = load_groq_keys()

    if not keys:
        raise RuntimeError(
            "No Groq API keys are available. Set GROQ_API_KEY or GROQ_API_KEY1 through GROQ_API_KEY9."
        )

    models = groq_model_candidates(keys)

    if not models:
        raise RuntimeError("No usable Groq chat models were discovered.")

    print("Loaded Groq keys: " + ", ".join(name for name, _ in keys))
    print("Trying Groq models in priority order: " + ", ".join(models))

    backoff = 1.0
    disabled_keys = set()

    for round_no in range(1, max_retry_rounds + 1):
        ordered_keys = rotate_items(keys, NEXT_GROQ_KEY_START_INDEX)
        waits = []
        temporary_errors = []
        last_error = ""

        for model in models:
            for key_name, api_key in ordered_keys:
                if key_name in disabled_keys:
                    continue

                print(f"Trying {key_name} with {model}...")
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_completion_tokens": max_tokens,
                }

                if json_mode:
                    payload["response_format"] = {"type": "json_object"}

                reasoning_payload = reasoning_options(model, hide_reasoning)
                payload_variants = (
                    [{**payload, **reasoning_payload}, payload]
                    if reasoning_payload
                    else [payload]
                )
                resp = None

                for variant_index, request_payload in enumerate(payload_variants):
                    try:
                        resp = requests.post(
                            GROQ_CHAT_COMPLETIONS_URL,
                            headers=make_groq_headers(api_key),
                            json=request_payload,
                            timeout=timeout,
                        )
                    except requests.RequestException as exc:
                        last_error = f"{key_name} {model} network error: {exc}"
                        temporary_errors.append(last_error)
                        print(f"[NET] {last_error}")
                        print("Trying next key/model before sleeping...")
                        resp = None
                        break

                    if variant_index == 0 and reasoning_payload and is_reasoning_option_error(resp):
                        print(
                            f"[400] {model} rejected hidden-reasoning options. "
                            "Retrying the same key/model without them..."
                        )
                        continue

                    break

                if resp is None:
                    continue

                if resp.status_code == 200:
                    original_index = next(
                        i for i, (name, _) in enumerate(keys)
                        if name == key_name
                    )
                    NEXT_GROQ_KEY_START_INDEX = (original_index + 1) % len(keys)
                    data = resp.json()
                    used_model = str(data.get("model") or model).strip()
                    save_successful_model(used_model)

                    print(f"[OK] Groq succeeded using {key_name} with {used_model}.")
                    data["_groq"] = {"model": used_model, "key_name": key_name}
                    return strip_reasoning_from_response(data)

                is_request_too_large_check = (
                    is_request_too_large_func
                    if is_request_too_large_func is not None
                    else default_is_request_too_large
                )

                if is_request_too_large_check(resp):
                    detail = f"{key_name} with {model}"
                    if make_request_too_large_error_func is not None:
                        raise make_request_too_large_error_func(resp, detail)

                    raise make_default_request_too_large_error(
                        resp,
                        detail,
                        request_too_large_error_cls,
                    )

                if resp.status_code == 400:
                    try:
                        code = resp.json().get("error", {}).get("code", "")
                    except Exception:
                        code = ""

                    if code == "json_validate_failed" and json_mode:
                        print("[400] JSON validation failed in JSON mode. Retrying without JSON mode...")
                        return groq_chat_try_all_models(
                            messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            timeout=timeout,
                            max_retry_rounds=max_retry_rounds,
                            json_mode=False,
                            hide_reasoning=hide_reasoning,
                            request_too_large_error_cls=request_too_large_error_cls,
                            is_request_too_large_func=is_request_too_large_func,
                            make_request_too_large_error_func=make_request_too_large_error_func,
                        )

                last_error = (
                    f"{key_name} {model} HTTP {resp.status_code}: "
                    f"{summarize_groq_error(resp.text)}"
                )
                print(f"[{resp.status_code}] Groq API error. {last_error}")

                if resp.status_code in (401, 403):
                    disabled_keys.add(key_name)
                    continue

                if resp.status_code == 429:
                    waits.append(max(parse_retry_after_seconds(resp, backoff), backoff) + 0.25)
                    continue

                if resp.status_code in (408, 409, 500, 502, 503, 504):
                    temporary_errors.append(last_error)
                    waits.append(max(parse_retry_after_seconds(resp, backoff), backoff))
                    continue

        if round_no == max_retry_rounds:
            raise RuntimeError(
                "All Groq API keys/models failed too many times. Last error: "
                + (last_error or "; ".join(temporary_errors[-3:]) or "unknown")
            )

        wait_s = max(waits) if waits else backoff + 0.25
        print(
            "All loaded Groq key/model combinations failed or were rate limited. "
            f"Sleeping {wait_s:.2f}s before retry round {round_no}/{max_retry_rounds}."
        )
        time.sleep(wait_s)
        backoff = min(backoff * 1.6, 30.0)

    raise RuntimeError("Groq API error: too many retries across all API keys/models.")
