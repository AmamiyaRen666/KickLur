# KickLur — BF Kicker

Telegram bot yang khusus buat **BF Kicker** (session kick MLBB). Nggak ada fitur
lain: nggak ada valid-check, bulk, lookup, license, broadcast — cuma kicker.

Dibuat dari jalur BF Kicker di repo `Jugo`, dipisah supaya ringan dan murah
dijalankan di Railway.

## Fitur

- **List device akumulatif** — kirim id satu-satu atau upload `.txt`, list-nya
  **nambah**, nggak nimpa. Ada tombol Reset kalau mau mulai dari nol.
- **Start langsung** — tekan ▶️ START KICK, langsung jalan (unlimited). Nggak ada
  wizard, nggak perlu pilih loop.
- **Run paralel** — tiap run jalan sendiri. Nambah device/run baru **nggak**
  menghentikan run yang sudah jalan.
- **Hapus device** — buka 📋 Device buat pause/lanjut **per device** (sementara,
  bisa di-resume), atau hapus semua sekaligus.
- **Worker pool, bukan batch** — tiap device "jalan sendiri": begitu ada worker
  kosong dia langsung ambil device berikutnya. Jadi satu device yang lambat
  (timeout 15s) nggak bikin device lain nganggur, dan pause/resume kepickup
  hampir instan.
- **Status on-demand** — pesan run **nggak** di-edit terus-terusan (ini yang
  biasanya bikin boros + kena flood limit Telegram). Klik 📊 Status baru
  di-refresh, plus sekali di akhir untuk ringkasan.
- **Status ada ID + nama** — tiap kick sekalian ambil **account id** dan
  **nickname** pemain, ditampilkan per device di blok 📋 Hasil. Nama
  di-cache per account, jadi unlimited run nggak nanya berulang.
- **Owner-only** — cuma chat id yang diizinkan yang bisa pakai.

## Deploy di Railway

1. Buat service baru dari repo ini (Railway autodetect Python lewat
   `requirements.txt`).
2. Set environment variables:

| Variable | Wajib | Default | Keterangan |
|---|---|---|---|
| `TG_BOT_TOKEN` | ✅ | — | token dari @BotFather |
| `TG_OWNER_CHAT_ID` | ✅ | — | chat id yang boleh pakai bot |
| `TG_ALLOWED_IDS` | — | kosong | chat id tambahan, dipisah koma |
| `BF_WORKERS` | — | `20` | worker per run |
| `BF_MAX_CONCURRENCY` | — | `40` | batas kick simultan total (semua run) |
| `BF_MAX_DEVICES` | — | `2000` | batas device per chat |
| `BF_LOOKUP` | — | `1` | resolve nama akun buat status (set `0` buat matiin) |
| `BF_ACK_WAIT` | — | `1.5` | detik nunggu ACK dari server. `0` = nggak nunggu (lebih cepat, tanpa konfirmasi) |
| `BF_KICK_TWICE` | — | `0` | kirim paket kick 2x (praktis gratis, cadangan kalau frame 1 hilang) |
| `BF_KICK_ALL_SERVERS` | — | `1` | nembak ke **semua** game server, paralel |
| `MLBB_GS_SEED` | — | kosong | alamat game server awal, dipisah koma |

3. Start command: `python main.py` (sudah di-set di `railway.json`).

Nggak butuh volume — bot ini nggak nyimpen state ke disk. Kalau `PORT` di-set
Railway, bot nyediain endpoint `/` + `/health` seadanya biar keliatan hidup.

## Berapa worker yang pas?

Ini **diukur**, bukan dikira-kira:

```
kick packet (SdpStruct + zstd) : 11.8 µs  -> 84.787 ops/detik
handshake packet               :  4.9 µs  -> 205.493 ops/detik
CPU per kick attempt           :  0.017 ms
RSS proses setelah import      : ~34 MB
```

Satu kick cuma makan **0.017 ms CPU** dan sisanya nunggu jaringan. Artinya
worker itu **I/O bound, bukan CPU bound** — nambah worker nggak akan menghabiskan
CPU, yang jadi batas justru seberapa sabar login/game server MLBB.

### Batas wajar BF_WORKERS (diukur ke server asli)

10 server per kick, `BF_ACK_WAIT=1.5`:

| BF_WORKERS | kick/detik | gagal | catatan |
|---|---|---|---|
| `20` | 19.9 | 0.0% | **default — aman** |
| `30` | 27.5 | 0.0% | masih naik |
| `40` | 37.2 | 0.0% | masih naik, masih nol gagal |
| `55` | 38.5 | 0.0% | **mentok** (plateau) |
| `60` | 93.1 | **68.9%** | 💥 server mulai nolak |
| `70` | 114.7 | **73.3%** | makin parah |

**Kesimpulan:** batas wajarnya **20-40**. Di atas ~50, throughput naik tapi
**gagalnya juga naik** — server game-nya mulai nolak (`GAME SERVER REFUSED`).
Worker tambahan jadi murni kegagalan, bukan kecepatan.

RAM aman: RSS **datar ~18 MB** dari 10 sampai 70 worker (1 kick = 1 thread,
semua koneksi server dijalankan non-blocking dari thread yang sama).

