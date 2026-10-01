"""iMessage AI assistant backed by an LLM API.

The model can call two tools: ``web_search`` (DuckDuckGo, no API key) and ``fetch_url``
(read a page). Each conversation keeps its own short history; text "reset" to clear it.

Configure in .env. Two API styles are supported:

    # Anthropic Messages API (e.g. Z.ai GLM)
    AI_API=anthropic-messages
    AI_BASE_URL=https://api.z.ai/api/anthropic
    AI_MODEL=glm-5.3-flash
    AI_API_KEY=...

    # OpenAI chat completions (e.g. a local Ollama server)
    AI_API=openai-completions
    AI_BASE_URL=http://localhost:11434/v1
    AI_MODEL=qwen3.8-unc:256k
    AI_API_KEY=ollama

then run:

    python ai.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

import spectrum
from spectrum.providers import IMessage

load_dotenv()
log = logging.getLogger("ai")

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    api: str = os.environ.get("AI_API", "anthropic-messages")
    base_url: str = os.environ.get("AI_BASE_URL", "https://api.z.ai/api/anthropic")
    model: str = os.environ.get("AI_MODEL", "glm-5.3-flash")
    api_key: str = os.environ.get("AI_API_KEY", "")
    max_tokens: int = int(os.environ.get("AI_MAX_TOKENS", "8192"))
    history_turns: int = int(os.environ.get("AI_HISTORY_TURNS", "20"))
    max_tool_rounds: int = int(os.environ.get("AI_MAX_TOOL_ROUNDS", "5"))
    timeout: float = float(os.environ.get("AI_TIMEOUT", "300"))


SETTINGS = Settings()

SYSTEM_PROMPT = """You are a helpful assistant that people reach by text message (iMessage or SMS).
- Reply in the language the user writes in.
- Keep replies short and easy to read on a phone: a few sentences or a short list. No tables or headings.
- Use web_search for anything current or factual you are not sure about (news, weather, prices, schedules,
  opening hours, exchange rates). Use fetch_url when search snippets are not enough.
- When you used the web, end with the source links (plain URLs, at most 2).
- Answer what the user actually asked. Do not invent context, puns or facts. If you cannot do something
  (for example take a screenshot), say so plainly and offer what you can do instead.
- Current local time: {now}."""


# --------------------------------------------------------------------------- tools


async def web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo HTML results as a compact text list."""
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
    ) as http:
        response = await http.post("https://html.duckduckgo.com/html/", data={"q": query})
        response.raise_for_status()
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.S,
    )
    results = []
    for href, title, snippet in pattern.findall(response.text)[: max(1, min(max_results, 10))]:
        results.append(f"- {_clean(title)}\n  {_resolve_ddg_link(href)}\n  {_clean(snippet)}")
    return "\n".join(results) or "No results."


async def fetch_url(url: str, max_chars: int = 6000) -> str:
    """Readable text of a web page (scripts, styles and tags removed)."""
    if not url.startswith(("http://", "https://")):
        return "Only http(s) URLs can be fetched."
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
    ) as http:
        response = await http.get(url)
    if response.status_code >= 400:
        return f"HTTP {response.status_code}"
    text = response.text
    text = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", text)
    text = _clean(text)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _resolve_ddg_link(href: str) -> str:
    """DuckDuckGo sometimes wraps results as //duckduckgo.com/l/?uddg=<target>."""
    if "duckduckgo.com/l/" in href:
        target = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query).get("uddg")
        if target:
            return target[0]
    return href


ToolFn = Callable[..., Awaitable[str]]
TOOLS: dict[str, tuple[ToolFn, dict[str, Any]]] = {
    "web_search": (
        web_search,
        {
            "description": "Search the web. Returns titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "1-10, default 5"},
                },
                "required": ["query"],
            },
        },
    ),
    "fetch_url": (
        fetch_url,
        {
            "description": "Fetch a web page and return its readable text.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
                "required": ["url"],
            },
        },
    ),
}
TOOL_SPECS = [{"type": "function", "function": {"name": name, **spec}} for name, (_, spec) in TOOLS.items()]


async def run_tool(name: str, arguments: str | dict[str, Any]) -> str:
    entry = TOOLS.get(name)
    if entry is None:
        return f"Unknown tool {name}."
    log.info("tool %s %s", name, arguments)
    try:
        kwargs: dict[str, Any] = json.loads(arguments or "{}") if isinstance(arguments, str) else arguments
        return await entry[0](**kwargs)
    except Exception as exc:  # tool failures are reported back to the model, not raised
        log.warning("tool %s failed: %s", name, exc)
        return f"Tool error: {exc}"


# --------------------------------------------------------------------------- LLM


def system_prompt() -> str:
    return SYSTEM_PROMPT.format(now=f"{datetime.now().astimezone():%Y-%m-%d %H:%M %Z}")


