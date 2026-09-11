"""Провайдеры LLM за одним интерфейсом.

Зачем абстракция в MVP: клиент из транскрипта 3 прямым текстом сказал —
«если для анализа вы отправляете записи в зарубежное облако, проект сразу стоп».
Поэтому смена движка на российский контур (GigaChat / YandexGPT) должна быть
сменой одного адаптера, а не переписыванием пайплайна.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from typing import Protocol


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


def extract_json(text: str) -> dict:
    """Модели любят оборачивать JSON в ```json ... ```. Достаём объект надёжно."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # последний шанс: самый внешний {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise LLMError(f"Не удалось извлечь JSON из ответа модели:\n{text[:500]}")


class ClaudeCLIProvider:
    """Headless Claude Code CLI. Без API-ключа — работает на подписке разработчика.

    MCP отключён и инструменты запрещены намеренно: нам нужен чистый
    текст-в-текст вызов, иначе CLI тратит секунды на коннекты к серверам.
    """

    name = "claude-cli"

    def __init__(self, model: str = "claude-sonnet-5", timeout: int = 600) -> None:
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        cmd = [
            "claude", "-p", user,
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system,
            "--mcp-config", '{"mcpServers":{}}',
            "--strict-mcp-config",
            "--disallowedTools", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Task",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude CLI не ответил за {self.timeout}с") from e

        if proc.returncode != 0:
            raise LLMError(f"claude CLI вернул код {proc.returncode}: {proc.stderr[:400]}")

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"claude CLI вернул не-JSON ответ: {proc.stdout[:400]}"
            ) from e
        if envelope.get("is_error"):
            raise LLMError(f"claude CLI: {envelope.get('result', '')[:400]}")
        if "result" not in envelope:
            raise LLMError(f"claude CLI: в ответе нет поля result: {str(envelope)[:400]}")
        return envelope["result"]


class OpenAICompatProvider:
    """Любой OpenAI-совместимый endpoint: OpenAI, OpenRouter, GigaChat-прокси,
    локальный vLLM/Ollama. Меняется только base_url и модель."""

    name = "openai-compat"

    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except Exception as e:  # noqa: BLE001 — в MVP любую сетевую ошибку заворачиваем
            raise LLMError(f"{self.base_url}: {e}") from e
        return data["choices"][0]["message"]["content"]


def get_provider(spec: str) -> LLMProvider:
    """spec: 'claude', 'claude:model', 'openai', 'openai:model'"""
    kind, _, model = spec.partition(":")
    if kind == "claude":
        return ClaudeCLIProvider(model=model or "claude-sonnet-5")
    if kind in {"openai", "openai-compat"}:
        return OpenAICompatProvider(model=model or "gpt-4.1-mini")
    raise ValueError(f"Неизвестный провайдер: {spec}")
