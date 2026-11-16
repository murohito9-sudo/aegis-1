# AEGIS-1 — Adaptive Execution & Guarded Investment System

Sistem trading modular: Python untuk data, indikator, sinyal, risiko, eksekusi,
dan audit; satu file HTML untuk console pemantauan real-time.

Paket ini berjalan sebagai **mesin eksekusi paper** (simulasi) dengan model biaya
eksplisit — fee maker/taker, slippage, spread. Seluruh guardrail risiko, audit
log, dan dashboard berfungsi penuh. **Tidak ada kode di paket ini yang mengirim
order nyata ke exchange.** Alasannya, dan apa yang harus dibangun untuk live,
ada di bagian [Batas paket](#batas-paket-dan-langkah-menuju-live).

> Isi berkas ini bukan nasihat investasi. Tidak ada janji profit di mana pun.
> Angka pada dashboard adalah hasil simulasi.

**Situs (GitHub Pages):** <https://murohito9-sudo.github.io/aegis-1/>

| Halaman | Isi |
| --- | --- |
| [index](https://murohito9-sudo.github.io/aegis-1/) | pemilih mode: demo atau live |
| [demo.html](https://murohito9-sudo.github.io/aegis-1/demo.html) | console dengan data contoh tertanam + validator strategi JSON |
| [live.html](https://murohito9-sudo.github.io/aegis-1/live.html) | langkah menjalankan bot sungguhan (lokal/VPS) |

Mode demo tidak memakai backend, tidak mengirim order, dan tidak membuka
koneksi jaringan — seluruh angkanya simulasi. Console yang benar-benar hidup
tetap dijalankan lokal: `python main.py` lalu buka `http://127.0.0.1:8787`
(lihat `live.html`).

**Strategi JSON.** Halaman demo menerima berkas strategi dari pengguna
(parameter indikator, filter, bobot konfluensi, setelan risiko) dan
memeriksanya di browser: rentang nilai, aturan silang seperti `ema_fast <
ema_slow`, total bobot = 1, dan kunci tak dikenal. Hasilnya juga berupa potongan
`.env` siap tempel untuk parameter yang punya variabel lingkungan
(`AEGIS_EMA_FAST`, `AEGIS_MIN_CONFLUENCE`, `AEGIS_RISK_PER_TRADE`, dst).

Setelah lolos validasi, strategi itu masuk pustaka dan bisa dipakai tiga cara
di halaman demo: **dipasang sebagai strategi aktif** (ukuran posisi, aksi sinyal
di tabel riwayat, dan chip `STRATEGI` di header console ikut berubah),
**dibandingkan berdampingan** dengan strategi lain (jumlah sinyal lolos, yang
ditolak skor atau reward:risk, ukuran posisi risk-based vs setelah batas
exposure, plus tabel parameter berbeda), dan **diekspor** sebagai ringkasan
Markdown. Tersedia tiga preset bawaan sebagai pembanding.

Halaman statis hanya memvalidasi, menghitung ulang, dan menampilkan — bot tidak
berubah karena unggahan itu; parameter tanpa env var harus diubah di
`config.py`.

---

## 1. Struktur folder

```
aegis1/
├── config.py            # parameter strategi, risiko, eksekusi (env: prefix AEGIS_)
├── data.py              # feed OHLCV (ccxt publik / CSV / sintetis) + gate kualitas data
├── indicators.py        # EMA, RSI Wilder, MACD, ATR, ROC, relative volume, swing, S/R, BOS
├── strategy.py          # regime detection, skor konfluensi, sinyal + alasan & veto
├── risk.py              # position sizing, SL/TP, trailing, partial TP, batas harian, drawdown
├── execution.py         # PaperBroker (fee, slippage, timeout, dedupe) + adapter live yang menolak
├── logger.py            # audit log SQLite + JSONL, alert lokal
├── dashboard.py         # backend FastAPI: state, history, aksi dua langkah, WebSocket
├── main.py              # loop utama, manajemen posisi, exception handling, graceful shutdown
├── tests.py             # 48 unit test (sizing, limit risiko, sinyal, gate, eksekusi)
├── static/
│   └── index.html       # "AEGIS-1 Trading Console" — satu file, tanpa CDN
├── deploy/
│   └── aegis1.service   # unit systemd untuk 24/7 + restart otomatis
├── data/                # SQLite, JSONL, kill-switch (dibuat saat runtime)
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 2. Instalasi

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # opsional; default sudah aman
python tests.py                                       # 48 test harus OK
```

Python 3.11+ (diuji pada 3.12). Tidak ada dependensi berbayar.

## 3. Menjalankan

```bash
# Demo tanpa jaringan (data sintetis deterministik, bagus untuk uji guardrail)
python main.py --offline --interval 0.5

# Data publik dari exchange lewat ccxt (read-only, tanpa API key)
python main.py

# Dari candle milik sendiri (kolom: ts,open,high,low,close,volume)
python main.py --csv data/BTCUSDT_15m.csv

# Tanpa dashboard, berhenti setelah N siklus (untuk CI)
python main.py --offline --ticks 50 --no-dashboard
```

Lalu buka **http://127.0.0.1:8787**. Dashboard hanya mengikat ke `127.0.0.1`
secara default; untuk akses jarak jauh pakai SSH tunnel
(`ssh -L 8787:127.0.0.1:8787 user@vps`), jangan membuka port ke publik tanpa
reverse proxy + autentikasi.

Nilai `[ISI: …]` pada brief belum diisi, jadi default yang dipakai: **crypto,
Binance, BTC/USDT, 15m, 10.000 USDT**. Semua bisa diubah di `.env` tanpa
menyentuh kode.

## 4. Konfigurasi

Semua parameter dibaca dari environment dengan prefix `AEGIS_`
(lihat `.env.example`). Yang paling sering diubah:

| Variabel | Default | Arti |
|---|---|---|
| `AEGIS_SYMBOL` | `BTC/USDT` | pair |
| `AEGIS_TIMEFRAME` | `15m` | timeframe candle |
| `AEGIS_EXCHANGE` | `binance` | id exchange ccxt (data publik) |
| `AEGIS_INITIAL_EQUITY` | `10000` | modal awal simulasi |
| `AEGIS_RISK_PER_TRADE` | `0.01` | 1% equity per trade |
| `AEGIS_MAX_TOTAL_EXPOSURE` | `0.20` | 20% equity |
| `AEGIS_MAX_DAILY_LOSS` | `0.03` | stop entry saat rugi harian 3% |
| `AEGIS_MAX_DRAWDOWN` | `0.10` | mode SAFE saat drawdown 10% |
| `AEGIS_MIN_CONFLUENCE` | `0.70` | ambang skor sinyal (0–1) |
| `AEGIS_OFFLINE` | `false` | `true` = data sintetis |

Kredensial: `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`,
`EXCHANGE_API_PASSPHRASE`. Paket ini hanya membaca **status terisi/kosong**
(`config.credential_presence()`), tidak pernah nilainya. Tidak ada key di kode,
di `.env.example`, di database, di log, atau di browser.

---

## 5. Cara kerja strategi

Satu baris: **enam konfirmasi harus searah, kalau tidak, tidak ada trade.**

### 5.1 Trend filter
Harga di atas EMA50 **dan** EMA50 di atas EMA200 (atau breakout terkonfirmasi:
close menembus swing high terakhir dengan harga di atas EMA50). Kebalikannya
untuk short. Melawan tren utama diizinkan hanya bila ambang konfluensi dinaikkan
+0,10.

### 5.2 Momentum
RSI(14) metode Wilder: long butuh 45 ≤ RSI ≤ 78. Batas atas 78 penting — RSI 90
berarti harga sudah jauh dari basis, dan mengejar di situ adalah cara klasik
kalah. MACD(12,26,9) harus searah (line > signal dan histogram > 0 untuk long).
ROC(10) harus searah.

### 5.3 Volatilitas
ATR(14) Wilder dipakai tiga kali: menentukan jarak stop (1,8 ATR), menentukan
trailing (1,5 ATR), dan mengklasifikasi regime (persentil ATR ≥ 90 → volatilitas
tinggi, ambang konfluensi +0,10).

### 5.4 Struktur pasar
Swing high/low terkonfirmasi (fraktal 5 kiri–5 kanan). Bar terakhir **tidak
pernah** dianggap swing karena butuh konfirmasi di kanan — ini yang mencegah
look-ahead. Dari swing itu: level support/resistance terdekat, status break of
structure (BOS), dan jarak bar sejak BOS.

### 5.5 Volume
Volume relatif terhadap rata-rata 20 bar **sebelumnya** (bukan termasuk bar
berjalan). Minimum 1,0× untuk entry. Di bawah 0,55× regime dianggap likuiditas
rendah dan bot berhenti.

### 5.6 Regime
`trending_up`, `trending_down`, `ranging`, `high_volatility`, `low_liquidity`.
Aturannya: `low_liquidity` → tidak ada trade sama sekali; `ranging` → butuh BOS;
`high_volatility` → ambang konfluensi naik.

### 5.7 Skor konfluensi
Bobot: trend 0,25 · struktur 0,20 · momentum 0,15 · MACD 0,15 · volume 0,15 ·
ROC 0,10. Entry butuh skor ≥ 0,70 (0,80 di volatilitas tinggi atau saat melawan
tren) **dan** trend + MACD + volume tidak boleh gagal. Tidak ada sinyal tunggal
yang bisa memicu entry.

### 5.8 Stop, target, reward:risk
Stop = yang lebih jauh antara `entry − 1,8 ATR` dan `swing low − 0,25 ATR`
(short: kebalikannya). Kalau swing terlalu jauh (> 1,8× batas ATR), stop memakai
ATR dan itu dicatat di alasan sinyal. Target = resistance/support struktural bila
jaraknya ≥ 2R; kalau resistance lebih dekat dari 2R, setup **ditolak** — itu
overhead supply. Target dibatasi maksimum 4R.

Setiap sinyal menyimpan `reasons` (kenapa layak) dan `vetoes` (kenapa ditolak),
keduanya masuk audit log. Contoh veto yang akan Anda lihat di log:
`RSI 91.7 ekstrem/tidak mendukung arah LONG`, `support 45663.41 terlalu dekat (<2.0R)`,
`skor konfluensi 0.70 < ambang 0.80`.

---

## 6. Guardrail risiko

### 6.1 Batas keras
| Guardrail | Nilai default |
|---|---|
| Risiko per trade | 1% equity |
| Posisi terbuka maksimum | 3 |
| Total exposure maksimum | 20% equity |
| Rugi harian maksimum | 3% → mode SAFE, entry berhenti sampai hari berikutnya |
| Drawdown maksimum | 10% → mode SAFE, butuh konfirmasi manual |
| Leverage maksimum | 1× |
| Reward:risk minimum | 2,0 |
| Spread maksimum | 8 bps |
| Cooldown setelah loss | 3 bar |
| Order per bar | 1 (deduplikasi per `signal_id`) |

### 6.2 Position sizing
```
position_size = (equity × risk_percent) / |entry − stop|
```
lalu dibatasi exposure/leverage, dibatasi porsi slot posisi, dan **selalu
dibulatkan ke bawah** ke `step_size` (`round_down_to_step`, memakai `Decimal`,
tidak pernah membulatkan ke atas). Ditolak bila di bawah `min_qty` atau
`min_notional`.

> **Catatan jujur dari hasil uji.** Dengan stop 1,8 ATR (jarak 1–2%) dan exposure
> maksimum 20%, batas exposure-lah yang mengikat lebih dulu, bukan 1%. Artinya
> risiko efektif per trade mendarat di sekitar 0,2–0,3%, bukan 1%. Ini bukan bug:
> sistem mengambil batas yang paling konservatif. Bila Anda memang ingin 1%
> benar-benar terpakai, naikkan `AEGIS_MAX_TOTAL_EXPOSURE` atau perkecil
> pengali ATR — dan sadari bahwa keduanya menaikkan risiko.

### 6.3 Checklist pra-trade 16 poin
Order hanya dikirim bila **semua** lolos. Satu gagal = order tidak dikirim, dan
alasannya masuk log (`pretrade_blocked`).

1. sinyal valid · 2. stop terdefinisi · 3. reward:risk ≥ 2 · 4. ukuran posisi
valid · 5. batas rugi harian belum tercapai · 6. kill-switch/pause tidak aktif ·
7. spread & likuiditas wajar · 8. slot posisi tersedia · 9. total exposure
setelah order masih ≤ 20% · 10. drawdown < 10% · 11. gate kualitas data lolos ·
12. tidak ada order pending · 13. bar ini belum dipakai untuk order ·
14. cooldown pasca-loss terlewati · 15. tidak ada blokir event volatilitas ·
16. izin live sesuai konfigurasi.

### 6.4 Gate kualitas data (fail-closed)
Sebelas pemeriksaan: jumlah bar ≥ 260, tanpa sel kosong, high/low/close
konsisten, timestamp unik dan naik monoton, ≤ 2% bar bervolume nol, bar terakhir
tidak lebih tua dari 3× timeframe, tanpa lompatan harga > 25%, spread ≤ 8 bps,
kuotasi bid < ask. Gagal satu → tidak ada sinyal yang diproses sama sekali.

### 6.5 Manajemen posisi
- **SL dan TP dikirim ke broker segera setelah fill entry** (`STOP_MARKET` dan
  `TAKE_PROFIT_MARKET`), bukan hanya dipantau di memori.
- Partial take profit 50% pada 1R dan pada 2R; qty SL/TP ikut disesuaikan agar
  tidak melebihi sisa posisi.
- Trailing stop aktif setelah 1R, jarak 1,5 ATR, hanya bergerak ke arah profit.
- Jaring pengaman: bila harga sudah melewati level SL/TP tetapi order tidak ada
  atau tidak terisi, posisi ditutup eksplisit (`stop_loss_safety` /
  `take_profit_safety`).
- Exit karena trailing dicatat sebagai `trailing_stop`, bukan `stop_loss`, supaya
  statistik R tidak menyesatkan.

---

## 7. Eksekusi dan biaya (paper)

`PaperBroker` memodelkan hal-hal yang biasanya diabaikan backtest naif:

| Hal | Model |
|---|---|
| Fee maker | 0,02% |
| Fee taker | 0,05% (limit yang menyeberang book dihitung taker) |
| Slippage market/stop | 1,5 bps di atas bid/ask |
| Fill limit | hanya bila harga menyentuh limit |
| Stop/TP | terisi saat harga melewati level, lalu market + slippage |
| Timeout limit | 20 detik → `EXPIRED` |
| Order ganda | `signal_id` yang sama ditolak (`OrderRejected`) |
| Partial fill | didukung (status `PARTIALLY_FILLED`) |
| Rekonsiliasi | setiap order/fill ditulis ke SQLite + JSONL, termasuk `exchange_id`, harga eksekusi, fee, slippage, timestamp |

## 8. Audit dan rekonstruksi

Tujuh tabel: `signals`, `orders`, `fills`, `positions`, `equity`, `events`,
`actions`. Setiap siklus menulis snapshot ekuitas; setiap sinyal menulis alasan
dan veto; setiap order menulis state lengkap sebelum dan sesudah. Jejak yang sama
ditulis append-only ke `data/aegis1.jsonl`. Untuk merekonstruksi satu keputusan:
cari `signal_id` di `signals`, lihat `checks` di event `order_summary`, lalu
`orders` → `fills` → `positions`.

```bash
sqlite3 data/aegis1.sqlite3 "select ts,kind,message from events order by id desc limit 20;"
```

## 9. Dashboard

Satu file `static/index.html` (tanpa CDN, tanpa framework, tanpa
localStorage/sessionStorage). Isi: status koneksi, mode (PAPER/SAFE/PAUSED/
KILL-SWITCH), equity, available balance, unrealized & realized PnL, win rate,
profit factor, average R, max drawdown, tabel posisi terbuka (entry, size, SL, TP,
PnL, R, age, trailing), panel risiko (risk/trade, rugi harian, exposure,
drawdown, slot), chart harga dengan EMA50/EMA200 dan marker entry/exit, riwayat
order/trade/sinyal, dan log eksekusi.

Tombol: **PAUSE TRADING**, **RESUME TRADING**, **CLOSE ALL POSITIONS**,
**EMERGENCY KILL-SWITCH**. Semuanya dua langkah: klik → ringkasan dampak →
ketik frasa konfirmasi (`PAUSE AEGIS-1`, `TUTUP SEMUA POSISI`,
`AKTIFKAN KILL SWITCH`, …). Token konfirmasi berlaku 120 detik, sekali pakai, dan
hangus bila frasa salah.

Endpoint: `GET /`, `GET /api/state`, `GET /api/history`, `GET /healthz`,
`WS /ws?csrf=…`, `POST /api/actions/{pause|resume|close_all|kill}/request`,
`POST /api/actions/{action}/confirm`. Setiap request butuh header `X-AEGIS-CSRF`
dari `<meta name="aegis-csrf">`; WebSocket tanpa token ditolak (kode 4403).

Kill-switch melakukan tiga hal sekaligus: menghentikan entry, membatalkan semua
order pending, menutup semua posisi — lalu menulis file `data/KILL_SWITCH`.
Selama file itu ada, bot tidak kembali sendiri; hapus file dan restart secara
sadar. Resume saat mode KILL ditolak dengan HTTP 409.

## 10. Uji

```bash
python tests.py            # 48 test
pytest tests.py -q         # sama, lewat pytest
```

Yang diuji antara lain: sizing 1% benar-benar 1% saat batas tidak mengikat,
pembulatan selalu ke bawah, exposure/leverage/slot membatasi, batas rugi harian
dan drawdown termasuk unrealized, 16 checklist menolak pada tiap kondisi tidak
aman (kill, pause, spread lebar, gate gagal, order pending, bar duplikat,
cooldown), sinyal menolak data kurang dan RSI ekstrem, sinyal actionable selalu
RR ≥ 2 dan stop di sisi yang benar, gate menolak data basi/duplikat/spread lebar,
eksekusi menolak order ganda dan qty di bawah minimum, stop terpicu saat harga
melewati level, limit order kedaluwarsa, dan `LIVE_MODE=true` **ditolak**.

## 11. Berjalan 24/7 di VPS

```bash
sudo useradd -r -s /bin/false aegis
sudo mkdir -p /opt/aegis1 && sudo chown aegis:aegis /opt/aegis1
# salin proyek, buat venv, install requirements
sudo cp deploy/aegis1.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aegis1
sudo journalctl -u aegis1 -f
```

`Restart=always` + `RestartSec=5` menangani crash; `KillSignal=SIGTERM` memicu
shutdown rapi (loop berhenti, state terakhir tersimpan). Jangan menjalankan
proses dengan `&`/`nohup` di luar supervisor — tidak ada yang me-restart-nya.

## 12. Alert

Saat ini alert ditulis ke audit log dan stdout: startup, shutdown, entry, exit,
mode change, drawdown, batas rugi harian, kill-switch, error siklus. Titik
sambungnya jelas: `Alerts.subscribe(fn)` di `logger.py`. Kanal eksternal
(Telegram/Discord/email) sengaja tidak disertakan — itu di luar cakupan paket ini
dan butuh token bot yang tidak boleh disimpan di repo. Bila Anda menambahkannya,
simpan token di environment/secret manager, bukan di kode.

---

## Batas paket dan langkah menuju live

Bagian ini yang paling penting untuk dibaca.

**Yang ada di paket ini.** Seluruh logika sinyal, risiko, sizing, manajemen
posisi, model biaya, audit, dashboard, kill-switch, dan 48 test. Mesin berjalan
penuh dan bisa dipakai untuk mengukur perilaku strategi atas data apa pun.

**Yang tidak ada, dengan sengaja.**

1. **Tidak ada kode pengirim order nyata.** `execution.LiveBrokerAdapter`
   melempar `LiveBrokerUnavailable` di konstruktor, dan `build_broker()` menolak
   `LIVE_MODE=true` sebelum menyentuh jaringan. Ini bukan tombol yang lupa diisi:
   ini keputusan. Kode yang mengirim order pasar dengan uang sungguhan tidak
   dibuat di sini.
2. **Tidak ada kanal alert eksternal** (Telegram/Discord/email) — lihat §12.
3. **Tidak ada jaminan profit, dan tidak ada klaim profit.**

**Kontrak yang harus dipenuhi bila Anda membangun adapter live sendiri.**
`execution.Broker` adalah protokolnya: `balance()`, `place()`, `cancel()`,
`modify()`, `open_orders()`, `all_orders()`, `on_price()`. Untuk layak dipakai
dengan uang nyata, minimal:

- baca kredensial dari environment saja, rotasi key, izin trade-only tanpa
  penarikan;
- idempotensi berbasis `client_id` dan `signal_id` (paket ini sudah menolak
  duplikat; adapter live harus memakai `newClientOrderId` exchange, bukan
  mengandalkan ingatan proses);
- rekonsiliasi posisi nyata terhadap database saat start — jangan berasumsi
  database adalah kebenaran;
- tangani rate limit (`429`), timeout, `5xx`, reconnect WebSocket, partial fill,
  dan posisi yang sudah tertutup di exchange tetapi masih terbuka di memori;
- verifikasi bahwa SL/TP benar-benar terpasang di exchange sebelum menganggap
  posisi terlindungi; bila gagal, tutup posisi dan kirim alert;
- uji di testnet exchange selama minimal beberapa minggu, lalu modal minimum
  yang Anda siap kehilangan seluruhnya;
- jangan melewati 16 checklist pra-trade dan gate kualitas data — itu bagian dari
  kontrak, bukan hiasan.

**Langkah aman sebelum uang sungguhan.**

1. `python tests.py` — semua hijau.
2. `python main.py --offline` beberapa jam; periksa `events`, `orders`,
   `positions` di SQLite. Pastikan tidak ada order tanpa SL.
3. Ulangi dengan `--csv` data nyata beberapa bulan, termasuk periode crash.
4. Baca distribusi R dan `close_reason`; kalau mayoritas exit adalah `stop_loss`,
   masalahnya di entry atau di stop, bukan di target.
5. Periksa biaya: fee + slippage pada log sering lebih besar dari dugaan.
6. Baru setelah itu pertimbangkan adapter live, dengan batas risiko yang sama
   atau lebih ketat, dan modal yang siap hilang.

---

## Lisensi dan tanggung jawab

Kode open-source untuk dipelajari dan diubah. Trading membawa risiko kehilangan
modal. Tidak ada bagian dari paket ini yang merupakan nasihat keuangan.
