"""Native bounded MCP runtime. DemoModel is a scenario parser, NOT an LLM."""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRITES = {"update_concept", "import_inforesource"}
SECRET = re.compile(r"token|secret|password|authorization|key|credential", re.I)


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class Decision:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    clarification: str = ""


def find_paths(value):
    paths = []
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            paths.append(value["path"])
        for child in value.values():
            paths.extend(find_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(find_paths(child))
    return list(dict.fromkeys(paths))


class DemoModel:
    """Limited Russian/English deterministic scenarios; not general language reasoning."""

    async def decide(self, prompt, tools, history):
        lower = prompt.lower()
        match = re.search(r"/[\w/-]+", prompt)
        path = match.group() if match else None
        graph = any(
            word in lower for word in ("подграф", "subgraph", "count", "посчитай", "количеств")
        )
        write = any(word in lower for word in ("измен", "запиш", "update", "write"))
        if not history:
            if (graph or write) and not path:
                return Decision(clarification="Уточните путь ресурса, например /demo/university.")
            return Decision(calls=[ToolCall("list_inforesources", {"filter": ""})])
        last = history[-1]
        if len(history) == 1 or (last["is_error"] and last["result"].get("code") == "not_found"):
            paths = find_paths(history[0]["result"])
            if not paths:
                return Decision(text="Ресурсы не найдены.")
            # A failed explicit path is recovered ONLY from the discovered catalog.
            selected = paths[0] if last["is_error"] else (path or paths[0])
            name = (
                "get_subgraph"
                if "подграф" in lower or "subgraph" in lower
                else "count_graph"
                if graph
                else "update_concept"
                if write
                else "get_inforesource"
            )
            args = {"path": selected}
            if write:
                args.update(concept_id="root", comment="Демонстрационная запись mock")
            if len(history) > 2:
                return Decision(
                    text="Восстановление не удалось: "
                    + json.dumps(last["result"], ensure_ascii=False)
                )
            return Decision(calls=[ToolCall(name, args)])
        return Decision(text=json.dumps(last["result"], ensure_ascii=False))


def decode_result(result):
    if result.structuredContent and not result.isError:
        return result.structuredContent
    text = "\n".join(item.text for item in result.content if item.type == "text")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except ValueError:
        # FastMCP can prefix its JSON ToolError with an execution message.
        start = text.find("{")
        if start >= 0:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text[start:])
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return {"message": text}


def redact(value):
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if SECRET.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value[:50]]
    if isinstance(value, str):
        text = value
        for key, secret in os.environ.items():
            if SECRET.search(key) and len(secret) >= 4:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)bearer\s+\S+|sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
        return text[:2048]
    return value


class Trace:
    """At most max_bytes per run; never follow a trace symlink; mode 0600."""

    def __init__(self, path, max_bytes=262144, max_record_bytes=16384):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_record_bytes = max_record_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.written = 0

    def record(self, **record):
        safe = redact(record)
        line = json.dumps(safe, ensure_ascii=False)
        if len(line.encode()) + 1 > self.max_record_bytes:
            safe["args"] = {"truncated": True}
            safe["result"] = {"truncated": True}
            line = json.dumps(safe, ensure_ascii=False)
        data = (line + "\n").encode()
        if self.written + len(data) > self.max_bytes:
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        self.written += len(data)


class Agent:
    def __init__(self, session, model, *, max_calls=12, trace=None, confirm=None, clarify=None):
        if not 1 <= max_calls <= 12:
            raise ValueError("max_calls must be 1..12")
        self.session, self.model = session, model
        self.max_calls, self.trace = max_calls, trace
        self.confirm, self.clarify = confirm, clarify

    async def run(self, prompt):
        tools = (await self.session.list_tools()).tools
        by_name = {tool.name: tool for tool in tools}
        history: list[dict[str, Any]] = []
        budget = 0
        attempted_writes = set()
        banner = "MOCK backend (synthetic fixtures, volatile writes). "
        # Clarifications and model turns are bounded independently of tool calls.
        for _ in range(self.max_calls + 3):
            decision = await self.model.decide(prompt, tools, history)
            if decision.clarification:
                if not self.clarify:
                    return banner + decision.clarification
                answer = self.clarify(decision.clarification)
                if not answer:
                    return banner + decision.clarification
                prompt += "\n" + answer
                continue
            if not decision.calls:
                return banner + decision.text
            for call in decision.calls:
                if budget >= self.max_calls:
                    return banner + "tool budget exhausted"
                budget += 1
                start = time.monotonic()
                tool = by_name.get(call.name)
                annotation = tool.annotations if tool else None
                needs_confirmation = call.name in WRITES or not (
                    annotation and annotation.readOnlyHint is True
                )
                signature = (call.name, json.dumps(call.arguments, sort_keys=True))
                if tool is None:
                    result, status = (
                        {"code": "unknown_tool", "message": "Tool not discovered"},
                        "error",
                    )
                elif needs_confirmation and signature in attempted_writes:
                    result, status = (
                        {"code": "permission_denied", "message": "Automatic write retry denied"},
                        "denied",
                    )
                elif needs_confirmation and (
                    not self.confirm or not self.confirm(call.name, call.arguments)
                ):
                    result, status = (
                        {
                            "code": "permission_denied",
                            "message": "Human CLI confirmation required; write denied",
                        },
                        "denied",
                    )
                else:
                    if needs_confirmation:
                        attempted_writes.add(signature)
                    try:
                        raw = await self.session.call_tool(call.name, call.arguments)
                        result, status = decode_result(raw), "error" if raw.isError else "ok"
                    except Exception as exc:
                        result, status = (
                            {"code": "transport_error", "message": str(exc), "retryable": False},
                            "error",
                        )
                event = {
                    "name": call.name,
                    "arguments": call.arguments,
                    "result": result,
                    "is_error": status != "ok",
                    "id": call.id,
                }
                history.append(event)
                if self.trace:
                    self.trace.record(
                        name=call.name,
                        args=call.arguments,
                        result=result,
                        status=status,
                        duration_ms=round((time.monotonic() - start) * 1000, 3),
                    )
            if budget >= self.max_calls:
                return banner + "tool budget exhausted"
        return banner + "model turn budget exhausted"
