# AEGIS-ONE V3 — Implementation status

Branch: `aegis-one-v3-foundation-reliability`
Base: `main`
Status: review required; not merged.

## Implemented in this branch

### DEMO/LIVE storage separation
- Default DEMO SQLite, JSONL, and kill-switch paths are under `data/demo/`.
- LIVE-specific paths are under `data/live/`.
- New environment variables: `AEGIS_DEMO_DB_PATH`, `AEGIS_DEMO_JSONL_PATH`, `AEGIS_LIVE_DB_PATH`, `AEGIS_LIVE_JSONL_PATH`, `AEGIS_DEMO_KILL_SWITCH_FILE`, and `AEGIS_LIVE_KILL_SWITCH_FILE`.
- Legacy `AEGIS_DB_PATH` and `AEGIS_JSONL_PATH` remain supported for DEMO.
- Configuration rejects explicitly shared DEMO/LIVE database or JSONL paths.

### LIVE fail-closed
- `Engine` refuses startup whenever `AEGIS_LIVE_MODE=true`.
- Reason: the current Engine pre-trade flow does not yet pass a verifiable LIVE authorization signal, and its displayed mode/state is not yet proven consistent with the selected broker.
- The standalone adapter remains in the repository, but this Engine build must be treated as PAPER-only.
- This is an intentional temporary safety gate, not removal of the adapter.

### Paper partial fills
- `ExecutionConfig.paper_fill_fraction` and `AEGIS_PAPER_FILL_FRACTION` control deterministic per-tick fill participation.
- Default remains 1.0 to preserve existing behavior.
- Partial fills update cumulative filled quantity, weighted average price, fees, slippage, and order status.
- A V3 regression test covers partial-to-complete fill progression.
- Important limitation: Engine position aggregation for multiple partial entry fills still requires dedicated integration tests before setting this fraction below 1.0 in a live paper session.

### Scheduler and operations
- Engine now runs scheduled heartbeat (30s), health checks (60s), and backups (6h).
- Backups retain the latest seven backup directories and include the selected mode's database, JSONL, and heartbeat.
- Scheduled failures are written to the audit log.
- Restore remains an operator-controlled action; automatic destructive restore is not enabled.

### Testing
- Added `tests_v3.py` for path isolation and PaperBroker partial fills.
- CI runs the V3 regression module on the existing Python 3.11/3.12 matrix.
- CI has not yet been observed passing for this branch at the time this document was written.

## Remaining work before V3 can be considered complete

1. Add end-to-end tests proving partial entry fills aggregate into one position and correctly resize protective orders.
2. Test SQLite backup consistency and restore drills, including WAL handling if enabled later.
3. Add runtime isolation tests for dashboard endpoints and state files.
4. Complete LIVE authorization design, reconcile exchange positions/orders before enabling entries, and test dry-run with a mocked exchange.
5. Add authenticated/authorized API protections and rate limiting to every mutating endpoint.
6. Verify the entire legacy test suite and GitHub Actions results.
7. Review UI/UX changes against the existing dashboard so no controls, charts, or tabs are removed. This branch currently makes only the operator-facing live-guide correction; a full visual redesign is not claimed.

## Safety statement

Do not enable LIVE or provide exchange credentials to this branch. The Engine deliberately refuses LIVE startup. No real exchange order should be sent by the Engine from this branch.
