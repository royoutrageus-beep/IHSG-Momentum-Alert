"""
IHSG MOMENTUM ALERT (dua arah)
==============================
Deteksi flash-move IHSG - baik JUNAM (guyuran turun) maupun APRESIASI (rally naik) -
pakai kombinasi VELOCITY (kecepatan gerak) + BREADTH (berapa banyak big caps ikut
bareng). Threshold statis dari open kelewat lambat buat nangkep pergerakan cepat
kayak kejadian 6.454 -> 6.306 dalam hitungan jam.

PENTING - Deployment via GitHub Actions:
GitHub Actions bukan long-running server. Script ini didesain jalan SEKALI per
hari, di-trigger cron pas jam buka bursa, terus loop internal sendiri sampai jam
tutup baru exit. Ini yang bikin state (cooldown, buffer harga) tetap konsisten
selama 1 sesi - beda kalau tiap alert cron trigger job baru (state hilang tiap run).

Cara pasang:
1. pip install yfinance requests pytz
2. isi TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID sebagai GitHub Secrets
3. pasang workflow .yml (contoh di bawah/terpisah) dengan cron trigger jam 08:55 WIB
4. script auto-exit setelah jam 15:50 WIB, job selesai, run lagi besok
"""

import os
import time
import requests
import yfinance as yf
from curl_cffi import requests as cffi_requests
from collections import deque
from datetime import datetime, time as dtime
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ISI_TOKEN_DISINI")

# Chat ID default (fallback kalau chat per-arah gak diisi)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_DISINI")

# OPSIONAL: pisah chat per arah biar bisa set custom notification sound beda-beda
# di Telegram (Settings > Notifications > custom per chat). Kalau gak diisi,
# alert dua-duanya jatuh ke TELEGRAM_CHAT_ID di atas.
TELEGRAM_CHAT_ID_JUNAM     = os.getenv("TELEGRAM_CHAT_ID_JUNAM", "")
TELEGRAM_CHAT_ID_APRESIASI = os.getenv("TELEGRAM_CHAT_ID_APRESIASI", "")

IHSG_TICKER = "^JKSE"

BIG_CAPS = [
    "BBCA.JK", "BBRI.JK", "BMRI.JK", "BBNI.JK",
    "TLKM.JK", "ASII.JK", "UNVR.JK", "ICBP.JK",
    "ADRO.JK", "AMMN.JK",
]

WINDOW_MINUTES      = 5      # rentang waktu buat hitung velocity
JUNAM_VELOCITY      = -0.5   # % turun dalam WINDOW_MINUTES => kandidat junam
APRESIASI_VELOCITY  = 0.5    # % naik dalam WINDOW_MINUTES => kandidat apresiasi
BREADTH_THRESHOLD   = 0.6    # minimal 60% big caps ikut arah yang sama
BREADTH_MIN_MOVE    = 0.3    # syarat gerak minimal per saham (%) biar dihitung "ikutan"
POLL_INTERVAL_SEC   = 90     # jarak antar polling
COOLDOWN_MINUTES    = 15     # jeda sebelum alert arah yang sama fire lagi

TZ = pytz.timezone("Asia/Jakarta")
MARKET_CLOSE = dtime(15, 50)

# Session impersonate browser Chrome - Yahoo Finance suka nge-block request
# polos dari IP server/cloud (termasuk GitHub Actions), jadi kita nyamar
# pakai fingerprint browser asli biar gak ke-block.
YF_SESSION = cffi_requests.Session(impersonate="chrome")

# ── STATE ─────────────────────────────────────────────────────────────────
ihsg_buffer = deque()              # (timestamp, price)
last_alert_time = {"junam": None, "apresiasi": None}


def is_market_hours(now):
    t = now.time()
    session1 = dtime(9, 0) <= t <= dtime(11, 30)
    session2 = dtime(13, 30) <= t <= dtime(15, 50)
    return (session1 or session2) and now.weekday() < 5


