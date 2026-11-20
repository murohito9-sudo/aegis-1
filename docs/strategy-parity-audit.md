# Audit Parity Strategi Demo JavaScript vs Python Core

Status: audit statis pada branch codex/live-control-room-v4. Dokumen ini memisahkan fakta kode saat ini dari target penyatuan. Belum ada klaim bahwa kedua mesin sudah identik.

## Ringkasan

Demo browser dan Python core masih menghitung sebagian indikator serta keputusan strategi dengan implementasi berbeda. Karena itu, LIVE Operations V4 yang membaca state Python tidak otomatis menghasilkan sinyal yang sama dengan simulator Demo. Jangan membandingkan hasil paper lintas mesin sebelum parity dibuktikan.

## Temuan terverifikasi

| Area | Demo docs/demo.html | Python core | Dampak |
|---|---|---|---|
| EMA | Recursive EMA dengan seed nilai pertama | pandas.Series.ewm(adjust=False) | Kemungkinan sejalan untuk seed/parameter sama; tetap perlu fixture parity. |
| RSI | Wilder smoothing, seed awal rata-rata gain/loss | indicators.rsi | Perlu fixture untuk memverifikasi warm-up dan seed. |
| MACD | EMA 12/26 dan signal EMA 9, seed nilai pertama | Parameter configurable dan pandas EWM | Pastikan parameter, warm-up, dan NaN policy identik. |
| ATR | TR pertama = high-low; seed ATR memakai rata-rata TR indeks 1..period; lalu Wilder | true_range + EWM alpha 1/period dengan min_periods=period | Seed awal berbeda; nilai ATR dan stop dapat berbeda. |
| Relative volume | Volume saat ini / rata-rata lookback candle sebelumnya | volume / volume.shift(1).rolling(...) | Formula tampak sejalan; fallback baseline nol/NaN perlu disamakan. |
| ATR percentile | Seluruh ATR valid; proporsi nilai strictly lebih rendah dari ATR terakhir | Rolling window (default 100) percentile rank | Window dan tie handling berbeda; regime volatility bisa berbeda. |
| Market structure | Swing terbaru; equality diterima; support/resistance dari ekstrem 40 candle | Swing unik terkonfirmasi; support/resistance dari swing terkonfirmasi | Definisi BOS, support/resistance, dan stop struktural berbeda. |
| Regime | Relative volume, ATR percentile, EMA fast/slow dan close | Menambahkan EMA slow slope dan memakai konfigurasi regime | Label regime dapat berbeda. |
| Bobot confluence | Satu set bobot tetap | Bobot dapat berubah menurut regime | Skor dan ambang sinyal dapat berbeda. |
| HTF bias | Mengelompokkan setiap 4 candle dan EMA 20/50 | intelligence.htf_bias | Perlu fixture parity; tidak boleh diasumsikan setara. |
| Pattern | Engulfing, hammer, shooting star lokal | intelligence.candle_pattern | Perlu samakan aturan candle dan edge cases. |
| Stop/target | ATR 1.8x + swing buffer, target dibatasi 4R | Formula Python harus dibandingkan per cabang LONG/SHORT | Level risiko dan hasil paper dapat berbeda. |

## Rencana penyatuan yang aman

1. Tetapkan Python core sebagai sumber kebenaran untuk indikator, regime, struktur, confluence, stop, dan target.
2. Bekukan kontrak input: candle OHLCV tertutup, urutan timestamp naik, timezone/precision, timeframe, dan parameter konfigurasi.
3. Buat fixture OHLCV deterministik yang sama untuk kedua implementasi: uptrend, downtrend, ranging, high volatility, volume rendah, gap, candle datar, NaN/zero, dan data warm-up minimum.
4. Bandingkan setiap kolom indikator dengan toleransi numerik eksplisit; bandingkan keputusan diskret secara exact (regime, BOS, votes, vetoes, side, stop/target).
5. Tambahkan golden tests untuk sinyal dan signal_id; setiap perubahan formula harus memperbarui fixture secara eksplisit.
6. Setelah parity teruji, ubah Demo agar mengonsumsi output core bersama atau port hasil kontrak yang sama. Jangan mempertahankan dua formula aktif yang berbeda tanpa label versi.
7. Pertahankan Demo sebagai simulator browser dan Python sebagai engine lokal. Tetap read-only/locked untuk operasi live; audit parity tidak membuka koneksi akun atau mengeksekusi order.

## Kriteria penerimaan

- Semua fixture memakai input candle identik dan hanya candle yang sudah tutup.
- Nilai indikator sesuai toleransi yang ditetapkan per indikator.
- Regime, confluence, veto, side, entry reference, stop, target, dan reward:risk cocok deterministik.
- Tidak ada look-ahead bias.
- Seluruh tes lama tetap lulus; tidak ada fitur V9/V10/V4 yang dihapus.
- CI mencakup parity tests pada Python 3.11 dan 3.12 serta regression browser Demo/LIVE.

## Batas audit

Ini audit statis, bukan hasil menjalankan fixture lintas runtime. RSI, HTF, candle pattern, dan formula stop/target masih perlu verifikasi numerik terarah sebelum dinyatakan berbeda atau identik. Tidak ada perubahan pada broker, kredensial, atau status LIVE lock.