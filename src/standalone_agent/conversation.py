"""Read-only conversational MCP service; no demo-model fallback."""

import asyncio
import json
import os

import httpx

from .answers import authored_answer, parse_answer, visible_text
from .citations import strip_citations
from .cli import connect
from .prompts import SUMMARY_CONTEXT_NOTE, summary_prompt, system_prompt
from .review_options import openrouter_review_options
from .runtime import SECRET, WRITES, redact
from .verification import normative_issues, review_messages, validate_review


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def estimate(messages, tools):
    # UTF-8 bytes overestimate common tokenizers, with explicit message overhead.
    return len(encoded({"messages": messages, "tools": tools}).encode()) + 128 * len(messages) + 512


def collect_evidence(data, registry, field="", path=None, concept=None, unit=None, operator=None):
    if isinstance(data, dict):
        path = data.get("path", path)
        concept = data.get("id", concept)
        unit = data.get("unit", unit)
        operator = data.get("operator", operator)
        for key, value in data.items():
            if key == "facts" and isinstance(value, list):
                for fact in value:
                    if not isinstance(fact, dict) or fact.get("value_kind") != "normative_limit":
                        continue
                    source = {
                        "id": f"e{len(registry) + 1}",
                        "resource_path": path,
                        **{
                            name: fact.get(name)
                            for name in (
                                "concept_id",
                                "field",
                                "value",
                                "unit",
                                "operator",
                                "label",
                                "value_kind",
                            )
                        },
                    }
                    if (
                        type(source["value"]) in (int, float)
                        and source["operator"] in ("≤", "≥")
                        and all(
                            isinstance(source[name], str) and 0 < len(source[name]) <= 1024
                            for name in ("resource_path", "concept_id", "field", "unit", "label")
                        )
                        and redact(source) == source
                    ):
                        registry[source["id"]] = source
                continue
            if not SECRET.search(key):
                collect_evidence(
                    value,
                    registry,
                    f"{field}/{key}" if field else key,
                    path,
                    concept,
                    unit,
                    operator,
                )
    elif isinstance(data, list):
        for index, value in enumerate(data):
            collect_evidence(value, registry, f"{field}/{index}", path, concept, unit, operator)
    elif data is None or type(data) in (str, int, float, bool):
        if (
            len(encoded(data).encode()) <= 1024
            and redact(data) == data
            and all(
                v is None or isinstance(v, str) and len(v) <= 1024 and redact(v) == v
                for v in (path, concept, unit, operator, field)
            )
        ):
            source_id = f"e{len(registry) + 1}"
            registry[source_id] = {
                "id": source_id,
                "resource_path": path,
                "concept_id": concept,
                "field": field,
                "value": data,
                "unit": unit,
                "operator": operator,
            }


class ConversationError(ValueError):
    """Safe public failure; never include provider/transport exception text."""

    def __init__(self, message, code="agent_error"):
        super().__init__(message)
        self.code = code


