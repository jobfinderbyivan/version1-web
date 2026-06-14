"""Claude API wrapper.

Every call records token usage into the token_usage table for the admin
budget dashboard. When no ANTHROPIC_API_KEY is configured, callers receive
None and fall back to heuristic implementations so the app remains usable.
"""
import json
import logging
import re

from . import config, db

log = logging.getLogger("llm")

_client = None
_client_failed = False


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY) and not _client_failed


def _get_client():
    global _client, _client_failed
    if not config.ANTHROPIC_API_KEY:
        return None  # heuristic fallback mode — don't construct a keyless client
    if _client is None and not _client_failed:
        try:
            import anthropic
            _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        except Exception:
            log.exception("Failed to initialise Anthropic client")
            _client_failed = True
    return _client


def _record_usage(usage, process_type: str, user_id):
    try:
        tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
        pricing = config.MODEL_PRICING.get(config.LLM_MODEL, (5.0, 25.0))
        cost = (usage.input_tokens or 0) / 1e6 * pricing[0] + (usage.output_tokens or 0) / 1e6 * pricing[1]
        db.execute(
            "INSERT INTO token_usage (user_id, process_type, tokens_used, estimated_cost) VALUES (?, ?, ?, ?)",
            (user_id, process_type, tokens, round(cost, 6)),
        )
    except Exception:
        log.exception("Failed to record token usage")


def complete(prompt: str, *, process_type: str, user_id=None, system: str = None,
             max_tokens: int = 4096) -> str | None:
    """Plain-text completion. Returns None when the LLM is unavailable/fails."""
    client = _get_client()
    if client is None:
        return None
    try:
        kwargs = {
            "model": config.LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        _record_usage(response.usage, process_type, user_id)
        return "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as exc:
        log.warning("LLM call failed (%s): %s", process_type, exc)
        return None


def complete_json(prompt: str, *, process_type: str, user_id=None, system: str = None,
                  schema: dict = None, max_tokens: int = 4096):
    """JSON completion. Uses structured outputs when a schema is given,
    otherwise parses the first JSON object/array in the response.
    Returns a dict/list, or None on failure."""
    client = _get_client()
    if client is None:
        return None
    try:
        kwargs = {
            "model": config.LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        response = client.messages.create(**kwargs)
        _record_usage(response.usage, process_type, user_id)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return _parse_json(text)
    except Exception as exc:
        log.warning("LLM JSON call failed (%s): %s", process_type, exc)
        return None


def _parse_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Strip markdown fences then grab the outermost object/array.
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


def obj_schema(properties: dict, required: list = None) -> dict:
    """Helper to build a strict JSON schema object."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties.keys()),
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
STR_ARR = {"type": "array", "items": {"type": "string"}}
