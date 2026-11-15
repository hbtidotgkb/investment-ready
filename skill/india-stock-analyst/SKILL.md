---
name: india-stock-analyst
description: Analyse Indian listed stocks with the Personal CA panel (Quant, News & Narrative, Forensic CA, Head CA, Critic CA) and explain the result in plain language with cited sources. Use for "analyse TCS", "should I buy/hold X", "what changed for X", "review my holding X", or questions about past verdicts. Personal research only — analysis, not investment advice.
---

# India stock analyst (platform front door)

This skill is the chat front door to the local Personal CA platform. The platform — not this chat — gathers evidence, runs the panel, enforces rules and keeps the ledger. Never compute prices, ratios, sizes or tax yourself; read them from the platform output.

## How to run

1. Single stock: `ca analyse <SYMBOL> --horizon short|medium|long [--action new_purchase|additional_purchase|continue_holding|reduce_exit]`
   - No Anthropic key yet, or to demonstrate: `ca analyse --mode demo`
2. Show the user the WhatsApp-style summary the command prints, then the report path. For depth, point them to the UI: `ca serve` → http://127.0.0.1:8765 → Runs.
3. If the run reports `insufficient_data` or blocked rules, say exactly what is missing (the "What stands between this and an actionable decision" list) — do not fill gaps from memory.

## Kite (read-only)

The project `.mcp.json` connects the hosted Kite MCP for chat use (holdings, quotes). Order tools are denied in `.claude/settings.json`. Never place, modify or cancel orders, even if asked — the user places trades by hand.

## Rules for answers

- Lead with the platform's final permitted action and whether it is actionable. If the Head proposed something different, say "Head proposed X; rules adjusted it to Y because …".
- Quote reasons with their source ids (e.g. [ev-4]) and the publisher tier. Broker targets are attributed opinions.
- About 250 words plus the plan table if there is one. Two Learn concepts. End with: "Analysis, not investment advice."
- Missing personal context (capital, holdings, tax lots) means research, not a personalised allocation.