class ConversationAgent:
    def __init__(
        self,
        *,
        provider=None,
        session_factory=None,
        client=None,
        max_calls=12,
        context_limit=None,
        output_reserve=None,
        tool_result_bytes=None,
        summary_bytes=None,
        reviewer=None,
    ):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
        self.provider = provider
        self.reviewer = reviewer
        self.session_factory = session_factory or (
            lambda: connect(
                os.environ.get("MCP_TRANSPORT", "stdio"),
                os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp"),
            )
        )
        self.context_limit = int(
            context_limit
            if context_limit is not None
            else os.environ.get("AGENT_CONTEXT_LIMIT", "48000")
        )
        self.output_reserve = int(
            output_reserve
            if output_reserve is not None
            else os.environ.get("AGENT_OUTPUT_RESERVE", "4096")
        )
        self.tool_result_bytes = int(
            tool_result_bytes
            if tool_result_bytes is not None
            else os.environ.get("AGENT_TOOL_RESULT_BYTES", "8192")
        )
        self.summary_bytes = int(
            summary_bytes
            if summary_bytes is not None
            else os.environ.get("AGENT_SUMMARY_BYTES", "2048")
        )
        if (
            min(self.output_reserve, self.tool_result_bytes, self.summary_bytes) < 64
            or self.output_reserve + self.tool_result_bytes + 1024 >= self.context_limit
        ):
            raise ConversationError("Некорректные лимиты контекста.", "configuration_error")
        self.client = client
        if type(max_calls) is not int or not 1 <= max_calls <= 12:
            raise ValueError("max_calls must be 1..12")
        self.max_calls = max_calls

    async def _complete(self, messages, tools, *, review=False):
        if estimate(messages, tools) + self.output_reserve > self.context_limit:
            raise ConversationError(
                "Превышен лимит контекста; запрос не отправлен.", "context_limit"
            )
        if self.provider is not None:
            return await self.provider(messages, tools)
        payload = {"model": self.model, "messages": messages, "max_tokens": self.output_reserve}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if review:
            payload.update(await openrouter_review_options(self.model, self.client))

        async def request(client):
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            if len(response.content) > 1048576:
                raise ValueError("Provider response exceeds limit")
            return response.json()["choices"][0]["message"]

        if self.client is not None:
            return await request(self.client)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await request(client)

    def _compact_tool_context(self, dialogue, tools):
        """Retire whole older native results; full events and evidence stay intact."""
        removed = 0
        tool_indices = [i for i, item in enumerate(dialogue) if item["role"] == "tool"]
        for index in tool_indices[:-1]:
            if estimate(dialogue, tools) + self.output_reserve <= self.context_limit:
                break
            payload = json.loads(dialogue[index]["content"])
            result = payload.get("result")
            if not isinstance(result, dict) or result.get("backend") != "iacpaas":
                continue
            dialogue[index] = {
                **dialogue[index],
                "content": encoded(
                    {
                        "omitted": True,
                        "reason": "working_context_budget",
                        "note": "Full result retained in event log. Omission is not absence.",
                    }
                ),
            }
            removed += 1
        # Retired placeholders and tool-call envelopes also consume context.
        # Drop only complete older exchanges, preserving the latest result and
        # matching every tool_call_id. Persistent events/evidence remain intact.
        index = 1
        while estimate(dialogue, tools) + self.output_reserve > self.context_limit and index < len(dialogue) - 1:
            calls = dialogue[index].get("tool_calls") or []
            count = len(calls)
            responses = dialogue[index + 1:index + 1 + count]
            ids = {call.get("id") for call in calls}
            if (count and len(responses) == count
                and all(item.get("role") == "tool" and json.loads(item["content"]).get("omitted") is True for item in responses)
                and {item.get("tool_call_id") for item in responses} == ids):
                del dialogue[index:index + 1 + count]
                removed += 1
            else:
                index += 1
        return removed

    async def _review_answer(self, question, rendered, provenance, history, emit, summary=None):
        await emit(
            "status",
            "Сверяю утверждения ответа: газ, компонент, единицы, границы и соответствие вопросу.",
        )
        answer, sources = rendered["answer"], rendered["sources"]
        blocks, request = review_messages(question, answer, sources, provenance, history, summary)
        packet = json.loads(request[-1]["content"])
        issues = normative_issues(blocks, sources)
        if issues:
            review = {
                "status": "rejected",
                "reason": "Единица или направление нормативной границы не совпадает с источником.",
                "issues": issues,
                "blocks": [],
            }
        else:
            try:
                if (
                    len(blocks) > 64
                    or estimate(request, []) + self.output_reserve > self.context_limit
                ):
                    raise ValueError("review_context_limit")
                async with asyncio.timeout(45):
                    response = await (
                        self.reviewer(request, [])
                        if self.reviewer
                        else self._complete(request, [], review=True)
                    )
                review = validate_review(
                    response,
                    blocks,
                    sources,
                    {source["id"]: source["document_id"] for source in packet["sources"]},
                )
            except Exception as exc:
                known = {
                    "review_context_limit",
                    "incomplete_review",
                    "invalid_block_review",
                    "unsupported_review_citation",
                    "factual_block_skipped",
                    "review_tools_forbidden",
                }
                diagnostic = (
                    str(exc)
                    if type(exc) is ValueError and str(exc) in known
                    else (
                        "review_json_invalid"
                        if isinstance(exc, json.JSONDecodeError)
                        else "review_timeout"
                        if isinstance(exc, TimeoutError)
                        else "review_unavailable"
                    )
                )
                review = {
                    "status": "unavailable",
                    "reason": "Не удалось получить полный корректный отчёт проверки.",
                    "diagnostic_code": diagnostic,
                    "blocks": [],
                }
        verification = dict(rendered["verification"], review=review, documents=packet["documents"])
        if review.get("status") == "rejected" and review.get("answers_question") is True:
            warnings = {
                str(warning).strip().rstrip(".").casefold()
                for result in provenance.values()
                for warning in result.get("warnings", [])
                if isinstance(warning, str)
            }
            warnings.update(
                {
                    "использованы демонстрационные данные mock",
                    "прочитана только часть данных в указанной области запроса",
                }
            )
            confirmed = [
                block
                for block in review["blocks"]
                if block["status"] == "supported"
                and strip_citations(block["text"]).strip().rstrip(".").casefold() not in warnings
            ]
            if confirmed:
                allowed = {
                    block["block_id"]
                    for block in review["blocks"]
                    if block["status"] in {"supported", "not_factual"}
                }
                answer = "\n\n".join(block["text"] for block in blocks if block["id"] in allowed)
                rendered["answer"] = (
                    answer
                    + "\n\nПодтверждена только эта часть ответа. Неподтверждённые утверждения исключены; полный отчёт сверки доступен ниже."
                )
                review["status"] = "partial"
        if review["status"] == "accepted":
            verification["checks"] = dict(
                verification.get("checks", {}), normative_bindings=True, semantic_blocks=True
            )
            verification.update(
                status="reviewed",
                reason="Утверждения и соответствие вопросу прошли автоматическую сверку с источниками. Отчёт доступен ниже.",
            )
            await emit("status", "Автоматическая сверка утверждений завершена.")
        elif review["status"] == "partial":
            verification.update(
                status="partial",
                reason="Показаны подтверждённые части ответа; остальные утверждения исключены.",
            )
            await emit("status", "Сверка завершена: показаны подтверждённые части ответа.")
        else:
            verification["validation"] = {
                "code": "semantic_rejection"
                if review["status"] == "rejected"
                else "review_failure",
                "candidate_text": answer,
            }
            verification.update(
                status="rejected" if review["status"] == "rejected" else "review_unavailable",
                reason="Часть утверждений ответа не подтверждена источниками."
                if review["status"] == "rejected"
                else review["reason"],
            )
            rendered["answer"] = (
                "Подготовленный ответ не прошёл сверку с источниками и вопросом. Этот вариант не опубликован; полученные источники сохранены ниже."
                if review["status"] == "rejected"
                else "Не удалось завершить проверку ответа по источникам. Непроверенный вывод не опубликован; источники сохранены ниже."
            )
            await emit(
                "status", "Проверка не подтвердила ответ; неподтверждённый вывод не опубликован."
            )
        rendered["verification"] = verification
        return rendered

    async def respond(
        self, message: str, messages: list[dict], summary: dict | None = None, *, on_event=None
    ) -> dict:
        try:
            if (
                not isinstance(message, str)
                or not message.strip()
                or not isinstance(messages, list)
                or any(
                    not isinstance(m, dict)
                    or m.get("role") not in {"user", "assistant"}
                    or not isinstance(m.get("content"), str)
                    or not isinstance(m.get("id"), str)
                    or not m["id"]
                    for m in messages
                )
                or len({m["id"] for m in messages}) != len(messages)
            ):
                raise ConversationError("Некорректный запрос или история.", "invalid_input")
            if summary is not None and (
                not isinstance(summary, dict)
                or not isinstance(summary.get("text"), str)
                or len(summary["text"].encode()) > self.summary_bytes
                or not isinstance(summary.get("covered_through_message_id"), str)
            ):
                raise ConversationError("Некорректная сводка истории.", "invalid_input")
            async with asyncio.timeout(180):
                return await self._respond(message, messages, summary, on_event)
        except ConversationError:
            raise
        except ExceptionGroup as exc:
            known, other = exc.split(ConversationError)
            if known is not None and other is None:
                failure = known
                while isinstance(failure, BaseExceptionGroup):
                    failure = failure.exceptions[0]
                raise failure from None
            raise ConversationError("Ошибка провайдера или MCP; ответ не подтверждён.") from None
        except Exception:
            raise ConversationError("Ошибка провайдера или MCP; ответ не подтверждён.") from None

    async def _context(self, message, messages, summary, tools, emit=None):
        # Failed turns remain visible in storage, but are not completed dialogue for the model.
        remaining = [item for item in messages if item.get("state", "complete") != "failed"]
        text, update, compactions = "", None, 0
        if summary:
            marker = summary["covered_through_message_id"]
            indices = [i for i, m in enumerate(remaining) if m["id"] == marker]
            if len(indices) != 1 or remaining[indices[0]]["role"] != "assistant":
                raise ConversationError(
                    "Невозможно сопоставить сводку с историей.", "context_limit"
                )
            remaining = remaining[indices[0] + 1 :]
            text = summary["text"]

        def dialogue():
            result = [{"role": "system", "content": system_prompt(self.max_calls)}]
            if text:
                result.append(
                    {
                        "role": "user",
                        "content": SUMMARY_CONTEXT_NOTE + text,
                    }
                )
            result.extend({"role": m["role"], "content": m["content"]} for m in remaining)
            result.append({"role": "user", "content": message})
            return result

        while (
            estimate(dialogue(), tools) + self.output_reserve + self.tool_result_bytes
            > self.context_limit
        ):
            endpoints = []
            for i, item in enumerate(remaining):
                if item.get("state", "complete") != "complete":
                    break
                if item["role"] == "assistant":
                    endpoints.append(i + 1)
            if not endpoints or compactions >= 12:
                raise ConversationError("Историю нельзя сжать в лимит контекста.", "context_limit")
            candidates = [n for n in endpoints if n <= max(2, len(remaining) // 2)] or endpoints[:1]
            request = []
            for count in reversed(candidates):
                request = [
                    {
                        "role": "system",
                        "content": summary_prompt(self.summary_bytes),
                    },
                    {
                        "role": "user",
                        "content": encoded(
                            {
                                "previous_summary": text,
                                "messages": [
                                    {"role": m["role"], "content": m["content"]}
                                    for m in remaining[:count]
                                ],
                            }
                        ),
                    },
                ]
                if estimate(request, []) + self.output_reserve <= self.context_limit:
                    break
            else:
                raise ConversationError(
                    "Один фрагмент истории превышает лимит контекста.", "context_limit"
                )
            if emit is not None:
                await emit("status", "Сжимаю рабочий контекст. Полная история диалога сохранится.")
            response = await self._complete(request, [])
            text = response.get("content")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode()) > self.summary_bytes
                or response.get("tool_calls")
            ):
                raise ConversationError("Модель не вернула ограниченную сводку.", "context_limit")
            update = {"text": text, "covered_through_message_id": remaining[count - 1]["id"]}
            remaining = remaining[count:]
            compactions += 1
        return dialogue(), update, compactions

    async def _respond(self, message, messages, summary, on_event=None):
        progress = []

        async def emit(kind, text):
            entry = {"kind": kind, "text": visible_text(text)[:1200]}
            if not entry["text"]:
                return
            progress.append(entry)
            if on_event is not None:
                await on_event(entry)

        if self.provider is None and not self.api_key.strip():
            raise ConversationError("OPENROUTER_API_KEY is required", "configuration_error")
        await emit(
            "status", "Разберу вопрос; если нужны сведения платформы, найду и сверю источники."
        )
        events, registry, provenance = [], {}, {}
        verification_details = {}
        answer, sources = "Недостаточно проверенных данных для ответа.", []
        status, reason = "insufficient_data", "Нет проверенных фактов MCP."
        async with self.session_factory() as session:
            discovered = (await session.list_tools()).tools
            allowed = {
                t.name: t
                for t in discovered
                if t.name not in WRITES and t.annotations and t.annotations.readOnlyHint is True
            }
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema,
                    },
                }
                for t in allowed.values()
            ]
            dialogue, summary_update, compactions = await self._context(
                message, messages, summary, tools, emit
            )
            for step in range(self.max_calls + 1):
                remaining_calls = self.max_calls - len(events)
                dialogue[0]["content"] = system_prompt(remaining_calls)
                available_tools = tools if remaining_calls else []
                compactions += self._compact_tool_context(dialogue, available_tools)
                response = await self._complete(dialogue, available_tools)
                calls = response.get("tool_calls") or []
                if calls and response.get("content"):
                    await emit("commentary", response["content"])
                if not calls:
                    await emit("status", "Подготавливаю ответ и проверяю ссылки на источники.")
                    try:
                        decision = parse_answer(
                            response.get("content"), has_evidence=bool(registry)
                        )
                    except (ValueError, TypeError):
                        raise ConversationError(
                            "Некорректный формат решения модели.", "model_output_error"
                        ) from None
                    if isinstance(decision, dict) and decision.get("type") == "answer":
                        try:
                            rendered = authored_answer(decision, registry, provenance)
                        except (ValueError, TypeError):
                            raise ConversationError(
                                "Некорректный формат ответа модели.", "model_output_error"
                            ) from None
                        if rendered["verification"]["status"] == "grounded":
                            rendered = await self._review_answer(
                                message,
                                rendered,
                                provenance,
                                messages,
                                emit,
                                summary_update or summary,
                            )
                        answer, sources = rendered["answer"], rendered["sources"]
                        status = rendered["verification"]["status"]
                        reason = rendered["verification"]["reason"]
                        verification_details = rendered["verification"]
                        break
                    selection = isinstance(decision, dict) and "source_ids" in decision
                    if selection:
                        ids = decision["source_ids"]
                        if (
                            set(decision) - {"source_ids", "clarification"}
                            or not isinstance(ids, list)
                            or len(ids) > 64
                            or any(not isinstance(item, str) for item in ids)
                            or type(decision.get("clarification", False)) is not bool
                            or decision.get("clarification") is True
                            and ids
                        ):
                            raise ConversationError(
                                "Некорректный формат выбора источников.", "model_output_error"
                            )
                        # Materialize facts only from this turn's server-owned evidence.
                        # The model supplies references, never authoritative field values.
                        decision = {
                            "clarification": decision.get("clarification", False),
                            "claims": [
                                {
                                    "source_id": source_id,
                                    **{
                                        key: value
                                        for key, value in registry.get(source_id, {}).items()
                                        if key != "id"
                                    },
                                }
                                for source_id in dict.fromkeys(ids)
                            ],
                        }
                    if (
                        not isinstance(decision, dict)
                        or not isinstance(decision.get("claims"), list)
                        or len(decision["claims"]) > 64
                        or any(
                            not isinstance(c, dict) or not isinstance(c.get("source_id"), str)
                            for c in decision["claims"]
                        )
                    ):
                        raise ConversationError(
                            "Некорректный формат решения модели.", "model_output_error"
                        )
                    if decision.get("clarification") is True and decision.get("claims") == []:
                        status, reason = "not_applicable", "Нужно уточнение запроса."
                        answer = "Уточните путь ресурса, объект и проверяемое поле (при необходимости — единицы и условие)?"
                    for claim in decision.get("claims", []):
                        source = registry.get(claim.get("source_id"))
                        if source is None or any(
                            k not in claim or type(claim[k]) is not type(v) or claim[k] != v
                            for k, v in source.items()
                            if k != "id"
                        ):
                            status, reason = (
                                "insufficient_data",
                                "Утверждение не совпало с доказательством MCP.",
                            )
                            sources = []
                            events.append(
                                {
                                    "name": "verify_sources",
                                    "status": "error",
                                    "result": {
                                        "code": "unknown_source"
                                        if source is None
                                        else "claim_mismatch",
                                        "missing_fields": [
                                            key
                                            for key in (source or {})
                                            if key != "id" and key not in claim
                                        ],
                                        "mismatched_fields": [
                                            key
                                            for key, value in (source or {}).items()
                                            if key != "id"
                                            and key in claim
                                            and (
                                                type(claim[key]) is not type(value)
                                                or claim[key] != value
                                            )
                                        ],
                                    },
                                }
                            )
                            if selection:
                                status = "error"
                                reason = (
                                    "Модель выбрала источник, который не получен в текущем запросе."
                                )
                                answer = "Ответ модели не прошёл проверку источников."
                            break
                        sources.append(source)
                    if sources:
                        status, reason = (
                            "verified",
                            "Поля и значения сверены с результатами MCP; вывод ограничен указанной областью.",
                        )
                        lines, warnings = [], []
                        for source in sources:
                            context = redact(provenance[source["id"]])
                            label = source.get("label") or {
                                "standard": "Нормативный документ",
                                "gas_name": "Газ",
                            }.get(source["field"], source["field"])
                            operator = {"≤": "не более", "≥": "не менее"}.get(
                                source["operator"], source["operator"] or ""
                            )
                            value = (
                                source["value"]
                                if isinstance(source["value"], str)
                                else encoded(source["value"])
                            )
                            unit = " " + source["unit"] if source["unit"] else ""
                            line = f"{label}: {operator + ' ' if operator else ''}{value}{unit}"
                            if source.get("value_kind") == "normative_limit":
                                line += " (нормативное ограничение, не результат измерения)"
                            lines.append(line)
                            warnings.extend(context.get("warnings", []))
                            if context.get("complete") is False:
                                warnings.append("Результат ограничен указанной областью запроса.")
                        native = all(
                            provenance[s["id"]].get("backend") == "iacpaas" for s in sources
                        )
                        header = (
                            "Данные IACPaaS, сверенные с источниками:"
                            if native
                            else "Проверенные поля MCP (MOCK — синтетические данные):"
                        )
                        answer = header + "\n" + "\n".join(lines + list(dict.fromkeys(warnings)))
                    break
                if len(events) + len(calls) > self.max_calls:
                    raise ConversationError("Исчерпан лимит вызовов MCP.", "tool_limit")
                prepared, ids = [], set()
                if not isinstance(calls, list):
                    raise ConversationError(
                        "Некорректный формат вызовов MCP.", "model_output_error"
                    )
                for call in calls:
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not call_id or call_id in ids:
                        raise ConversationError(
                            "Некорректные идентификаторы вызовов MCP.", "model_output_error"
                        )
                    ids.add(call_id)
                    name = call["function"]["name"]
                    arguments = json.loads(call["function"]["arguments"])
                    if (
                        not isinstance(arguments, dict)
                        or len(encoded(arguments).encode()) > self.tool_result_bytes
                    ):
                        raise ConversationError(
                            "Аргументы MCP не являются объектом или превышают лимит.",
                            "tool_result_limit",
                        )
                    if name not in allowed:
                        raise ConversationError(
                            "Запись или неизвестный инструмент MCP запрещены.", "permission_denied"
                        )
                    prepared.append((call_id, name, arguments))
                safe_calls = [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": name, "arguments": encoded(args)},
                    }
                    for cid, name, args in prepared
                ]
                dialogue.append({"role": "assistant", "content": None, "tool_calls": safe_calls})
                for call_id, name, arguments in prepared:
                    labels = {
                        "list_resources": "Смотрю доступные ресурсы",
                        "list_inforesources": "Смотрю доступные ресурсы",
                        "query_graph": "Ищу нужные объекты в ресурсе",
                        "get_gas_specification": "Читаю состав газа и нормативные ограничения",
                        "get_subgraph": "Читаю характеристики найденного объекта",
                        "get_resource": "Читаю описание ресурса",
                        "count_graph": "Проверяю количество объектов",
                    }
                    await emit("tool_start", labels.get(name, "Читаю данные через MCP"))
                    raw = await session.call_tool(name, arguments)
                    await emit(
                        "tool_end",
                        "Чтение завершилось ошибкой."
                        if raw.isError
                        else "Получен результат чтения; сверяю его с вопросом.",
                    )
                    result = raw.structuredContent
                    raw_bytes = len(encoded(result).encode())
                    envelope_bytes = len(
                        encoded({"result": redact(result), "evidence": []}).encode()
                    )
                    if max(raw_bytes, envelope_bytes) > self.tool_result_bytes:
                        if (
                            not raw.isError
                            and isinstance(result, dict)
                            and result.get("backend") == "iacpaas"
                            and result.get("ok") is True
                        ):
                            diagnostic = {
                                "ok": False,
                                "backend": "iacpaas",
                                "error": {
                                    "code": "tool_result_limit",
                                    "message": "Whole result omitted. Narrow limit/max_nodes/depth or query a specific field. This is not evidence of absence.",
                                    "recoverable": True,
                                },
                                "complete": False,
                            }
                            payload = encoded({"result": diagnostic, "evidence": []})
                            if len(payload.encode()) <= self.tool_result_bytes:
                                events.append(
                                    {
                                        "name": name,
                                        "arguments": redact(arguments),
                                        "result": diagnostic,
                                        "status": "error",
                                        "result_bytes": raw_bytes,
                                    }
                                )
                                dialogue.append(
                                    {"role": "tool", "tool_call_id": call_id, "content": payload}
                                )
                                continue
                        raise ConversationError(
                            "Результат MCP превышает лимит; уточните область запроса.",
                            "tool_result_limit",
                        )
                    failed = raw.isError or isinstance(result, dict) and result.get("ok") is False
                    event = {
                        "name": name,
                        "arguments": redact(arguments),
                        "result": redact(result),
                        "status": "error" if failed else "ok",
                    }
                    events.append(event)
                    if failed:
                        status, reason, answer = (
                            "error",
                            "Ошибка выполнения MCP.",
                            "Не удалось получить данные MCP.",
                        )
                        break
                    before = set(registry)
                    if (
                        isinstance(result, dict)
                        and result.get("ok") is True
                        and isinstance(result.get("data"), dict)
                    ):
                        collect_evidence(result["data"], registry)
                    new = [s for key, s in registry.items() if key not in before]
                    payload = encoded({"result": redact(result), "evidence": new})
                    while (
                        isinstance(result, dict)
                        and result.get("backend") == "iacpaas"
                        and new
                        and len(payload.encode()) > self.tool_result_bytes
                    ):
                        registry.pop(new.pop()["id"])
                        payload = encoded({"result": redact(result), "evidence": new})
                    if len(payload.encode()) > self.tool_result_bytes:
                        raise ConversationError(
                            "Доказательства MCP превышают лимит; сузьте запрос.",
                            "tool_result_limit",
                        )
                    provenance.update({s["id"]: result for s in new})
                    dialogue.append({"role": "tool", "tool_call_id": call_id, "content": payload})
                if status == "error":
                    break
            else:
                status, reason, answer = (
                    "error",
                    "Исчерпан лимит шагов.",
                    "Исчерпан лимит шагов MCP.",
                )
        # Compatibility formats must not bypass semantic/relevance review.
        if status == "verified" and sources:
            rendered = await self._review_answer(
                message,
                {
                    "answer": answer,
                    "sources": sources,
                    "verification": {"status": status, "reason": reason},
                },
                provenance,
                messages,
                emit,
                summary_update or summary,
            )
            verification_details = rendered["verification"]
            status = (
                "verified"
                if verification_details["status"] == "reviewed"
                else verification_details["status"]
            )
            reason, answer = verification_details["reason"], rendered["answer"]
        verification = {
            **verification_details,
            "status": status,
            "reason": reason,
            "progress": progress,
        }
        return {
            "answer": answer,
            "sources": sources,
            "verification": verification,
            "events": events,
            "summary_update": summary_update,
            "context": {
                "estimated_input_tokens": estimate(dialogue, tools),
                "limit": self.context_limit,
                "compactions": compactions,
            },
        }