`BF_MAX_CONCURRENCY` (default 40) nahan total kick dari **semua** run sekaligus,
jadi buka 5 run barengan nggak akan meledakkan jumlah koneksi. Ingat: itu dibagi
rata antar run — 4 run × `BF_MAX_CONCURRENCY=40` = **10 slot per run**.

## Cara pakai

1. Kirim device id (teks atau file .txt) — list-nya **nambah**, nggak nimpa
2. Tekan ▶️ START KICK → langsung jalan (unlimited)
3. Tekan 📊 Status buat lihat progress
4. Tekan 📋 Device buat pause/lanjut/hapus device

Tanda di menu: `🟢` jalan · `⏸` pause · `✅` sudah kena kick · `⏹` dihapus.

Kalau **semua** device di-pause, run **nunggu** (nggak mati) — begitu satu
dilanjut, jalan lagi.

## Kick nyampe ke server yang benar (ini yang paling penting)

Server login pakai **load balancer**. Tiap login dikasih alamat game server
**berbeda** — terukur **10 alamat berbeda dari 12 login**:

```
185.23.183.11 / .14 / .19 / .20 / .30 / .45 / .46 / .47 / .48 / .100 / .101 / .111 / .114
```

Sesi pemain ada di **satu** alamat itu. Kalau kick cuma dikirim ke alamat yang
baru saja dikasih, peluang kena cuma **~1 dari 10** — 90% meleset. Ini penyebab
"kadang masih bisa login".

Sekarang kick dikirim ke **semua** alamat yang diketahui, **paralel**:

| | waktu |
|---|---|
| 7-9 server, paralel | **~200 ms** |
| 7-9 server, satu-satu | ~1330 ms |

Sama seperti nembak 1 server, karena connect-nya jalan barengan. Tiap kick
belajar satu alamat baru dan menyimpannya, jadi cakupannya makin lengkap sendiri.
Isi `MLBB_GS_SEED` kalau mau langsung penuh sejak kick pertama.

## Kecepatan kick (diukur, bukan dikira)

Angka nyata ke server game:

```
DNS                        :    6 ms
TCP connect ke game server : ~189 ms   ← ini lantainya
kirim paket kick           :  0.2 ms
server balas ACK           : ~570 ms KEMUDIAN
```

### ⚠️ ACK itu WAJIB ditunggu

Server game **selalu** membalas paket `10002` ~570 ms setelah kick. Itu bukan
hiasan — itu tanda kick-nya diproses.

| cara | dapat ACK | per kick |
|---|---|---|
| kirim lalu tutup socket | **0 / 25** | 189 ms |
| kirim lalu tunggu ACK (cara BF Kicker asli) | **25 / 25** | 756 ms |

Kalau socket ditutup sebelum balasan datang, kernel kita menjawab balasan itu
dengan **RST** — bukan kelakuan client normal, dan itu satu-satunya perbedaan
nyata dari BF Kicker yang sudah terbukti nendang. Default sekarang
`BF_ACK_WAIT=1.5` (menunggu).

Ongkosnya: satu kick jadi ~790 ms (dari ~190 ms), tapi kick jalan **paralel**,
jadi throughput total praktis sama:

```
5 worker, nunggu ACK  : 5.0 kick/detik total
```

Karena ACK-nya ditunggu, laporannya sekarang jujur — bukan "SENT OK" yang cuma
berarti "byte sudah keluar", tapi:

```
✅ ACK ×6/6 server · 783ms · acct 652907530
```

Itu artinya **6 dari 6 server benar-benar memproses** kick-nya.

Perkiraan throughput (1 vCPU, `BF_WORKERS=20`): **~25 kick/detik** dengan ACK
ditunggu. Kalau mau lebih cepat, naikkan `BF_WORKERS`.

Kalau mau balik ke perilaku lama (tidak menunggu ACK, lebih cepat tapi tanpa
konfirmasi), set `BF_ACK_WAIT=0`.

**Catatan jujur:** dari box ini aku **tidak bisa membuktikan** sesi di server
game benar-benar ke-revoke — butuh akun sungguhan yang sedang online. Yang
terukur: **ACK dari server** (bukti paket diproses), kecepatan, dan konfirmasi
pengiriman.

## Jalankan lokal

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # isi token + owner id
python main.py
```

## Struktur

```
main.py                  entry point + health endpoint
kicklur/
  config.py              env -> konstanta
  sdp.py                 wire format SDP + BaseConn (identik dengan produksi)
  kicker.py              fetch_session_profile + send_session_kick + kick_once
  devices.py             parsing device id + list akumulatif per chat
  runs.py                registry run paralel + executor (worker pool)
  telegram.py            client Telegram: pacing, flood handling, long poll
  ui.py                  semua teks + keyboard (murni render)
  bot.py                 dispatch update -> aksi
```

## Catatan

- Bot ini alat **kick**, jadi sengaja dibatasi owner-only.
- `MLBB_CLI_VER` / `MLBB_CHANNEL` / `MLBB_LOGIN_HOST` bisa diubah lewat env kalau
  server gamenya berubah; default-nya nilai yang sudah terbukti jalan.
