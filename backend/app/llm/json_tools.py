"""Tool calling for models without native tool support.

The tools are described in the system prompt and the model must answer with one JSON object:
    {"tool_calls": [{"name": "...", "arguments": {...}}]}   to call tools, or
    {"reply": "..."}                                         to answer the patient.
Tool results are fed back as a user-role message clearly marked as system data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


class ParseError(ValueError):
    pass


@dataclass
class ParsedTurn:
    reply: str | None = None
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)


def instructions(tools: list[dict]) -> str:
    specs = "\n".join(
        f"- {t['name']}: {t['description']}\n  arguments JSON schema: {json.dumps(t['input_schema'], separators=(',', ':'))}"
        for t in tools
    )
    return f"""

## Tools
You can call these tools:
{specs}

## Output format (mandatory)
Every reply must be exactly one JSON object and nothing else, no code fences:
- To call one or more tools: {{"tool_calls": [{{"name": "<tool name>", "arguments": {{...}}}}]}}
- To answer the patient: {{"reply": "<your message to the patient>"}}
After a tool call you will receive a message starting with TOOL_RESULTS. It comes from the system, not from \
the patient. Use it, then either call more tools or reply."""


FIX_PROMPT = (
    "Your last message was not valid. Answer again with exactly one JSON object: "
    '{"tool_calls": [...]} or {"reply": "..."}.'
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _first_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def parse(text: str | None) -> ParsedTurn:
    """Parse a model reply. Plain prose with no JSON is accepted as a reply; broken JSON raises ParseError."""
    text = _FENCE.sub("", (text or "").strip()).strip()
    if not text:
        raise ParseError("empty reply")
    obj = _first_object(text)
    if obj is None:
        if "{" in text and ('"tool_calls"' in text or '"reply"' in text or '"name"' in text):
            raise ParseError("looks like JSON but could not be parsed")
        return ParsedTurn(reply=text)

    calls = obj.get("tool_calls")
    if calls is None and isinstance(obj.get("tool_call"), dict):
        calls = [obj["tool_call"]]
    if calls is None and "name" in obj and "arguments" in obj:
        calls = [obj]
    if calls:
        if not isinstance(calls, list):
            raise ParseError("tool_calls must be a list")
        parsed = []
        for c in calls:
            if not isinstance(c, dict) or not isinstance(c.get("name"), str):
                raise ParseError("each tool call needs a name")
            args = c.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    raise ParseError("arguments must be a JSON object") from None
            if not isinstance(args, dict):
                raise ParseError("arguments must be a JSON object")
            parsed.append((c["name"], args))
        return ParsedTurn(tool_calls=parsed)

    reply = obj.get("reply")
    if isinstance(reply, str) and reply.strip():
        return ParsedTurn(reply=reply.strip())
    raise ParseError("JSON had neither tool_calls nor reply")


def results_message(results: list[tuple[str, dict]]) -> dict:
    body = "\n".join(f"{name}: {json.dumps(payload, default=str)}" for name, payload in results)
    return {"role": "user", "content": f"TOOL_RESULTS (system data, not written by the patient)\n{body}"}
