"""LLM gateway (spec §12). One place where every model call happens, for every provider.

Guarantees (identical for Anthropic and OpenAI roles):
  * Monthly budget cap enforced in code, with a per-run reservation taken before a panel.
  * Every call recorded: role, provider, prompt version, configured + served model, provider
    request id, tokens, web searches, cost (or "price unknown"), latency, full request/response.
  * Cache keyed by a hash of the COMPLETE effective request (provider, model, settings, prompt,
    messages, schema) — any change to inputs changes the key.
  * Structured outputs validated by Pydantic, then by a caller-supplied semantic validator, with
    up to `max_validation_repairs` bounded repairs carrying the exact defects. Repair never asks
    the model to change its analytical view.
  * Anthropic: server-side refusal fallback enabled; a substitution is recorded, never silent.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from .ledger import Ledger
from .settings import PROVIDER_KEYS, canonical_hash, config, get_secret, iso, now_ist

FALLBACK_BETA = "server-side-fallback-2026-07-01"
OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class BudgetExceeded(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


class CallResult(BaseModel):
    parsed: Any = None
    text: str = ""
    raw_content: list = []
    record: dict = {}


def price_of(model: str) -> dict | None:
    return config("models")["pricing_usd_per_mtok"].get(model)


def cost_of(model: str, usage: Any, web_searches: int = 0) -> tuple[float, bool]:
    """(usd, price_known). Unknown prices return 0.0 with price_known=False — never a guess."""
    p = price_of(model)
    if not p:
        return 0.0, False
    inp = getattr(usage, "input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    usd = (inp * p["input"] + cw * p["input"] * 1.25 + cr * p["input"] * 0.1 + out * p["output"]) / 1e6
    usd += web_searches * config("models")["pricing_usd_per_mtok"].get("web_search_per_1k", 10.0) / 1000
    return round(usd, 6), True


def role_provider(role: str) -> str:
    return config("models")["roles"][role].get("provider", "anthropic")


def vendor_separation() -> dict:
    roles = config("models")["roles"]
    prov = {r: roles[r].get("provider", "anthropic") for r in ("quant", "news", "forensic", "head", "critic")}
    hc = prov["head"] != prov["critic"]
    brains = len({prov["quant"], prov["news"], prov["forensic"]})
    status = "separated" if hc and brains >= 2 else ("partial" if hc or brains >= 2 else "degraded")
    return {"status": status, "head_critic_separated": hc, "distinct_brain_providers": brains, "providers": prov,
            "models": {r: roles[r]["model"] for r in prov}}


def missing_keys(roles: list[str]) -> list[str]:
    need = {PROVIDER_KEYS[role_provider(r)] for r in roles}
    return sorted(k for k in need if not get_secret(k))


class Budget:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def status(self) -> dict:
        cap = float(config("user")["budget"]["monthly_llm_budget_usd"])
        spent = self.ledger.month_spend_usd(now_ist().strftime("%Y-%m"))
        unknown = self.ledger.fetchone(
            "SELECT COUNT(*) AS n FROM request_records WHERE created_at LIKE ? AND status='ok' AND cost_usd IS NULL",
            [now_ist().strftime("%Y-%m") + "%"])["n"]
        return {"cap_usd": cap, "spent_usd": round(spent, 4), "remaining_usd": round(cap - spent, 4),
                "calls_with_unknown_price": unknown}

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
        self.unknown_cost_calls = 0
        self.fallback_events: list[dict] = []
        self._clients: dict[str, Any] = {}

    # ----------------------------------------------------------- clients ---
    def client(self, provider: str):
        if provider in self._clients:
            return self._clients[provider]
        key = get_secret(PROVIDER_KEYS[provider])
        if not key:
            raise LLMUnavailable(f"{PROVIDER_KEYS[provider]} is not configured (needed for {provider} roles)")
        if provider == "anthropic":
            import anthropic
            c = anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=600.0)
        elif provider == "openai":
            import openai
            c = openai.AsyncOpenAI(api_key=key, max_retries=2, timeout=600.0)
        else:
            raise LLMUnavailable(f"Unsupported provider {provider!r}")
        self._clients[provider] = c
        return c

    def available_for(self, role: str) -> bool:
        return bool(get_secret(PROVIDER_KEYS[role_provider(role)]))

    @property
    def available(self) -> bool:  # discovery/research helpers are Anthropic-locked
        return bool(get_secret(PROVIDER_KEYS["anthropic"]))

    def role_cfg(self, role: str) -> dict:
        return self.cfg["roles"][role]

    # ------------------------------------------------------------ cache ---
    def _cached(self, request_hash: str) -> Optional[dict]:
        return self.ledger.fetchone(
            "SELECT * FROM request_records WHERE request_hash=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
            [request_hash])

    def _record(self, **kw) -> dict:
        price_known = kw.get("price_known", True)
        cost = kw.get("cost_usd", 0.0) if price_known else None
        rec = {"request_id": kw.get("request_id") or f"req-{uuid.uuid4().hex[:12]}", "run_id": self.run_id,
               "role": kw["role"], "model": kw["model"], "prompt_version": kw["prompt_version"],
               "request_hash": kw["request_hash"], "provider_request_id": kw.get("provider_request_id"),
               "input_tokens": kw.get("input_tokens", 0), "output_tokens": kw.get("output_tokens", 0),
               "web_searches": kw.get("web_searches", 0), "cost_usd": cost,
               "latency_ms": kw.get("latency_ms", 0), "status": kw["status"], "created_at": iso(),
               "request": json.dumps(kw.get("request"), default=str, ensure_ascii=False),
               "response": json.dumps(kw.get("response"), default=str, ensure_ascii=False)}
        self.ledger.execute("INSERT INTO request_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            list(rec.values()))
        if cost is None and kw["status"] == "ok":
            self.unknown_cost_calls += 1
        self.total_cost += cost or 0.0
        rec["cost_usd"] = cost or 0.0
        rec["price_known"] = price_known
        rec["provider"] = kw.get("provider", "anthropic")
        rec["served_model"] = kw.get("served_model", kw["model"])
        rec["cache_hit"] = kw.get("cache_hit", False)
        rec["fallback"] = kw.get("fallback")
        return rec

    # ------------------------------------------------------ structured ---
    async def structured(self, *, role: str, stage: str, system: str, user: str,
                         output_model: Type[BaseModel],
                         semantic_check: Callable[[BaseModel], list[str]] | None = None) -> CallResult:
        """Structured call with Pydantic + semantic validation and bounded repair, any provider."""
        rc = self.role_cfg(role)
        provider = rc.get("provider", "anthropic")
        model, effort, max_tokens = rc["model"], rc.get("effort", "high"), rc.get("max_tokens", 16000)
        self.client(provider)  # fail fast with a clear message if the key is missing
        messages: list[dict] = [{"role": "user", "content": user}]
        attempts = 1 + int(self.cfg.get("max_validation_repairs", 2))
        last_errors: list[str] = []
        history: list[dict] = []
        for attempt in range(attempts):
            request = {"provider": provider, "model": model, "effort": effort, "max_tokens": max_tokens,
                       "system": system, "messages": messages, "schema": output_model.__name__,
                       "prompt_version": rc["prompt"]}
            rhash = canonical_hash(request)
            cached = self._cached(rhash)
            if cached:
                resp = json.loads(cached["response"])
                parsed = output_model.model_validate(resp["parsed"])
                rec = dict(cached, cache_hit=True, provider=provider, served_model=resp.get("served_model", model),
                           cost_usd=0.0, price_known=True)
                self.log(stage, f"Cache hit for identical request ({rhash[:10]}…) — no new model call")
            else:
                self.log(stage, f"Calling {provider}:{model} (effort={effort}, prompt {rc['prompt']})"
                         + (f" — repair attempt {attempt}" if attempt else ""))
                call = self._call_anthropic if provider == "anthropic" else self._call_openai
                parsed, rec = await call(role, request, rhash, system, messages, model, effort, max_tokens, output_model)
                cost_txt = f"${rec['cost_usd']:.4f}" if rec["price_known"] else "cost unknown (add pricing in config/models.yaml)"
                self.log(stage, f"Response in {rec['latency_ms']/1000:.1f}s · {rec['input_tokens']:,} in / "
                         f"{rec['output_tokens']:,} out tokens · {cost_txt}"
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

    async def _call_anthropic(self, role, request, rhash, system, messages, model, effort, max_tokens, output_model):
        import anthropic
        t0 = time.monotonic()
        base = dict(role=role, model=model, prompt_version=request["prompt_version"], request_hash=rhash,
                    request=request, provider="anthropic")
        try:
            resp = await self.client("anthropic").beta.messages.parse(
                model=model, max_tokens=max_tokens, system=system, messages=messages,
                output_config={"effort": effort}, output_format=output_model,
                betas=[FALLBACK_BETA], fallbacks="default",
            )
        except (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError) as e:
            self._record(**base, status="error", response={"error": str(e)}, latency_ms=int((time.monotonic() - t0) * 1000))
            raise LLMUnavailable(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
        except (ValidationError, json.JSONDecodeError) as e:
            rec = self._record(**base, status="invalid", response={"error": str(e)[:2000]},
                               latency_ms=int((time.monotonic() - t0) * 1000))
            rec["error"] = f"Schema validation failed: {str(e)[:400]}"
            return None, rec
        latency = int((time.monotonic() - t0) * 1000)
        served = getattr(resp, "model", model) or model
        fallback = None
        if served != model:
            fallback = {"configured": model, "served": served, "role": role}
            self.fallback_events.append(fallback)
        cost, known = cost_of(served if price_of(served) else model, resp.usage)
        parsed = getattr(resp, "parsed_output", None)
        error = None
        if resp.stop_reason == "refusal":
            parsed, error = None, f"Model declined (refusal: {getattr(resp.stop_details, 'category', None)})"
        elif resp.stop_reason == "max_tokens":
            parsed, error = None, "Output truncated at max_tokens"
        rec = self._record(**base, provider_request_id=getattr(resp, "_request_id", None),
                           input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens, cost_usd=cost,
                           price_known=known, latency_ms=latency, status="ok" if parsed is not None else "invalid",
                           response={"parsed": parsed.model_dump() if parsed is not None else None,
                                     "stop_reason": resp.stop_reason, "served_model": served},
                           served_model=served, fallback=fallback)
        if error:
            rec["error"] = error
        return parsed, rec

    async def _call_openai(self, role, request, rhash, system, messages, model, effort, max_tokens, output_model):
        """OpenAI Responses API with Pydantic structured output (strict JSON schema)."""
        import openai
        t0 = time.monotonic()
        base = dict(role=role, model=model, prompt_version=request["prompt_version"], request_hash=rhash,
                    request=request, provider="openai")
        kwargs = dict(model=model, instructions=system, input=[{"role": m["role"], "content": m["content"]} for m in messages],
                      text_format=output_model, max_output_tokens=max_tokens, store=False,
                      reasoning={"effort": OPENAI_EFFORT.get(effort, "medium")})
        client = self.client("openai")
        try:
            try:
                resp = await client.responses.parse(**kwargs)
            except openai.BadRequestError as e:
                if "reasoning" not in str(e).lower():
                    raise
                kwargs.pop("reasoning")  # non-reasoning model: retry once without the effort setting
                self.log("gateway", f"{model} does not accept reasoning effort — retrying without it")
                resp = await client.responses.parse(**kwargs)
        except (openai.BadRequestError, openai.AuthenticationError, openai.PermissionDeniedError,
                openai.NotFoundError) as e:
            self._record(**base, status="error", response={"error": str(e)}, latency_ms=int((time.monotonic() - t0) * 1000))
            raise LLMUnavailable(f"OpenAI {type(e).__name__}: {getattr(e, 'message', e)}") from e
        except (ValidationError, json.JSONDecodeError) as e:
            rec = self._record(**base, status="invalid", response={"error": str(e)[:2000]},
                               latency_ms=int((time.monotonic() - t0) * 1000))
            rec["error"] = f"Schema validation failed: {str(e)[:400]}"
            return None, rec
        latency = int((time.monotonic() - t0) * 1000)
        served = getattr(resp, "model", model) or model
        parsed = getattr(resp, "output_parsed", None)
        error = None
        if getattr(resp, "status", "completed") == "incomplete":
            parsed = None
            error = f"Incomplete response: {getattr(getattr(resp, 'incomplete_details', None), 'reason', 'unknown')}"
        elif parsed is None:
            refusal = next((c.refusal for o in (resp.output or []) for c in (getattr(o, "content", None) or [])
                            if getattr(c, "type", "") == "refusal"), None)
            error = f"Model declined: {refusal}" if refusal else "No structured output returned"
        usage = resp.usage
        cost, known = cost_of(model, usage)
        rec = self._record(**base, provider_request_id=getattr(resp, "id", None),
                           input_tokens=getattr(usage, "input_tokens", 0), output_tokens=getattr(usage, "output_tokens", 0),
                           cost_usd=cost, price_known=known, latency_ms=latency,
                           status="ok" if parsed is not None else "invalid",
                           response={"parsed": parsed.model_dump() if parsed is not None else None,
                                     "status": getattr(resp, "status", None), "served_model": served},
                           served_model=served)
        if error:
            rec["error"] = error
        return parsed, rec

    # ---------------------------------------------------- web discovery ---
    async def web_discovery(self, *, role: str, stage: str, system: str, user: str,
                            allowed_domains: list[str], max_uses: int = 5) -> dict:
        """Search-grounded discovery with domains Anthropic's search cannot reach removed first.

        Some publishers block Anthropic's crawler; the API then rejects the whole request. Blocked
        domains are learned from that error, remembered, logged, and the request is retried without
        them (their content still arrives via RSS headlines and your browser captures)."""
        import anthropic
        blocked = blocked_search_domains()
        usable = [d for d in allowed_domains if d not in blocked]
        skipped = [d for d in allowed_domains if d in blocked]
        if skipped:
            self.log(stage, f"Search skips {len(skipped)} publisher(s) that block Anthropic's crawler: {', '.join(skipped)}")
        for _ in range(3):
            if not usable:
                raise LLMUnavailable("No allow-listed domain is reachable by web search")
            try:
                return await self._web_discovery(role=role, stage=stage, system=system, user=user,
                                                 allowed_domains=usable, max_uses=max_uses)
            except anthropic.BadRequestError as e:
                found = _blocked_from_error(str(e))
                if not found:
                    raise
                remember_blocked_domains(found)
                usable = [d for d in usable if d not in found]
                self.log(stage, f"Publishers blocking Anthropic's crawler (now skipped): {', '.join(found)}")
        raise LLMUnavailable("Web search kept rejecting the domain list")

    async def _web_discovery(self, *, role: str, stage: str, system: str, user: str,
                             allowed_domains: list[str], max_uses: int = 5) -> dict:
        rc = self.role_cfg(role)
        model, effort = rc["model"], rc.get("effort", "low")
        client = self.client("anthropic")
        # Deliberately the BASIC search tool: web_search_20260209 filters results through code
        # execution and its final text carries no citation objects (verified 15 Nov 2026), which
        # would break verbatim-quote accountability. The basic tool returns web_search_result_location
        # citations with the source's own cited_text.
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses,
                  "allowed_domains": allowed_domains, "user_location": {"type": "approximate", "country": "IN"}}]
        messages: list[dict] = [{"role": "user", "content": user}]
        request = {"provider": "anthropic", "model": model, "effort": effort, "system": system, "messages": messages,
                   "tools": tools, "prompt_version": rc["prompt"], "day": now_ist().date().isoformat()}
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
            resp = await client.beta.messages.create(
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
        cost, known = cost_of(model, usage, searches)
        out = {"text": "\n".join(text_parts), "citations": cited, "results": results,
               "served_model": served, "stop_reason": resp.stop_reason if resp else None}
        rec = self._record(role=role, model=model, prompt_version=rc["prompt"], request_hash=rhash,
                           provider_request_id=getattr(resp, "_request_id", None), input_tokens=usage_in,
                           output_tokens=usage_out, web_searches=searches, cost_usd=cost, price_known=known,
                           latency_ms=latency, status="ok", request=request, response=out, served_model=served)
        self.log(stage, f"{searches} searches, {len(results)} results, {len(cited)} cited passages · ${rec['cost_usd']:.4f}")
        return out | {"record": rec}

    # ------------------------------------------------- free text + docs ---
    async def cited_answer(self, *, role: str, system: str, documents: list[dict], question: str) -> dict:
        """Research Q&A using the Citations API over custom-content documents. Each passage is
        its own content block, so every citation maps back to an exact passage id."""
        rc = self.role_cfg(role)
        model = rc["model"]
        client = self.client("anthropic")
        content: list[dict] = []
        for d in documents:
            content.append({"type": "document", "title": d["title"][:250],
                            "source": {"type": "content",
                                       "content": [{"type": "text", "text": p["text"]} for p in d["passages"]]},
                            "citations": {"enabled": True}})
        content.append({"type": "text", "text": question})
        request = {"provider": "anthropic", "model": model, "system": system,
                   "docs": [[p["id"] for p in d["passages"]] for d in documents],
                   "question": question, "prompt_version": rc["prompt"]}
        rhash = canonical_hash(request | {"texts": canonical_hash(documents)})
        t0 = time.monotonic()
        resp = await client.messages.create(model=model, max_tokens=rc.get("max_tokens", 8000), system=system,
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
        cost, known = cost_of(model, resp.usage)
        self._record(role=role, model=model, prompt_version=rc["prompt"], request_hash=rhash,
                     provider_request_id=getattr(resp, "_request_id", None), input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens, cost_usd=cost, price_known=known,
                     latency_ms=int((time.monotonic() - t0) * 1000), status="ok", request=request,
                     response={"parts": parts})
        return {"parts": parts, "cost_usd": cost, "model": resp.model}


# ------------------------------------------------- search-blocked publishers ---

def _blocked_file():
    from .settings import SETTINGS_DIR
    return SETTINGS_DIR / "search_blocked_domains.json"


def blocked_search_domains() -> set[str]:
    f = _blocked_file()
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text(encoding="utf-8")).get("domains", []))
    except (json.JSONDecodeError, OSError):
        return set()


def remember_blocked_domains(domains: list[str]) -> None:
    cur = blocked_search_domains() | set(domains)
    _blocked_file().write_text(json.dumps({"domains": sorted(cur), "updated_at": iso(),
                                           "why": "Publisher blocks Anthropic's crawler; web search rejects requests including it"}),
                               encoding="utf-8")


def _blocked_from_error(msg: str) -> list[str]:
    import re
    m = re.search(r"not accessible to our user agent:\s*\[([^\]]*)\]", msg)
    if not m:
        return []
    return [d.strip().strip("'\"") for d in m.group(1).split(",") if d.strip()]


# ------------------------------------------------------------ model catalog ---

_CATALOG: dict[str, tuple[float, list[dict]]] = {}


def model_catalog(provider: str, refresh: bool = False) -> dict:
    """Latest models from the provider's Models API (cached 1h). Falls back to suggestions
    from config when the key is missing — clearly labelled as not live."""
    now = time.time()
    if not refresh and provider in _CATALOG and now - _CATALOG[provider][0] < 3600:
        return {"provider": provider, "live": True, "models": _CATALOG[provider][1]}
    key = get_secret(PROVIDER_KEYS[provider])
    sugg = [{"id": m, "label": m, "priced": bool(price_of(m))} for m in config("models").get("suggested_models", {}).get(provider, [])]
    if not key:
        return {"provider": provider, "live": False, "models": sugg,
                "note": f"Add {PROVIDER_KEYS[provider]} to load the live model list."}
    try:
        if provider == "anthropic":
            import anthropic
            rows = list(anthropic.Anthropic(api_key=key).models.list(limit=50))
            models = [{"id": m.id, "label": getattr(m, "display_name", m.id), "created": str(getattr(m, "created_at", "")),
                       "priced": bool(price_of(m.id))} for m in rows]
        else:
            import openai
            rows = list(openai.OpenAI(api_key=key).models.list())
            keep = [m for m in rows if m.id.startswith(("gpt-", "o", "chatgpt-")) and not any(
                x in m.id for x in ("audio", "realtime", "transcribe", "tts", "image", "embedding", "search", "moderation"))]
            keep.sort(key=lambda m: getattr(m, "created", 0), reverse=True)
            models = [{"id": m.id, "label": m.id, "created": getattr(m, "created", None), "priced": bool(price_of(m.id))}
                      for m in keep[:15]]
        _CATALOG[provider] = (now, models)
        return {"provider": provider, "live": True, "models": models}
    except Exception as e:  # network/auth problems are shown, not hidden
        return {"provider": provider, "live": False, "models": sugg, "note": f"Live list unavailable: {type(e).__name__}"}