class OpenAIChat:
    """OpenAI-style ``/chat/completions`` with a tool-calling loop (Ollama, vLLM, OpenAI ...)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.timeout,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _complete(self, messages: list[dict[str, Any]], *, tools: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self._settings.model, "messages": messages}
        if tools:
            body["tools"] = TOOL_SPECS
        response = await self._http.post("/chat/completions", json=body)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]

    async def answer(self, history: list[dict[str, Any]]) -> str:
        messages = [{"role": "system", "content": system_prompt()}, *history]
        for _ in range(self._settings.max_tool_rounds):
            reply = await self._complete(messages)
            calls = reply.get("tool_calls") or []
            if not calls:
                return _strip_thinking(reply.get("content") or "")
            messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for call in calls:
                result = await run_tool(call["function"]["name"], call["function"].get("arguments") or "{}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        messages.append({"role": "user", "content": "Answer now with what you have."})
        return _strip_thinking((await self._complete(messages, tools=False)).get("content") or "")


class AnthropicMessages:
    """Anthropic-style ``/v1/messages`` with a tool-use loop (Anthropic, Z.ai GLM ...)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.timeout,
            headers={
                "x-api-key": settings.api_key,
                "Authorization": f"Bearer {settings.api_key}",
                "anthropic-version": "2023-06-01",
            },
        )
        self._tools = [
            {"name": name, "description": spec["description"], "input_schema": spec["parameters"]}
            for name, (_, spec) in TOOLS.items()
        ]

    async def close(self) -> None:
        await self._http.aclose()

    async def _complete(self, messages: list[dict[str, Any]], *, tools: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": self._settings.max_tokens,
            "system": system_prompt(),
            "messages": messages,
        }
        if tools:
            body["tools"] = self._tools
        response = await self._http.post("/v1/messages", json=body)
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.text[:300]}", request=response.request, response=response
            )
        return response.json()

    @staticmethod
    def _text(reply: dict[str, Any]) -> str:
        blocks = reply.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    async def answer(self, history: list[dict[str, Any]]) -> str:
        messages = list(history)
        for _ in range(self._settings.max_tool_rounds):
            reply = await self._complete(messages)
            uses = [b for b in reply.get("content", []) if b.get("type") == "tool_use"]
            if not uses:
                return self._text(reply)
            # keep the whole assistant turn (thinking blocks included) so the model can continue
            messages.append({"role": "assistant", "content": reply["content"]})
            results = []
            for use in uses:
                output = await run_tool(use["name"], use.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": output})
            messages.append({"role": "user", "content": results})
        messages.append({"role": "user", "content": "Answer now with what you have."})
        return self._text(await self._complete(messages, tools=False))


def make_llm(settings: Settings) -> OpenAIChat | AnthropicMessages:
    if settings.api == "openai-completions":
        return OpenAIChat(settings)
    if settings.api == "anthropic-messages":
        if not settings.api_key:
            raise SystemExit("AI_API_KEY is not set: put your API key in .env")
        return AnthropicMessages(settings)
    raise SystemExit(f"unknown AI_API {settings.api!r}: use anthropic-messages or openai-completions")


def _strip_thinking(text: str) -> str:
    return re.sub(r"(?s)<think>.*?</think>", "", text).strip()


# --------------------------------------------------------------------------- conversations


@dataclass
class Conversation:
    history: deque[dict[str, Any]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Conversations:
    def __init__(self, turns: int) -> None:
        self._turns = turns
        self._by_space: dict[str, Conversation] = {}

    def get(self, space_id: str) -> Conversation:
        if space_id not in self._by_space:
            self._by_space[space_id] = Conversation(deque(maxlen=self._turns * 2))
        return self._by_space[space_id]

    def reset(self, space_id: str) -> None:
        self._by_space.pop(space_id, None)


# --------------------------------------------------------------------------- bot

client = spectrum.Client(providers=[IMessage()])
llm = make_llm(SETTINGS)
conversations = Conversations(SETTINGS.history_turns)


@client.event
async def on_ready():
    print(f"ready — model {SETTINGS.model} at {SETTINGS.base_url}, lines {client.imessage.phones}")


@client.event
async def on_message(message: spectrum.Message):
    if message.type is not spectrum.ContentType.TEXT or not message.text:
        return
    text = message.text.strip()
    print(f"<- {message.sender} ({message.sender.service if message.sender else '?'}): {text}")
    await message.read()

    if text.lower() in {"reset", "/reset"}:
        conversations.reset(message.space.id)
        await message.space.send("Conversation cleared 🧹")
        return

    conversation = conversations.get(message.space.id)
    async with conversation.lock:  # one answer at a time per conversation
        conversation.history.append({"role": "user", "content": text})
        try:
            async with message.space.typing():
                answer = await llm.answer(list(conversation.history))
        except httpx.HTTPError as exc:
            log.error("LLM request failed: %r", exc)
            conversation.history.pop()
            await message.space.send("Sorry, I can't reach the AI server right now. Try again in a moment.")
            return
        answer = answer or "(no answer)"
        conversation.history.append({"role": "assistant", "content": answer})
        print(f"-> {answer[:120]}")
        await message.space.send(spectrum.markdown(answer))


@client.event
async def on_close():
    await llm.close()


@client.event
async def on_error(event: str, exc: Exception, *args):
    print(f"error in on_{event}: {exc!r}")


if __name__ == "__main__":
    client.run()
