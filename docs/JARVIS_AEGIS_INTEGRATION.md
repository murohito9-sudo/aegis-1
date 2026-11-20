# JARVIS → AEGIS-ONE Integration Blueprint

Status: Phase 0 architecture mapping. This document records the integration contract; it does not claim all JARVIS capabilities are implemented.

## Goal
AEGIS-ONE remains the single system of record and paper simulator. JARVIS capabilities become services and interfaces inside AEGIS-ONE. Do not maintain a second independent strategy engine, portfolio ledger, risk manager or trade history.

## Existing components and gaps

| JARVIS capability | Existing AEGIS-ONE component | Status / gap |
|---|---|---|
| Central brain | brain.py, strategy.py, main.py Engine | Rule-based briefing and strategy exist; a unified orchestrator contract is still needed. Do not duplicate signal generation. |
| Market data | data.py: Feed, MarketSnapshot, CCXTFeed, CSVFeed | Public CCXT, CSV, replay and synthetic feeds exist. Coinbase-specific and CMC context adapters are not verified as integrated. |
| Global market context | data.py, indicators.py, evaluation.py | Provider interfaces and provenance/freshness metadata needed. |
| On-chain / whale | onchain.py | Z-score engine and CSV provider exist; Arkham provider and wallet attribution are not integrated. |
| News / sentiment | news.py | RSS and lexicon sentiment exist; source quality, deduplication and stale-data handling need strengthening. |
| Technical analysis | indicators.py, intelligence.py, strategy.py | Existing core; Python is canonical while browser/Python parity is being tested. |
| Risk | risk.py, config.py | Central risk manager and guardrails exist; JARVIS must call these, never duplicate risk math. |
| Paper trading | execution.py, main.py, logger.py | Paper broker, fees, slippage, persistence and audit exist; reuse the single ledger. |
| Portfolio / DCA | portfolio.py | Holdings, weights, concentration, DCA planning, rebalance and stress test exist. |
| Performance / research | analytics.py, evaluation.py, research.py | Reports, calibration, walk-forward and Monte Carlo research exist. |
| Scheduler / alerts | monitor.py, logger.py, dashboard.py | Scheduler, health, heartbeat, backups, alerts, API and WebSocket exist. |
| Voice | voice.py, brain.py | Narration and optional TTS exist; connect through a shared briefing payload. |
| Telegram | No Telegram adapter confirmed in inspected files | New private adapter required: owner allowlist, command validation, rate limits, read-only status and paper-only commands. |
| Owner settings | config.py, logger.py, dashboard.py | Environment config exists; persistent editable settings with validation, versioning and audit remain to build. |
| Security | security.py, dashboard.py, logger.py | Rate limiting, CSRF and audit primitives exist; Telegram authorization and command permissions need tests. |

## Target architecture

```text
Coinbase / CCXT / CSV / CMC context / optional Arkham / RSS
                         |
              Provider adapters
                         |
         Data validation + provenance gate
                         |
              Canonical MarketSnapshot
                         |
       Indicators + MTF + microstructure
                         |
       On-chain + news + market context
                         |
        AEGIS Intelligence / JARVIS Brain
                         |
       Strategy decision + evidence + vetoes
                         |
            Central RiskManager gate
                         |
        PAPER broker only (single ledger)
                         |
       AuditLog + analytics + evaluation
               /         |          \\
         Dashboard    Telegram     Voice/TTS
```

## Non-negotiable contracts
1. One canonical Python engine and one paper ledger. Telegram, voice and dashboard are clients, not trading engines.
2. Every provider returns normalized data with source, symbol, timestamp, freshness, units and quality status. Missing or stale data is unknown, never silently converted into a directional vote.
3. Every brain output includes decision, score, evidence, source timestamps, missing inputs, vetoes and a human-readable explanation.
4. Every proposed paper action passes the existing RiskManager and PaperBroker; interfaces cannot bypass them.
5. Persist setting changes with actor, timestamp, old/new value, validation result and reason. Secrets never appear in API responses, logs, chat or voice.
6. Providers are optional. The system must work offline and degrade safely if CMC, Arkham, RSS, Coinbase or Telegram is unavailable.
7. Closed candles only; no look-ahead and no duplicate signal/order processing.
8. Dashboard, Telegram and voice report the same engine state and ledger values.

## Telegram command scope
Initial scope is read-only and paper-simulation only:
- /start, /help: safe command list and authorization status.
- /status: mode, feed health, last update, symbol and timeframe.
- /price, /market: public market snapshot with source timestamps.
- /analysis, /signal: engine analysis, evidence and vetoes.
- /risk: risk limits and simulated exposure/drawdown.
- /balance, /positions, /history, /report: paper account only.
- /portfolio, /dca: portfolio analytics and hypothetical plans.
- /onchain, /whale, /news: optional intelligence with source/freshness.
- /alerts, /voice, /health: operational status.
- /settings: initially read-only; later validated paper-setting changes with confirmation and audit.
No command may place real orders, access exchange account balances, expose credentials or enable live execution.

## Paper targets
- Preserve existing AEGIS-ONE risk limits and defaults.
- JARVIS targets are configurable paper-notification thresholds only: daily simulated PnL Rp2,000 and cumulative simulated-profit alert Rp10,000,001. They are not forecasts or guarantees and must not change sizing or force trades.
- Reaching a threshold must not cause an automatic trade, increase risk or stop analysis.
- IDR thresholds must not be compared with USDT ledger values without an explicit FX source and timestamp.

## Delivery phases and acceptance
### Phase 0 — inventory and contracts
- Map JARVIS capabilities to current modules and identify gaps without duplication.
### Phase 1 — unified intelligence contract
- Add a typed intelligence result schema.
- Compose existing strategy, brain, news, on-chain and microstructure results.
- Test stale/missing providers, provenance and veto precedence.
### Phase 2 — provider adapters
- Add Coinbase public market data, CMC context and optional Arkham adapter behind provider protocols.
- Add timeouts, bounded retries, caching, rate limits, schema validation and health reporting.
- Keep optional credentials server-side and redacted.
### Phase 3 — Telegram
- Add private owner allowlist, secure token configuration, rate limits and audit trail.
- Read the same engine state; initially read-only and paper-only.
- Test unauthorized users, malformed commands, duplicate updates, outages and secret redaction.
### Phase 4 — voice and settings
- Connect narration to the same briefing payload used by dashboard and Telegram.
- Add persistent settings registry, validation, safe bounds, audit history and explicit confirmation.
- No voice/chat action may bypass risk controls.
### Phase 5 — parity and end-to-end
- Verify Demo/Python parity with deterministic fixtures.
- Test offline startup, provider failures, Telegram outage, restart recovery, backup/restore, audit reconstruction and dashboard consistency.
- Run all legacy regressions; remove no existing feature.

## Completion definition
Integration is complete only when each capability has implementation, tests, documented configuration, observable health, failure-mode behavior and proof it uses the canonical engine/ledger. A module or UI panel alone is not proof of end-to-end integration.

## Safety boundary
This project is for learning, market research and paper simulation. Keep exchange live execution disabled and locked. Do not place real orders.