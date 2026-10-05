# KickLur — BF Kicker

Bot Telegram buat **BF Kicker** (session kick MLBB), hasil port setia dari
`brutetolslhoya.py`.

Tujuannya cuma satu: **nendang sesi yang masih bisa ditembus**. Jadi bot ini
sengaja nggak punya checker, ban check, bulk lookup, license — cuma kicker.

## Bedanya dari Sumire

| | KickLur | Sumire |
|---|---|---|
| Sumber | `brutetolslhoya` | jalur BF Kicker di `Jugo` |
| CLI version | **2.1.97.1232.1** | 2.2.16.1232.1 |
| Alur profil | `login1 → srv → login2 → lookup → skin → bancheck` | `fetch_session_profile` |
| Kick | kirim 10001, timeout **4.5s**, baca balasan | kirim 10001, tunggu ACK, validasi 10002 |
| Cakupan server | **1 alamat** (yang login kasih) | semua alamat yang diketahui |
| Jeda default | 0.5s | 0.5s |

Perbedaan paling penting: **versi CLI-nya beda** (2.1.97 vs 2.2.16). Kalau
dugaanmu "Sumire masih bisa ditembus" itu karena versinya, KickLur inilah
pembandingnya.

## Fitur

- **List device akumulatif** — kirim id satu-satu atau upload `.txt`; list
  nambah, nggak nimpa. Ada Reset.
- **Run paralel** — tiap run punya worker pool sendiri. Nambah run nggak
  menghentikan run yang jalan.
- **Pause per device** — klik device di 📋 Hasil buat pause/lanjut. Device lain
  tetap jalan.
- **Stop 2 level** — ⛔ STOP RUN INI (run itu saja) atau ⛔ STOP SEMUA.
- **Budget global** — `BF_MAX_CONCURRENCY` nahan total kick dari semua run
  sekaligus, biar buka banyak run nggak meledak.
- **Status on-demand** — pesan nggak di-edit terus-terusan (ini yang bikin
  kena flood limit Telegram). Klik 📊 Refresh, plus sekali di akhir.
- **Owner-only**.
- **Nama pemain** di-cache per akun, jadi unlimited run nggak nanya berulang.

## Deploy di Railway

1. Bikin service baru dari repo ini (Railway autodetect Python lewat
   `requirements.txt`).
2. Set environment variables:

| Variable | Wajib | Default | Keterangan |
|---|---|---|---|
| `TG_BOT_TOKEN` | ✅ | — | token dari @BotFather |
| `TG_OWNER_CHAT_ID` | ✅ | — | chat id yang boleh pakai |
| `TG_ALLOWED_IDS` | — | kosong | chat id tambahan, dipisah koma |
| `BF_WORKERS` | — | `20` | worker per run |
| `BF_MAX_CONCURRENCY` | — | `40` | batas kick simultan total |
| `BF_MAX_DEVICES` | — | `2000` | batas device per chat |
| `BF_KICK_DELAY` | — | `0.5` | jeda antar kick (detik) |
| `BF_LOOKUP` | — | `1` | resolve nama pemain |
| `MLBB_OPEN_TIMEOUT` | — | `5` | timeout connect |
| `MLBB_KICK_TIMEOUT` | — | `4.5` | timeout kick (nilai asli) |
| `MLBB_FETCH_ATTEMPTS` | — | `3` | retry ambil profil (nilai asli) |

3. Start command: `python main.py` (sudah di `railway.json`).

Nggak butuh volume — bot nggak nyimpen state ke disk.

## Protokol (dari brutetolslhoya, TIDAK diubah)

```
Login  : login.ml.youngjoygame.com:30021
CLI    : 2.1.97.1232.1
Channel: and_usa
Lang   : en
```

Alur satu kick:

```
1. login server   : paket 1  -> 2      (device id -> acc, skey, zid)
2. game server    : paket 5  -> 6      ("host:port")
3. masuk game     : 10001    -> 10002
4. nama pemain    : 11153    -> 11154
5. skin           : 10143    -> 10144
6. bancheck       : 10101
7. KICK           : 10001 ke game server, timeout 4.5s
```

Paket kick:
```
SdpStruct({0: acc, 1: skey, 2: zid, 4: VER, 13: CHAN, 15: device_id})
dibungkus {0: 10001, 1: 1, 5: body}, zstd, header (len+4)|(16<<24)
```

## Catatan penting

- **Lookup nama itu opsional.** Satu paket 11153 per device, tapi hasilnya
  di-cache per akun, jadi unlimited run nggak nambah beban. Set `BF_LOOKUP=0`
  kalau mau matiin.
- **Jeda bukan hiasan.** Kirim back-to-back bikin game server balas
  `GAME SERVER REFUSED`. Default 0.5s.
- **Kick cuma 1 alamat** — yang dikasih login server. Ini sama seperti sumber
  dan seperti BF Kicker asli (Jugo). Kalau mau cakupan lebih luas, itu yang
  Sumire lakukan.
- **Belum dibuktikan sampai revoke.** Yang terukur: paket terkirim, balasan
  server diterima. Membuktikan sesi benar-benar ke-revoke butuh akun yang
  sedang online.

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
  sdp.py                 wire format SDP + Conn (tag-15 sentinel, zstd, AES)
  engine.py              Login / Game / Brute -> fetch_profile + kick
  devices.py             parsing device id + list akumulatif
  runs.py                registry run paralel + worker pool
  telegram.py            client Telegram: pacing, flood handling, long poll
  ui.py                  semua teks + keyboard (murni render)
  bot.py                 dispatch update -> aksi
```
