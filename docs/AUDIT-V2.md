# AEGIS-ONE V2 — Audit & Baseline (Phase 1–2)

**Tanggal audit:** 19 November 2026
**Commit yang diaudit:** `d1251c9` — *README: explain live-data demo, warm-up and reset* (branch `main`)
**Metode:** clone bersih dari GitHub, kompilasi seluruh modul, menjalankan seluruh test, pembacaan kode baris demi baris untuk setiap klaim fitur.
**Sifat dokumen:** hasil audit. Tidak ada kode yang diubah pada tahap ini, sesuai PART 14 Phase 1.

## Ringkasan

Repository berada dalam kondisi sehat dan jujur secara teknis: seluruh modul terkompilasi dan baseline 122 test diuji pada commit audit. Audit menemukan empat temuan material:

1. DEMO dan LIVE belum terisolasi di lapisan data.
2. Scheduler, backup, dan retensi belum tersambung ke Engine utama.
3. Klaim partial fill belum didukung implementasi PaperBroker.
4. Mesin backtest, walk-forward, Monte Carlo, dan benchmark belum ada.

Urutan perbaikan: tutup temuan keamanan dan operasi, lalu bangun research engine, intelligence, attribution, dan UI institusional.

## Temuan utama

### D.1 DEMO dan LIVE berbagi database

`config.Settings` memiliki satu pasang `db_path` dan `jsonl_path`. `LiveBroker` dan `PaperBroker` dapat menulis ke audit log yang sama. Laporan PnL dapat mencampur simulasi dan eksekusi nyata.

Rencana: gunakan path database terpisah untuk demo/live, tambahkan kolom mode wajib, migrasi lama ke demo secara non-destruktif, dan uji anti-kebocoran.

### D.2 Scheduler dan backup tidak berjalan

`Scheduler`, `backup()`, dan `restore()` ada dan teruji, tetapi tidak dipanggil secara otomatis oleh `main.Engine`.

Rencana: jadwalkan heartbeat 30 detik, health 60 detik, backup 6 jam dengan retensi 7, serta keluarkan status ke audit dan `/api/health`.

### D.3 Partial fill tidak diimplementasikan

`PARTIALLY_FILLED` hanya enum; `PaperBroker.place()` belum mensimulasikan isi parsial.

Rencana pilihan: implementasikan partial fill deterministik, atau turunkan klaim dokumentasi sampai fitur tersedia.

### D.4 Tidak ada mesin backtest

Replay, analytics, dan evaluation sudah ada sebagai komponen, tetapi belum dirangkai menjadi runner backtest dengan split, walk-forward, sweep, Monte Carlo, dan benchmark.

## Status V2

- Isolasi DEMO/LIVE: BELUM ADA
- Backtest/walk-forward/Monte Carlo/benchmark: BELUM ADA
- Prediksi terkalibrasi: BELUM ADA jalur rekam-prediksi
- Derivatif funding/OI: BELUM ADA
- Korelasi dan stress test: BELUM ADA
- Atribusi performa: BELUM ADA
- Strategy registry server-side: PARSIAL
- UI institusional: PARSIAL
- CI GitHub Actions: TERIMPLEMENTASI, belum terverifikasi

## Phase A — wajib pertama

1. Pisahkan database dan state demo/live.
2. Sambungkan scheduler, health check, backup, dan retensi.
3. Terapkan rate limit pada seluruh `/api/*`.
4. Implementasikan atau koreksi klaim partial fill.

Kriteria terima: test lama tetap lulus, ada test isolasi baru, tidak ada endpoint yang hilang.

## Phase B — research engine

Bangun `backtest.py` dengan replay bar tertutup, benchmark, walk-forward, parameter sweep, stabilitas out-of-sample, dan Monte Carlo deterministik.

## Phase C–E

- Prediksi: rekam sinyal sebelum hasil dan tampilkan probabilitas hanya bila sampel cukup.
- Tambahkan provider derivatif dengan status NOT AVAILABLE eksplisit.
- Pindahkan validasi strategi ke registry server.
- Tambahkan atribusi strategi/aset/regime/timeframe/arah dan dampak fee/slippage.
- Tambahkan sidebar, pusat modul, dan katalog definisi metrik tanpa menghapus tab lama.

## Batasan

- LIVE Binance belum dapat diverifikasi dari lingkungan audit tanpa kredensial.
- CI belum memiliki run GitHub Actions yang dapat dijadikan bukti hijau.
- Angka dashboard demo adalah simulasi, bukan hasil trading nyata.
