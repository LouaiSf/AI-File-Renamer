"""OpenAI-compatible chat client (Ollama, Gemini, OpenRouter, Groq, LM Studio, OpenAI...)."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass

import httpx

from .config import Provider
from .content import Content


class LLMError(Exception):
    pass


class RateLimiter:
    """Spaces requests evenly so a free tier's requests-per-minute limit is never hit."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self.next_slot = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


def _error_message(r: httpx.Response) -> str:
    try:
        data = r.json()
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", data)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        msg = r.text
    return f"HTTP {r.status_code}: {(msg or '').strip()[:400]}"


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    header = r.headers.get("retry-after")
    if header and header.replace(".", "").isdigit():
        return min(float(header), 90)
    m = re.search(r'retry(?:Delay| in)["\s:]*([\d.]+)s', r.text, re.I)
    if m:
        return min(float(m.group(1)) + 1, 90)
    return min(2 ** attempt * 3, 60)


class Client:
    def __init__(self, provider: Provider, timeout: float = 120, temperature: float = 0.2, extra_body: dict | None = None):
        self.provider = provider
        self.temperature = temperature
        self.extra_body = extra_body or {}
        self.limiter = RateLimiter(provider.rpm)
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10), headers=headers)
        self._json_mode = True
        self._no_think = True  # Ollama: ask hybrid models to skip their (slow) reasoning

    def close(self) -> None:
        self.http.close()

    def _url(self, path: str) -> str:
        return f"{self.provider.base_url}/{path}"

    @property
    def _native(self) -> bool:
        return self.provider.kind == "ollama"

    def _request(self, messages: list[dict]) -> tuple[str, dict]:
        if not self._native:
            body = {"model": self.provider.model, "messages": messages, "temperature": self.temperature}
            if self._json_mode:
                body["response_format"] = {"type": "json_object"}
            return self._url("chat/completions"), {**body, **self.extra_body}
        # Ollama's own API: supports `think` and a context size, which its OpenAI endpoint does not.
        native = []
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                native.append({"role": m["role"], "content": content})
                continue
            text = "\n".join(p["text"] for p in content if p["type"] == "text")
            images = [p["image_url"]["url"].split(",", 1)[1] for p in content if p["type"] == "image_url"]
            native.append({"role": m["role"], "content": text, **({"images": images} if images else {})})
        body = {"model": self.provider.model, "messages": native, "stream": False, "keep_alive": "15m",
                "options": {"temperature": self.temperature, "num_ctx": 16384}}
        if self._json_mode:
            body["format"] = "json"
        if self._no_think:
            body["think"] = False
        base = self.provider.base_url.removesuffix("/v1")
        return f"{base}/api/chat", {**body, **self.extra_body}

    def _content(self, data: dict) -> str:
        if self._native:
            return data["message"].get("content") or ""
        return data["choices"][0]["message"].get("content") or ""

    def _connect_error(self, e: Exception) -> LLMError:
        if self.provider.kind == "ollama":
            return LLMError("Can't reach Ollama. Is it installed and running? (Settings > Local models)")
        return LLMError(f"Can't reach {self.provider.base_url}: {e}")

    def chat(self, messages: list[dict]) -> str:
        if not self.provider.model:
            raise LLMError(f"No model selected for {self.provider.label}. Pick one in Settings.")
        if self.provider.kind != "ollama" and not self.provider.api_key:
            raise LLMError(f"No API key set for {self.provider.label}. Add it in Settings.")
        for attempt in range(6):
            url, body = self._request(messages)
            self.limiter.wait()
            try:
                r = self.http.post(url, json=body)
            except httpx.TimeoutException:
                if attempt < 2:
                    continue
                raise LLMError("The model took too long to answer (timeout).")
            except httpx.TransportError as e:
                raise self._connect_error(e)
            if r.status_code == 429 or r.status_code >= 500:
                overloaded = r.status_code >= 500
                if attempt == 5 or (overloaded and attempt == 2):
                    hint = "  (The provider is overloaded; try again later or pick another model in Settings.)" if overloaded else ""
                    raise LLMError(_error_message(r) + hint)
                time.sleep(_retry_delay(r, attempt))
                continue
            if r.status_code == 400 and self._json_mode and "response_format" in r.text.lower():
                self._json_mode = False
                continue
            if r.status_code == 400 and self._native and self._no_think and "think" in r.text.lower():
                self._no_think = False  # model has no thinking switch
                continue
            if r.status_code == 404 and self._native and "not found" in r.text:
                raise LLMError(f"Model '{self.provider.model}' isn't downloaded yet. Get it in Settings > Local models.")
            if r.status_code >= 400:
                raise LLMError(_error_message(r))
            try:
                return self._content(r.json())
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError(f"Unexpected response: {r.text[:300]}")
        raise LLMError("Gave up after repeated rate-limit errors.")

    def list_models(self) -> list[str]:
        try:
            r = self.http.get(self._url("models") if not self._native else self.provider.base_url.removesuffix("/v1") + "/v1/models")
        except httpx.TransportError as e:
            raise self._connect_error(e)
        if r.status_code >= 400:
            raise LLMError(_error_message(r))
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        return sorted(i.removeprefix("models/") for i in ids if i)

    def test(self) -> str:
        start = time.monotonic()
        reply = self.chat([{"role": "user", "content": 'Reply with exactly this JSON: {"ok": true}'}])
        return f"OK - {self.provider.model} answered in {time.monotonic() - start:.1f}s ({reply.strip()[:40]})"


@dataclass
class Suggestion:
    title: str
    category: str = ""
    date: str | None = None
    date_relevant: bool = False
    confidence: float = 0.5


_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def parse_reply(raw: str) -> Suggestion:
    text = re.sub(r"(?is)<think>.*?</think>", "", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"No JSON in reply: {raw[:200]!r}")
    data = json.loads(text[start : end + 1])
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("Reply has no title")
    date = data.get("date")
    date = str(date).strip() if date else None
    if date and not _DATE_RE.match(date):
        date = None
    relevant = data.get("date_relevant")
    if isinstance(relevant, str):
        relevant = relevant.strip().lower() == "true"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return Suggestion(title=title, category=str(data.get("category") or "").strip(),
                      date=date, date_relevant=bool(relevant) and bool(date), confidence=confidence)


def build_messages(prompt: str, content: Content) -> list[dict]:
    hints = "\n".join(f"- {k}: {v}" for k, v in content.hints.items())
    body = f"Hints:\n{hints}\n\n"
    if content.text:
        body += f"Extracted content:\n\"\"\"\n{content.text}\n\"\"\""
    elif content.images:
        body += "The file content is in the attached image(s)."
    else:
        body += "No content could be extracted; use the hints."
    parts: list[dict] = [{"type": "text", "text": body}]
    for img in content.images:
        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}})
    return [{"role": "system", "content": prompt}, {"role": "user", "content": parts}]


def suggest(client: Client, prompt: str, content: Content) -> Suggestion:
    messages = build_messages(prompt, content)
    raw = client.chat(messages)
    try:
        return parse_reply(raw)
    except (ValueError, json.JSONDecodeError):
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "That was not valid. Reply with ONLY the JSON object described in the instructions."},
        ]
        try:
            return parse_reply(client.chat(messages))
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"Model did not return usable JSON: {e}")
