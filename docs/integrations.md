# Integrasi — aegis-one

Status koneksi yang benar-benar terverifikasi. Diperbarui 2026-11-18.

| Platform | Status | Kemampuan | Yang tidak bisa dilakukan |
|---|---|---|---|
| GitHub | Connected | repo, file, branch, commit, PR, Actions | — |
| Supabase | Connected, database tidak aktif | project terbaca; `list_tables` gagal karena project INACTIVE | eksekusi SQL, migrasi, verifikasi RLS |
| Penyedia data market | Unsupported | — | menarik OHLCV/on-chain dari sisi agent; `ccxt` hanya berjalan di dalam kode repo |
| Runner test di sesi agent | Not Yet Verified | — | menjalankan `tests.py` dari sisi agent |
| Sentry / Datadog / SigNoz | Not Connected | — | observability terpusat |

## Aturan

- Jangan menyebut platform sebagai terhubung sebelum lolos connection test **dan** functional test.
- Kalau sebuah langkah butuh platform berstatus Unsupported, hentikan dan tulis langkah manualnya.
- Perbarui tabel ini setiap kali status berubah, dengan tanggal.
