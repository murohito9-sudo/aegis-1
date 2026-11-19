# AEGIS-ONE V3 — Implementation status

Branch: `aegis-one-v3-v2based`
Base: `codex/aegis-v2-hardening` (existing V2 Pull Request #4)
Status: draft; not merged.

## Implemented in this branch
- Separate default DEMO and LIVE SQLite, JSONL, and kill-switch paths.
- Legacy `AEGIS_DB_PATH` and `AEGIS_JSONL_PATH` remain DEMO aliases; LIVE has explicit `AEGIS_LIVE_DB_PATH` and `AEGIS_LIVE_JSONL_PATH`.
- Reject explicitly shared DEMO/LIVE database and JSONL paths.
- Engine refuses startup with `AEGIS_LIVE_MODE=true` until authorization, displayed mode, state isolation, and reconciliation are tested end-to-end. The adapter code is retained.
- PaperBroker supports deterministic per-tick partial fill participation via `AEGIS_PAPER_FILL_FRACTION`; default 1.0 preserves legacy behavior.
- Engine scheduler now runs heartbeat every 30s, health checks every 60s, and backup every 6h with seven-backup retention.
- Added `tests_v3.py` and CI invocation.
- Existing V2 institutional UI, Research/Monitoring integrations, and Playwright responsive UI tests are inherited from the V2 hardening branch and are not replaced.

## Remaining acceptance gates
1. Add Engine-level aggregation tests for multiple partial entry fills and protective-order resizing. Keep `AEGIS_PAPER_FILL_FRACTION=1.0` until then.
2. Verify SQLite backup consistency and perform restore drills; handle WAL explicitly if enabled.
3. Add runtime tests for DEMO/LIVE dashboard and state isolation.
4. Complete mocked LIVE authorization and exchange reconciliation tests before considering removal of the fail-closed startup gate.
5. Verify every mutating API route has appropriate authorization and rate limiting.
6. Run and inspect the full GitHub Actions matrix and browser tests.
7. Perform a visual regression review to confirm no existing V2 UI controls, tabs, charts, or navigation are lost.

## Safety
Do not enable LIVE or supply exchange credentials to this branch. Engine startup deliberately rejects LIVE. This PR must remain draft until the acceptance gates are reviewed.
