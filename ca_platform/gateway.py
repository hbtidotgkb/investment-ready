"""LLM gateway (spec §12). One place where every model call happens.

Guarantees:
  * Monthly budget cap enforced in code, with a per-run reservation taken before a panel.
  * Every call recorded: role, prompt version, configured + served model, provider request id,
    tokens, web searches, cost, latency, full request and response (local audit store).
  * Cache keyed by a hash of the COMPLETE effective request — any change to inputs, prompt,
    model or settings changes the key (spec §12 "Cache by a canonical hash ...").
  * Structured outputs validated by Pydantic, then by a caller-supplied semantic validator.
    Up to `max_validation_repairs` bounded repair attempts carry the exact validation errors.
    Repair never asks the model to change its analytical view — only to fix the stated defects.
  * Server-side refusal fallback is enabled; if a fallback model served the call, the
    substitution is recorded and surfaced (never silent).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from .ledger import Ledger
from .settings import canonical_hash, config, get_secret, iso, now_ist

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


class CallResult(BaseModel):
    parsed: Any = None
    text: str = ""
    raw_content: list = []
    record: dict = {}


def _price(model: str) -> dict:
    return config("models")["pricing_usd_per_mtok"].get(model, {"input": 0.0, "output": 0.0})


def cost_of(model: str, usage: Any, web_searches: int = 0) -> float:
    p = _price(model)
    inp = getattr(usage, "input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    usd = (inp * p["input"] + cw * p["input"] * 1.25 + cr * p["input"] * 0.1 + out * p["output"]) / 1e6
    usd += web_searches * config("models")["pricing_usd_per_mtok"].get("web_search_per_1k", 10.0) / 1000
    return round(usd, 6)


class Budget:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def status(self) -> dict:
        cap = float(config("user")["budget"]["monthly_llm_budget_usd"])
        spent = self.ledger.month_spend_usd(now_ist().strftime("%Y-%m"))
        return {"cap_usd": cap, "spent_usd": round(spent, 4), "remaining_usd": round(cap - spent, 4)}

    def reserve(self, amount: float) -> None:
        s = self.status()
        if s["remaining_usd"] < amount:
            raise BudgetExceeded(f"Monthly LLM budget would be exceeded: remaining ${s['remaining_usd']:.2f}, "
                                 f"this run reserves ${amount:.2f}. Raise budget in config/user.yaml or wait.")


class Gateway:
    def __init__(self, ledger: Ledger, run_id: str, log: Callable[[str, str], None] | None = None):
        self.ledger = ledger
        self.run_id = run_id
        self.log = log or (lambda stage, msg: None)
        self.cfg = config("models")
        self.total_cost = 0.0
        self.fallback_events: list[dict] = []
        key = get_secret("ANTHROPIC_API_KEY")
        self._client = None
        if key:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=600.0)

    @property
    def available(self) -> bool:
        return self._client is not None

    def role_cfg(self, role: str) -> dict:
        return self.cfg["roles"][role]

    # ------------------------------------------------------------ cache ---
    def _cached(self, request_hash: str) -> Optional[dict]:
        return self.ledger.fetchone(
            "SELECT * FROM request_records WHERE request_hash=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
            [request_hash])

    def _record(self, **kw) -> dict:
        rec = {"request_id": kw.get("request_id") or f"req-{uuid.uuid4().hex[:12]}", "run_id": self.run_id,
               "role": kw["role"], "model": kw["model"], "prompt_version": kw["prompt_version"],
               "request_hash": kw["request_hash"], "provider_request_id": kw.get("provider_request_id"),
               "input_tokens": kw.get("input_tokens", 0), "output_tokens": kw.get("output_tokens", 0),
               "web_searches": kw.get("web_searches", 0), "cost_usd": kw.get("cost_usd", 0.0),
               "latency_ms": kw.get("latency_ms", 0), "status": kw["status"], "created_at": iso(),
               "request": json.dumps(kw.get("request"), default=str, ensure_ascii=False),
               "response": json.dumps(kw.get("response"), default=str, ensure_ascii=False)}
        self.ledger.execute("INSERT INTO request_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            list(rec.values()))
        self.total_cost += rec["cost_usd"]
        rec["served_model"] = kw.get("served_model", kw["model"])
        rec["cache_hit"] = kw.get("cache_hit", False)
        rec["fallback"] = kw.get("fallback")
        return rec

    # ------------------------------------------------------ structured ---
    async def structured(self, *, role: str, stage: str, system: str, user: str,
                         output_model: Type[BaseModel],
                         semantic_check: Callable[[BaseModel], list[str]] | None = None) -> CallResult:
        """Structured call with Pydantic + semantic validation and bounded repair."""
        if not self.available:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        rc = self.role_cfg(role)
        model, effort, max_tokens = rc["model"], rc.get("effort", "high"), rc.get("max_tokens", 16000)
        messages: list[dict] = [{"role": "user", "content": user}]
        attempts = 1 + int(self.cfg.get("max_validation_repairs", 2))
        last_errors: list[str] = []
        history: list[dict] = []
        for attempt in range(attempts):
            request = {"model": model, "effort": effort, "max_tokens": max_tokens, "system": system,
                       "messages": messages, "schema": output_model.__name__, "prompt_version": rc["prompt"]}
            rhash = canonical_hash(request)
            cached = self._cached(rhash)
            if cached:
                resp = json.loads(cached["response"])
                parsed = output_model.model_validate(resp["parsed"])
                rec = dict(cached, cache_hit=True, served_model=resp.get("served_model", model))
                self.log(stage, f"Cache hit for identical request ({rhash[:10]}…) — no new model call")
            else:
                self.log(stage, f"Calling {model} (effort={effort}, prompt {rc['prompt']})"
                         + (f" — repair attempt {attempt}" if attempt else ""))
                parsed, rec = await self._call_parse(role, request, rhash, system, messages, model, effort,
                                                     max_tokens, output_model)
                self.log(stage, f"Response in {rec['latency_ms']/1000:.1f}s · {rec['input_tokens']:,} in / "
                         f"{rec['output_tokens']:,} out tokens · ${rec['cost_usd']:.4f}"
                         + (f" · served by fallback {rec['served_model']}" if rec.get("fallback") else ""))
            errors: list[str] = []
            if parsed is None:
                errors = [rec.get("error", "No valid structured output")]
            elif semantic_check:
                errors = semantic_check(parsed)
            history.append({"attempt": attempt, "request_id": rec["request_id"], "errors": errors})
            if not errors:
                return CallResult(parsed=parsed, record=dict(rec, attempts=history))
            last_errors = errors
            self.log(stage, f"Validation found {len(errors)} defect(s): " + "; ".join(errors[:3])
                     + (" …" if len(errors) > 3 else ""))
            if attempt + 1 < attempts:
                prior = parsed.model_dump_json() if parsed is not None else "(no parseable output)"
                messages = messages + [
                    {"role": "assistant", "content": prior},
                    {"role": "user", "content": (
                        "Your previous output failed validation. Fix ONLY these defects and return the full corrected "
                        "object. Do not change your analytical conclusions unless a defect forces it (e.g. a claim "
                        "whose citation does not exist must be removed or re-cited). Defects:\n- "
                        + "\n- ".join(errors))},
                ]
        raise ValueError(f"{role}: output still invalid after {attempts} attempt(s): " + "; ".join(last_errors[:5]))

    async def _call_parse(self, role, request, rhash, system, messages, model, effort, max_tokens, output_model):
        import anthropic
        t0 = time.monotonic()
        try:
            resp = await self._client.beta.messages.parse(
                model=model, max_tokens=max_tokens, system=system, messages=messages,
                output_config={"effort": effort}, output_format=output_model,
                betas=[FALLBACK_BETA], fallbacks="default",
            )
        except (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError) as e:
            rec = self._record(role=role, model=model, prompt_version=request["prompt_version"], request_hash=rhash,
                               status="error", request=request, response={"error": str(e)},
                               latency_ms=int((time.monotonic() - t0) * 1000))
            raise LLMUnavailable(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
        except (ValidationError, json.JSONDecodeError) as e:
            rec = self._record(role=role, model=model, prompt_version=request["prompt_version"], request_hash=rhash,
                               status="invalid", request=request, response={"error": str(e)[:2000]},
                               latency_ms=int((time.monotonic() - t0) * 1000))
            rec["error"] = f"Schema validation failed: {str(e)[:400]}"
            return None, rec
        latency = int((time.monotonic() - t0) * 1000)
        served = getattr(resp, "model", model)
        fallback = None
        if served and served != model:
            fallback = {"configured": model, "served": served, "role": role}
            self.fallback_events.append(fallback)
        usage = resp.usage
        cost = cost_of(served if served in config("models")["pricing_usd_per_mtok"] else model, usage)
        parsed = getattr(resp, "parsed_output", None)
        error = None
        if resp.stop_reason == "refusal":
            parsed, error = None, f"Model declined (refusal: {getattr(resp.stop_details, 'category', None)})"
        elif resp.stop_reason == "max_tokens":
            parsed, error = None, "Output truncated at max_tokens"
        rec = self._record(role=role, model=model, prompt_version=request["prompt_version"], request_hash=rhash,
                           provider_request_id=getattr(resp, "_request_id", None),
                           input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=cost,
                           latency_ms=latency, status="ok" if parsed is not None else "invalid", request=request,
                           response={"parsed": parsed.model_dump() if parsed is not None else None,
                                     "stop_reason": resp.stop_reason, "served_model": served},
                           served_model=served, fallback=fallback)
        if error:
            rec["error"] = error
        return parsed, rec

    # ---------------------------------------------------- web discovery ---
    async def web_discovery(self, *, role: str, stage: str, system: str, user: str,
                            allowed_domains: list[str], max_uses: int = 5) -> dict:
        """Search-grounded discovery. Returns cited passages straight from the API's
        citation objects (url, title, verbatim cited_text) plus the raw result list."""
        if not self.available:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        rc = self.role_cfg(role)
        model, effort = rc["model"], rc.get("effort", "low")
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses,
                  "allowed_domains": allowed_domains, "user_location": {"type": "approximate", "country": "IN"}}]
        messages: list[dict] = [{"role": "user", "content": user}]
        request = {"model": model, "effort": effort, "system": system, "messages": messages, "tools": tools,
                   "prompt_version": rc["prompt"], "day": now_ist().date().isoformat()}
        rhash = canonical_hash(request)
        cached = self._cached(rhash)
        if cached:
            self.log(stage, "Cache hit — reusing today's identical discovery request")
            return json.loads(cached["response"]) | {"record": dict(cached, cache_hit=True)}
        self.log(stage, f"Search-grounded discovery with {model} on {len(allowed_domains)} allow-listed domains")
        t0 = time.monotonic()
        content: list = []
        usage_in = usage_out = searches = 0
        served = model
        resp = None
        for _ in range(4):  # handle pause_turn continuation
            resp = await self._client.beta.messages.create(
                model=model, max_tokens=rc.get("max_tokens", 16000), system=system, messages=messages,
                tools=tools, output_config={"effort": effort}, betas=[FALLBACK_BETA], fallbacks="default")
            served = resp.model or model
            usage_in += resp.usage.input_tokens
            usage_out += resp.usage.output_tokens
            stu = getattr(resp.usage, "server_tool_use", None)
            searches += getattr(stu, "web_search_requests", 0) or 0
            content.extend(resp.content)
            if resp.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": resp.content}]
        latency = int((time.monotonic() - t0) * 1000)

        cited: list[dict] = []
        results: list[dict] = []
        text_parts: list[str] = []
        for b in content:
            if b.type == "text":
                text_parts.append(b.text)
                for c in (getattr(b, "citations", None) or []):
                    if getattr(c, "type", "") == "web_search_result_location":
                        cited.append({"url": c.url, "title": c.title, "cited_text": c.cited_text,
                                      "claim": b.text.strip()})
            elif b.type == "web_search_tool_result":
                if isinstance(b.content, list):
                    for r in b.content:
                        results.append({"url": r.url, "title": r.title, "page_age": getattr(r, "page_age", None)})
        usage = type("U", (), {"input_tokens": usage_in, "output_tokens": usage_out})()
        cost = cost_of(model, usage, searches)
        out = {"text": "\n".join(text_parts), "citations": cited, "results": results,
               "served_model": served, "stop_reason": resp.stop_reason if resp else None}
        rec = self._record(role=role, model=model, prompt_version=rc["prompt"], request_hash=rhash,
                           provider_request_id=getattr(resp, "_request_id", None), input_tokens=usage_in,
                           output_tokens=usage_out, web_searches=searches, cost_usd=cost, latency_ms=latency,
                           status="ok", request=request, response=out, served_model=served)
        self.log(stage, f"{searches} searches, {len(results)} results, {len(cited)} cited passages · ${cost:.4f}")
        return out | {"record": rec}

    # ------------------------------------------------- free text + docs ---
    async def cited_answer(self, *, role: str, system: str, documents: list[dict], question: str) -> dict:
        """Research Q&A using the Citations API over custom-content documents. Each passage is
        its own content block, so every citation maps back to an exact passage id."""
        if not self.available:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        rc = self.role_cfg(role)
        model = rc["model"]
        content: list[dict] = []
        for d in documents:
            content.append({"type": "document", "title": d["title"][:250],
                            "source": {"type": "content",
                                       "content": [{"type": "text", "text": p["text"]} for p in d["passages"]]},
                            "citations": {"enabled": True}})
        content.append({"type": "text", "text": question})
        request = {"model": model, "system": system, "docs": [[p["id"] for p in d["passages"]] for d in documents],
                   "question": question, "prompt_version": rc["prompt"]}
        rhash = canonical_hash(request | {"texts": canonical_hash(documents)})
        t0 = time.monotonic()
        resp = await self._client.messages.create(model=model, max_tokens=rc.get("max_tokens", 8000), system=system,
                                                  messages=[{"role": "user", "content": content}],
                                                  output_config={"effort": rc.get("effort", "low")})
        parts = []
        for b in resp.content:
            if b.type != "text":
                continue
            cites = []
            for c in (getattr(b, "citations", None) or []):
                if getattr(c, "type", "") == "content_block_location":
                    doc = documents[c.document_index]
                    pids = [doc["passages"][i]["id"] for i in range(c.start_block_index, c.end_block_index)
                            if i < len(doc["passages"])]
                    cites.append({"document": doc["title"], "source_id": doc.get("source_id"),
                                  "passage_ids": pids, "cited_text": c.cited_text})
            parts.append({"text": b.text, "citations": cites})
        cost = cost_of(model, resp.usage)
        self._record(role=role, model=model, prompt_version=rc["prompt"], request_hash=rhash,
                     provider_request_id=getattr(resp, "_request_id", None), input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens, cost_usd=cost,
                     latency_ms=int((time.monotonic() - t0) * 1000), status="ok", request=request,
                     response={"parts": parts})
        return {"parts": parts, "cost_usd": cost, "model": resp.model}
