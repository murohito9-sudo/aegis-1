# Experiment

Satu experiment = satu eksekusi dengan parameter yang dibekukan. Tanpa catatan ini, pertanyaan "kenapa hasilnya berbeda?" tidak bisa dijawab.

```
EXP-NNN
- Hipotesis: HYP-NNN
- Strategy version: STRATEGY-NNN vX.Y.Z
- Dataset version: DATASET-NNN
- Code commit: <sha>
- Parameter: <key=value, ...>
- Periode: <YYYY-MM-DD s/d YYYY-MM-DD>
- Market / timeframe: <...>
- Biaya transaksi: <bps atau persen>
- Asumsi slippage: <...>
- Konfigurasi risiko: <...>
- Hasil: <metrik utama, bukan hanya profit>
- Status: Valid / Invalid (alasan) / Unverified
```

## Aturan

- Hasil lama tidak ditimpa. Experiment baru = nomor baru.
- Kalau terdeteksi look-ahead atau leakage: tandai `BACKTEST INVALID` dan jangan dipakai sebagai bukti.
- Sebelum laporan final, jalankan ulang experiment bila memungkinkan. Hasil berbeda → `REPRODUCIBILITY FAILURE` dan investigasi.