def send_telegram(msg, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(url, data={"chat_id": target, "text": msg,
                                  "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"[WARN] gagal kirim telegram: {e}")


def get_ihsg_price(retries=2):
    for attempt in range(retries):
        try:
            data = yf.Ticker(IHSG_TICKER, session=YF_SESSION).history(period="1d", interval="1m")
            if not data.empty:
                return float(data["Close"].iloc[-1])
        except Exception as e:
            print(f"[WARN] gagal ambil harga IHSG (percobaan {attempt+1}/{retries}): {e}")
        time.sleep(3)
    return None


def get_bigcap_changes(window_minutes):
    """Return dict {ticker: pct_change} dalam window_minutes terakhir."""
    changes = {}
    for tk in BIG_CAPS:
        try:
            hist = yf.Ticker(tk, session=YF_SESSION).history(period="1d", interval="1m")
            if len(hist) < window_minutes + 1:
                continue
            recent = hist["Close"].iloc[-window_minutes:]
            pct = (recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0] * 100
            changes[tk] = pct
        except Exception:
            continue
    return changes


def fire_alert(direction, now, price, velocity_pct, changes, matching):
    ratio = len(matching) / len(changes)
    label = "🚨 IHSG JUNAM ALERT 🚨" if direction == "junam" else "🟢 IHSG APRESIASI ALERT 🟢"
    verb = "merah" if direction == "junam" else "hijau"
    detail_list = "\n".join(
        f"  • {tk}: {pct:+.2f}%" for tk, pct in sorted(matching.items(), key=lambda x: x[1],
                                                        reverse=(direction == "apresiasi"))
    )
    msg = (
        f"*{label}*\n\n"
        f"IHSG: {price:,.2f} ({velocity_pct:+.2f}% dalam {WINDOW_MINUTES} menit)\n"
        f"Breadth: {len(matching)}/{len(changes)} big caps ikut {verb}\n\n"
        f"{detail_list}\n\n"
        f"⏰ {now.strftime('%H:%M:%S WIB')}"
    )
    chat_override = TELEGRAM_CHAT_ID_JUNAM if direction == "junam" else TELEGRAM_CHAT_ID_APRESIASI
    send_telegram(msg, chat_id=chat_override or None)
    last_alert_time[direction] = now
    print(f"[ALERT FIRED] {direction} - {now}")


def check_momentum():
    now = datetime.now(TZ)

    price = get_ihsg_price()
    if price is None:
        return
    ihsg_buffer.append((now, price))

    cutoff = now.timestamp() - WINDOW_MINUTES * 60
    while ihsg_buffer and ihsg_buffer[0][0].timestamp() < cutoff:
        ihsg_buffer.popleft()

    if len(ihsg_buffer) < 2:
        return

    start_price = ihsg_buffer[0][1]
    velocity_pct = (price - start_price) / start_price * 100

    # tentuin arah kandidat, skip kalau gerak masih dalam batas normal
    if velocity_pct <= JUNAM_VELOCITY:
        direction = "junam"
    elif velocity_pct >= APRESIASI_VELOCITY:
        direction = "apresiasi"
    else:
        return  # belum ada momentum signifikan ke arah manapun

    # cooldown check per arah
    last = last_alert_time[direction]
    if last and (now - last).total_seconds() < COOLDOWN_MINUTES * 60:
        return

    changes = get_bigcap_changes(WINDOW_MINUTES)
    if not changes:
        return

    if direction == "junam":
        matching = {tk: pct for tk, pct in changes.items() if pct <= -BREADTH_MIN_MOVE}
    else:
        matching = {tk: pct for tk, pct in changes.items() if pct >= BREADTH_MIN_MOVE}

    breadth_ratio = len(matching) / len(changes)

    if breadth_ratio >= BREADTH_THRESHOLD:
        fire_alert(direction, now, price, velocity_pct, changes, matching)
    else:
        print(f"[INFO] velocity {direction} kena ({velocity_pct:+.2f}%) tapi breadth "
              f"cuma {breadth_ratio*100:.0f}%, kemungkinan cuma noise/beberapa saham")


def main_loop():
    print("IHSG Momentum Alert jalan... memantau tiap", POLL_INTERVAL_SEC, "detik")
    while True:
        now = datetime.now(TZ)

        # auto-exit setelah market tutup - biar job GitHub Actions selesai
        # dan gak numpuk running cost / kena limit 6 jam
        if now.time() > MARKET_CLOSE:
            print(f"[EXIT] Market udah tutup ({now.strftime('%H:%M')} WIB), job selesai.")
            break

        if is_market_hours(now):
            try:
                check_momentum()
            except Exception as e:
                print(f"[ERROR] {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main_loop()
