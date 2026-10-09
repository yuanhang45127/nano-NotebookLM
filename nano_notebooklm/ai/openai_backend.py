"""OpenAI-compatible backend (supports codex proxy and standard OpenAI)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import openai

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend, TruncationSignal
from nano_notebooklm.types import LLMResponse

# 2026-05-13: raised 4 → 24. This is a module-wide thread pool used by
# every codex sync call: complete_codex_sync, complete_chat_sync,
# AND complete_stream's producer thread. The 4-worker cap was silently
# bottlenecking notes generation — even with notes_full_course's
# DEFAULT_CONCURRENCY=8 and ChatRequest.concurrency.le=16, only 4 codex
# streams could actually run in parallel; the 5th-Nth files queued on
# this pool while their asyncio.Semaphore slots sat idle. 24 leaves
# room for: 16 concurrent per-file notes + 1 review stream + a couple
# concurrent chats and an agent loop without queue-starving each other.
# Tunable via env in case operators want a lower ceiling.
_executor = ThreadPoolExecutor(
    max_workers=int(os.getenv("OPENAI_EXECUTOR_WORKERS", "24")),
    thread_name_prefix="nlm-openai",
)

# fix-all v4 #B5: same cap discipline as agent_loop._CANCEL_WATCHER_LIMIT.
# Stream endpoints (notes / report) reach this code path; without a cap
# every concurrent stream span an unbounded watcher thread.
_CANCEL_WATCHER_LIMIT = threading.BoundedSemaphore(
    value=int(os.getenv("OPENAI_CANCEL_WATCHER_LIMIT", "64")),
)

# Per-request HTTP timeout for the underlying httpx client. Without this the
# sync client blocks indefinitely on a stalled provider; combined with the
# `asyncio.wait_for` wrapper in qa_skill (translate path), an unset timeout
# means the executor thread holds the connection even after wait_for cancels
# the awaiting coroutine. Tunable via env so the long generations (notes /
# report) can override.
#
# 2026-05-12: default raised 120 → 600. This is the SSE *chunk-to-chunk*
# read timeout, not the wall-clock cap. Provider reasoning phases
# regularly produces 2-3min of silence between deltas on dense per-file
# note generations (NOTES_PER_FILE_MAX_TOKENS=12288 + concurrent fan-out),
# and the proxy throttles concurrent SSE streams further. A 120s ceiling
# was causing all N concurrent per-file calls to ReadTimeout simultaneously
# → every file empty → all_files_failed surfaced to the user.
_DEFAULT_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT_SECONDS", "600"))

# Reasoning effort for codex responses API (GPT-5+ family). Lower effort
# = shorter thinking phase before the first SSE delta, at the cost of
# less elaborate planning. Default "medium" balances first-token latency
# (~5-15s vs ~30-90s for "high") with the quality needed for structured
# LaTeX notes. Set to "low" if you don't mind shorter sections, or "high"
# for the original proxy default. Empty / "auto" / "default" disables the
# field entirely so the proxy uses its own default.
_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "medium").strip().lower()
_REASONING_EFFORT_VALID = _REASONING_EFFORT in {"low", "medium", "high"}

# ── 2026-10 protocol modernization ─────────────────────────────────────
# Newer OpenAI-compatible providers diverge from the classic schema in
# three ways this backend must tolerate:
#   1. Reasoning models (DeepSeek V4, LongCat, GLM-5.x thinking, …) put
#      the answer in non-standard `reasoning_content` and leave `content`
#      empty — with no way to disable thinking via the gateway.
#   2. Some models (MiniMax M2) inline <think>…</think> blocks in content.
#   3. Newer endpoints reject `max_tokens` and want `max_completion_tokens`.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_think(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    # an unclosed <think> (truncated stream) — drop from the marker to EOF
    i = text.rfind("<think>")
    if i != -1 and "</think>" not in text[i:]:
        text = text[:i]
    return text


def _extract_reasoning(obj, *names):
    """Pull non-standard reasoning fields off SDK models / raw dicts."""
    for n in names:
        v = getattr(obj, n, None)
        if v:
            return v
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for n in names:
            v = extra.get(n)
            if v:
                return v
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if v:
                return v
    return None


class _ThinkStripper:
    """Streaming-safe <think> block stripper.

    Buffers output while inside an unclosed <think> block (keeping only a
    small tail so the search index stays valid across the trim); swallows
    the whitespace right after </think> to match the non-streaming regex;
    holds back a short tail when a partial "<think" tag may be arriving."""

    TAIL = 8

    def __init__(self):
        self.buf = ""
        self.in_think = False
        self.skip_ws = False

    def feed(self, delta: str) -> str:
        self.buf += delta
        return self._drain(final=False)

    def flush(self) -> str:
        if self.in_think:
            return ""  # unclosed think block at EOF → drop entirely
        out = self.buf.lstrip() if self.skip_ws else self.buf
        self.buf = ""
        self.skip_ws = False
        return out

    def _drain(self, final: bool) -> str:
        out = ""
        while True:
            if self.in_think:
                end = self.buf.find("</think>")
                if end == -1:
                    if final:
                        self.buf = ""  # never closed → nothing to emit
                    else:
                        self.buf = self.buf[-self.TAIL:]
                    return out
                self.buf = self.buf[end + len("</think>"):]
                self.in_think = False
                self.skip_ws = True
                continue
            if self.skip_ws:
                stripped = self.buf.lstrip()
                if not stripped and not final:
                    self.buf = ""  # pure whitespace so far — keep swallowing
                    return out
                self.buf = stripped
                self.skip_ws = False
                continue
            start = self.buf.find("<think>")
            if start == -1:
                if final:
                    out += self.buf
                    self.buf = ""
                    return out
                keep = 0
                tail = self.buf[-self.TAIL:]
                i = tail.rfind("<")
                if i != -1:
                    keep = len(tail) - i
                if keep:
                    out += self.buf[: len(self.buf) - keep]
                    self.buf = self.buf[len(self.buf) - keep:]
                else:
                    out += self.buf
                    self.buf = ""
                return out
            out += self.buf[:start]
            open_end = self.buf.find(">", start)
            if open_end == -1:
                if final:
                    self.buf = ""
                    return out
                self.buf = self.buf[start:]  # wait for the tag to close
                return out
            self.buf = self.buf[open_end + 1:]
            self.in_think = True
            self.skip_ws = True


def _chat_create_with_token_fallback(client, kwargs):
    """chat.completions.create with max_tokens → max_completion_tokens
    fallback for newer OpenAI-compatible endpoints that reject max_tokens."""
    try:
        return client.chat.completions.create(**kwargs)
    except openai.BadRequestError as e:
        msg = str(e)
        if "max_tokens" in kwargs and ("max_completion_tokens" in msg or "max_tokens is not supported" in msg):
            retry = dict(kwargs)
            retry["max_completion_tokens"] = retry.pop("max_tokens")
            return client.chat.completions.create(**retry)
        raise


class OpenAIBackend(LLMBackend):
    name = "openai"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        http_timeout: float | None = None,
    ):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or config.OPENAI_MODEL
        self._is_codex = "codex" in self.base_url.lower()
        # 2026-05-17: DeepSeek V4 默认走 thinking mode → 单次 5-15s
        # 通用 chat / rewrite / summary 类不需要 reasoning，关掉省时间。
        # 真要 thinking 可以在调用端 task_type 路由到别的 backend。
        self._is_deepseek = "deepseek" in self.base_url.lower()
        # Use sync client for codex compatibility. Configure httpx timeout so a
        # stalled upstream actually aborts — `asyncio.wait_for` alone can't
        # cancel a sync call running in a thread executor (the executor thread
        # keeps running until httpx's own timeout fires, occupying a worker
        # slot). The `http_timeout` kwarg lets callers (specifically the
        # `/api/providers/{id}/test` endpoint) drop the ceiling so a misconfig
        # row can't pin 24 executor threads for the full 600s default.
        effective_timeout = http_timeout if http_timeout is not None else _DEFAULT_HTTP_TIMEOUT
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(effective_timeout, connect=10.0),
        )

    def _thinking_extra(self) -> dict | None:
        """Provider-specific body fields that disable thinking mode.

        Reasoning models burn latency (and, with small max_tokens, the
        entire budget) on hidden thinking even for trivial rewrites. The
        toggles below cover the families seen behind OpenAI-compatible
        gateways; unknown models rely on the reasoning_content fallback
        instead of a disable flag."""
        name = (self.model or "").lower()
        if self._is_deepseek or "deepseek" in name:
            return {"thinking": {"type": "disabled"}}
        if "glm" in name or "zhipu" in name:
            return {"thinking": {"type": "disabled"}}
        if "qwen" in name or "qwq" in name:
            return {"enable_thinking": False}
        return None

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        start = time.time()
        loop = asyncio.get_event_loop()
        if self._is_codex:
            return await loop.run_in_executor(
                _executor, self._complete_codex_sync, prompt, system, temperature, max_tokens, start
            )
        return await loop.run_in_executor(
            _executor, self._complete_chat_sync, prompt, system, temperature, max_tokens, start
        )

    def _complete_codex_sync(
        self, prompt: str, system: str, temperature: float, max_tokens: int, start: float,
    ) -> LLMResponse:
        """Call codex proxy using responses API (sync, streaming)."""
        input_msgs = []
        if system:
            input_msgs.append({"role": "system", "content": system})
        input_msgs.append({"role": "user", "content": prompt})

        # fix-all v3 #M7: pass `max_output_tokens` so the per-skill caps
        # (notes 8K, report 8K, qa 4K) actually clamp the codex side.
        # Some proxy variants don't accept the field — fall back if so.
        # 2026-05-12: also pass `reasoning.effort` to compress GPT-5+
        # thinking phase.
        # 2026-05-13 (review-swarm fix-now CRITICAL #4): two-stage
        # TypeError fallback — first drop `reasoning` only (preserves
        # the per-skill `max_output_tokens` clamp on proxies that
        # accept the cap but not the reasoning field), then if that
        # still TypeErrors drop `max_output_tokens` too. The previous
        # collapsed-fallback dropped both at once, breaking the v3 #M7
        # invariant on proxies that supported max_output_tokens.
        kwargs = dict(
            model=self.model, input=input_msgs, temperature=temperature,
            max_output_tokens=max_tokens, stream=True,
        )
        if _REASONING_EFFORT_VALID:
            kwargs["reasoning"] = {"effort": _REASONING_EFFORT}
        try:
            stream = self.client.responses.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning", None)
            try:
                stream = self.client.responses.create(**kwargs)
            except TypeError:
                kwargs.pop("max_output_tokens", None)
                stream = self.client.responses.create(**kwargs)

        content = ""
        for event in stream:
            if event.type == "response.output_text.delta":
                content += event.delta

        return LLMResponse(
            content=content,
            model=self.model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=self._elapsed_ms(start),
        )

    def _complete_chat_sync(
        self, prompt: str, system: str, temperature: float, max_tokens: int, start: float,
    ) -> LLMResponse:
        """Use standard OpenAI chat completions API (sync)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        extra = self._thinking_extra()
        if extra:
            kwargs["extra_body"] = extra
        resp = _chat_create_with_token_fallback(self.client, kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        # Reasoning models may leave content empty and stash the answer in
        # non-standard `reasoning_content` — surface it rather than
        # returning an empty response (2026-10 protocol modernization).
        content = choice.message.content or ""
        if not content.strip():
            content = _extract_reasoning(choice.message, "reasoning_content", "reasoning") or ""
        content = _strip_think(content)
        return LLMResponse(
            content=content,
            model=resp.model or self.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=self._elapsed_ms(start),
        )

    async def complete_stream(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """Real streaming — yield each provider delta as it arrives.

        review-swarm fix-all v3 #H7: bound the bridge queue so a slow /
        disconnected consumer back-pressures the producer instead of
        buffering unbounded provider tokens; spin off a cancel watcher
        thread that closes the upstream stream as soon as the consumer
        leaves so the executor thread doesn't keep the LLM call running
        (and billable) after the client is gone.
        """
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        cancel_event = threading.Event()
        active_stream: dict = {"obj": None}
        put_timeout = float(os.getenv("OPENAI_STREAM_PUT_TIMEOUT_S", "5.0"))

        def _thread_put(item):
            try:
                fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                fut.result(timeout=put_timeout)
            except Exception:
                cancel_event.set()

        def _cancel_watcher():
            # 2026-05-13 review-swarm fix-now HIGH #7: align the watcher
            # ceiling with `_DEFAULT_HTTP_TIMEOUT`. The pre-fix 180s
            # ceiling was shorter than the new 600s HTTP timeout, so a
            # genuinely-slow upstream that wasn't cancelled could
            # continue burning tokens for up to (HTTP_TIMEOUT - 180)s
            # = ~7 minutes after a disconnect. Tying both to the same
            # value keeps "client gone → stream stops" within one
            # budget window.
            cancel_event.wait(timeout=_DEFAULT_HTTP_TIMEOUT)
            obj = active_stream.get("obj")
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

        if _CANCEL_WATCHER_LIMIT.acquire(blocking=False):
            def _wrapped_watcher():
                try:
                    _cancel_watcher()
                finally:
                    _CANCEL_WATCHER_LIMIT.release()
            threading.Thread(target=_wrapped_watcher, daemon=True,
                             name="nlm-stream-cancel-watcher").start()

        def _producer():
            try:
                if self._is_codex:
                    input_msgs = []
                    if system:
                        input_msgs.append({"role": "system", "content": system})
                    input_msgs.append({"role": "user", "content": prompt})
                    kwargs = dict(
                        model=self.model, input=input_msgs,
                        temperature=temperature, max_output_tokens=max_tokens,
                        stream=True,
                    )
                    if _REASONING_EFFORT_VALID:
                        kwargs["reasoning"] = {"effort": _REASONING_EFFORT}
                    # 2026-05-13 review-swarm fix-now CRITICAL #4: same
                    # two-stage TypeError fallback as _complete_codex_sync.
                    try:
                        stream = self.client.responses.create(**kwargs)
                    except TypeError:
                        kwargs.pop("reasoning", None)
                        try:
                            stream = self.client.responses.create(**kwargs)
                        except TypeError:
                            kwargs.pop("max_output_tokens", None)
                            stream = self.client.responses.create(**kwargs)
                    active_stream["obj"] = stream
                    for event in stream:
                        if cancel_event.is_set():
                            break
                        if event.type == "response.output_text.delta":
                            _thread_put(event.delta)
                        elif event.type in (
                            "response.completed",
                            "response.incomplete",
                        ):
                            # Codex responses API surfaces max_output_tokens
                            # truncation via `response.incomplete_details.reason
                            # == "max_output_tokens"` on the terminal event.
                            # Surface it as a sentinel so the caller can warn
                            # the user instead of silently shipping a half-
                            # written `\begin{...}` env.
                            try:
                                resp = getattr(event, "response", None)
                                details = getattr(resp, "incomplete_details", None)
                                reason = getattr(details, "reason", None) if details else None
                                if reason == "max_output_tokens":
                                    _thread_put(TruncationSignal(reason=reason))
                            except Exception:
                                # Proxy variants may shape the event differently;
                                # never let a missing field blow up the stream.
                                pass
                else:
                    messages = []
                    if system:
                        messages.append({"role": "system", "content": system})
                    messages.append({"role": "user", "content": prompt})
                    stream_kwargs = {
                        "model": self.model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": True,
                    }
                    extra = self._thinking_extra()
                    if extra:
                        stream_kwargs["extra_body"] = extra
                    stream = _chat_create_with_token_fallback(self.client, stream_kwargs)
                    active_stream["obj"] = stream
                    # 2026-10 protocol modernization: strip <think> blocks
                    # on the way out; if the model emits ONLY reasoning
                    # (content never arrives), flush reasoning_content as
                    # the final body instead of returning an empty answer.
                    stripper = _ThinkStripper()
                    emitted_len = 0
                    reasoning_buf = []
                    saw_content = False
                    for event in stream:
                        if cancel_event.is_set():
                            break
                        if not event.choices:
                            continue
                        choice = event.choices[0]
                        delta_obj = choice.delta
                        delta = getattr(delta_obj, "content", None)
                        if delta:
                            saw_content = True
                            cleaned = stripper.feed(delta)
                            if cleaned:
                                _thread_put(cleaned)
                                emitted_len += len(cleaned)
                        else:
                            r = _extract_reasoning(delta_obj, "reasoning_content", "reasoning")
                            if r:
                                reasoning_buf.append(r)
                        # chat.completions reports truncation via the
                        # final chunk's `finish_reason == "length"`. Emit
                        # AFTER any content delta on the same chunk so
                        # the caller sees the full body first, then the
                        # truncation tag.
                        finish_reason = getattr(choice, "finish_reason", None)
                        if finish_reason == "length":
                            tail = stripper.flush()
                            if tail:
                                _thread_put(tail)
                            _thread_put(TruncationSignal(reason="length"))
                    if not saw_content:
                        tail = stripper.flush()
                        if tail:
                            _thread_put(tail)
                            emitted_len += len(tail)
                    if emitted_len == 0 and reasoning_buf:
                        _thread_put("".join(reasoning_buf))
            except Exception as exc:  # surface to consumer
                if not cancel_event.is_set():
                    _thread_put(exc)
            finally:
                cancel_event.set()
                obj = active_stream.get("obj")
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
                _thread_put(None)

        producer_fut = loop.run_in_executor(_executor, _producer)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            cancel_event.set()
            # Ensure producer completes / errors are surfaced
            try:
                await producer_fut
            except Exception:
                pass

    async def complete_structured(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        json_system = (system + "\n\n" if system else "") + (
            "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        resp = await self.complete(prompt, system=json_system, temperature=temperature, max_tokens=max_tokens)
        return _parse_json(resp.content)


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    return json.loads(text)
