# AEGIS-ONE — Data Integrity & Evaluation Notes

Last updated: 2026-11-19  
Scope: PAPER / historical research only

## Data-quality policy

The market-data gate is fail-closed. A snapshot is rejected when any required OHLCV column is absent, any OHLCV cell is non-numeric or non-finite, prices are not positive, volume is negative, candle high/low do not contain open and close, timestamps are duplicated/out of order/non-positive, the latest bar is stale or in the future, or the quote/spread checks fail.

For CCXT snapshots, provider bid and ask are treated as observed quotes only when both values are present, finite, positive, and ordered (bid < ask). Missing provider quotes are not replaced with fabricated prices. Such snapshots are labelled quotes=missing and fail the quote gate.

A last price may fall back to the most recent candle close when the ticker has no last field; this is not a replacement for bid/ask and does not make the quote gate pass.

## Evaluation metric definitions

| Metric | Definition | Source / formula | Limitation |
|---|---|---|---|
| Samples | Number of closed outcomes included | Evaluation records | Depends on the loaded history window |
| Hit rate | Fraction of outcomes with positive R | count(R > 0) / N | Does not describe payoff size |
| Average R | Arithmetic mean of R-multiples | mean(r_multiple) | Sensitive to outliers and sample composition |
| Brier score | Mean squared error of predicted probability | mean((p - y)^2), where y is 1 for positive R and 0 otherwise | Only valid when p is an independently recorded probability in [0,1] |
| Probability calibration | Empirical win rate within predicted-probability buckets | Probability buckets; compare mean predicted probability with observed outcome rate | Not shown without explicit probability records; small buckets are noisy |
| Score performance | Hit rate and average R grouped by confluence-score bucket | Group by raw confluence score | This is performance attribution, not probability calibration |
| Drift | Difference between average R in the first and second chronological halves | Sort by timestamp, split in half, compare means | Simple descriptive heuristic, not a statistical significance test |

## Important interpretation rules

- A confluence score is not a probability.
- A confidence label is not a probability unless independently calibrated and validated.
- An outcome-derived score is leakage. Never reconstruct a prediction score from the eventual trade R-multiple or PnL.
- Brier score and probability calibration remain unavailable (null / empty) until a probability is recorded before its outcome and linked to that outcome.
- Current position history does not yet preserve a stable prediction identifier and pre-outcome probability. Therefore the current engine reports outcome statistics, but intentionally does not fabricate Brier/calibration values from closed positions.
- The drift threshold currently classifies a change larger than 0.25R as improving or worsening. It is descriptive and must not be interpreted as proof of predictive edge.

## CI evidence for the hardening branch

The GitHub Actions workflow runs the unit suite and offline smoke test on Python 3.11 and 3.12. The hardening branch's run on 2026-11-19 passed both jobs with 85 tests per Python version and both offline smoke tests. This validates the tested code paths only; it does not establish production readiness, profitability, or LIVE readiness.


## Walk-forward dan Monte Carlo (offline)

Modul `research.py` menambahkan dua alat riset deterministik yang hanya membaca hasil trade historis/paper:

- `walk_forward(rows, train_size, test_size, step_size)`: membagi catatan secara kronologis menjadi jendela train dan test. Statistik test dihitung terpisah; data training tidak digunakan untuk menyetel parameter atau membuat probabilitas.
- `monte_carlo_risk(rows, simulations, starting_equity, risk_fraction, seed)`: bootstrap hasil R-multiple dengan pengembalian, lalu melaporkan persentil ekuitas akhir dan drawdown. Seed membuat simulasi dapat direproduksi.

Keterbatasan: walk-forward ini adalah splitter/evaluator, bukan optimizer atau validasi strategi end-to-end. Bootstrap mengasumsikan distribusi hasil relatif tetap dan trade dapat diacak; asumsi tersebut dapat gagal pada data berurutan, perubahan rezim, biaya berubah, atau korelasi antar-posisi. Hasil simulasi bukan prediksi dan bukan rekomendasi ukuran risiko.

## Korelasi dan stress test portofolio (offline)

`portfolio.py` menyediakan `correlation_matrix` untuk return series yang sudah sejajar berdasarkan timestamp dan `stress_test` untuk menerapkan shock deterministik ke bobot portofolio. Seri yang tidak sejajar ditolak; korelasi yang tidak terdefinisi dikembalikan sebagai `null`. Shock hanya menghasilkan estimasi sensitivitas; bukan ramalan, VaR, atau pengganti pengujian historis.
