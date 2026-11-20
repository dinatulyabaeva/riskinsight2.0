"""Responses API transport adapter; portfolio execution stays in the MCP client.

The planner consumes the same normalized message for all providers. Raw Responses
items are retained only within this adapter instance to replay function-call
history (including opaque reasoning items) during bounded planning corrections.
They are never included in the user-facing report, MCP trace or logs.
"""
from copy import deepcopy
import json
from urllib.parse import urlparse

import httpx

from .errors import AgentError


def _json_object(raw):
    """Accept unambiguous finite JSON objects only, before any MCP execution."""
    def invalid_constant(_):
        raise ValueError()

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    if not isinstance(raw, str):
        raise ValueError()
    value = json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError()
    # Reject overflow such as 1e999, not just the non-standard NaN/Infinity tokens.
    json.dumps(value, allow_nan=False)
    return value


def _call_key(calls):
    return tuple((call["id"], call["function"]["name"], call["function"]["arguments"])
                 for call in calls)


class ResponsesProvider:
    """Non-streaming Responses client with no provider-side conversation storage."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport
        self._replay = {}

    def _headers(self):
        scheme = getattr(self.settings, "llm_auth_scheme", "bearer")
        if scheme not in {"bearer", "api-key"}:
            raise AgentError("LLM_AUTH_SCHEME должен быть bearer или api-key")
        project = getattr(self.settings, "llm_project", "").strip()
        if urlparse(self.settings.llm_base_url).hostname == "ai.api.cloud.yandex.net":
            model = urlparse(self.settings.llm_model)
            if model.scheme != "gpt" or not model.netloc or not model.path.strip("/"):
                raise AgentError("Для Yandex Responses задайте LLM_MODEL в формате gpt://ID_каталога/модель")
            if project and project != model.netloc:
                raise AgentError("LLM_PROJECT должен совпадать с ID каталога в LLM_MODEL")
            project = model.netloc
        if any(c in project for c in "\r\n"):
            raise AgentError("Некорректный LLM_PROJECT")
        headers = {"Authorization": f"{'Api-Key' if scheme == 'api-key' else 'Bearer'} {self.settings.llm_api_key}",
                   "Content-Type": "application/json"}
        if project:
            headers["OpenAI-Project"] = project
        return headers

    def _input(self, messages):
        converted = []
        pending = set()
        for message in messages:
            role = message["role"]
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError()
                output = message.get("content")
                if not isinstance(output, str):
                    raise ValueError()
                converted.append({"type": "function_call_output", "call_id": call_id, "output": output})
                pending.remove(call_id)
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                raise ValueError()
            calls = message.get("tool_calls") or []
            text = message.get("content") or ""
            if not isinstance(text, str) or not isinstance(calls, list):
                raise ValueError()
            if calls:
                if role != "assistant" or pending:
                    raise ValueError()
                for call in calls:
                    call_id = call["id"]
                    name = call["function"]["name"]
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        raise ValueError()
                    if not isinstance(name, str) or not name:
                        raise ValueError()
                    _json_object(call["function"]["arguments"])
                    pending.add(call_id)
                raw = self._replay.get(_call_key(calls))
                if raw is not None:
                    converted.extend(deepcopy(raw))
                else:
                    if text:
                        converted.append({"role": role, "content": text})
                    converted.extend({"type": "function_call", "call_id": call["id"],
                                      "name": call["function"]["name"],
                                      "arguments": call["function"]["arguments"]} for call in calls)
            else:
                converted.append({"role": role, "content": text})
        if pending:
            raise ValueError()
        return converted

    @staticmethod
    def _tools(tools):
        converted = []
        for tool in tools:
            if tool.get("type") != "function":
                raise ValueError()
            function = tool["function"]
            if not isinstance(function.get("name"), str) or not function["name"]:
                raise ValueError()
            if not isinstance(function.get("parameters"), dict):
                raise ValueError()
            converted.append({"type": "function", "name": function["name"],
                              "description": function.get("description", ""),
                              "parameters": deepcopy(function["parameters"]),
                              "strict": function.get("strict", False)})
        return converted

    @staticmethod
    def _usage(data):
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise ValueError()
        result = {}
        for destination, primary, alternate in (("prompt_tokens", "input_tokens", "prompt_tokens"),
                                                ("completion_tokens", "output_tokens", "completion_tokens")):
            value = usage.get(primary, usage.get(alternate, 0))
            if value is None:
                value = 0
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError()
            result[destination] = value
        return result

    def _response(self, data):
        if not isinstance(data, dict):
            raise ValueError()
        usage = self._usage(data)
        status = data.get("status")
        if data.get("error"):
            raise AgentError("Responses API сообщил об ошибке выполнения. Проверьте настройки и лимиты провайдера.")
        if status == "incomplete":
            details = data.get("incomplete_details") or {}
            if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                # Never execute a partly generated function call.
                return {"role": "assistant", "content": ""}, usage, "length"
            raise AgentError("Responses API не завершил ответ: ограничение содержимого или неполный результат.")
        if status != "completed":
            raise AgentError("Responses API не вернул завершённый ответ. Запрос не переключён в DEMO.")
        output = data.get("output")
        if not isinstance(output, list) or not output:
            raise ValueError()
        text, calls, call_ids = [], [], set()
        for item in output:
            if not isinstance(item, dict):
                raise ValueError()
            kind = item.get("type")
            if kind == "reasoning":
                # Kept for the next request, never interpreted or displayed.
                continue
            if kind == "function_call":
                if item.get("status") not in {None, "completed"}:
                    raise ValueError()
                call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
                if not isinstance(call_id, str) or not call_id or call_id in call_ids:
                    raise ValueError()
                if not isinstance(name, str) or not name:
                    raise ValueError()
                _json_object(arguments)
                call_ids.add(call_id)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": arguments}})
            elif kind == "message":
                if item.get("role", "assistant") != "assistant" or item.get("status") not in {None, "completed"}:
                    raise ValueError()
                content = item.get("content")
                if not isinstance(content, list):
                    raise ValueError()
                for block in content:
                    if not isinstance(block, dict):
                        raise ValueError()
                    if block.get("type") == "refusal":
                        raise AgentError("Модель отказалась обрабатывать запрос. Ответ с расчётами не сформирован.")
                    if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                        raise ValueError()
                    text.append(block["text"])
            else:
                # No hosted web search, code execution or remote MCP is enabled.
                raise ValueError()
        result = {"role": "assistant", "content": "\n".join(text)}
        if calls:
            result["tool_calls"] = calls
            self._replay[_call_key(calls)] = deepcopy(output)
            while len(self._replay) > 8:
                self._replay.pop(next(iter(self._replay)))
        elif not result["content"].strip():
            raise ValueError()
        return result, usage, "tool_calls" if calls else "stop"

    async def complete(self, messages, tools, choice):
        headers = self._headers()
        try:
            if choice not in {"auto", "none", "required"}:
                raise ValueError()
            body = {"model": self.settings.llm_model, "input": self._input(messages),
                    "max_output_tokens": self.settings.max_output_tokens,
                    "stream": False, "store": False, "tool_choice": choice,
                    "include": ["reasoning.encrypted_content"]}
            if choice != "none":
                body["tools"] = self._tools(tools)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise AgentError("Некорректная история или схема инструментов для Responses API") from None
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout, transport=self.transport,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.post(self.settings.llm_base_url + "/responses", headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException:
            raise AgentError("Responses API: LLM не ответила в пределах тайм-аута. DEMO не включён.", status_code=504) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retry = exc.response.headers.get("retry-after", "")
            retry = min(int(retry), 300) if retry.isdigit() else None
            if status == 429:
                message = "Responses API вернул HTTP 429: квота или временное ограничение провайдера. Повторите позже."
            elif status in {401, 403}:
                message = f"Responses API вернул HTTP {status}. Проверьте API-ключ, схему авторизации, каталог и роли сервисного аккаунта."
            else:
                message = f"Responses API вернул HTTP {status}. Проверьте модель, лимиты и поддержку Responses API."
            raise AgentError(message, status_code=status, retry_after=retry) from None
        except (httpx.HTTPError, ValueError):
            raise AgentError("Ошибка связи или JSON-ответа Responses API. Проверьте серверную конфигурацию.") from None
        try:
            return self._response(data)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
            raise AgentError("Responses API вернул некорректный ответ или незавершённые аргументы функции") from None
