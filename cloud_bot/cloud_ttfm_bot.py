import os
import time
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

# ============================================================================
# TTFM v4.3 BULUT (7/24) TELEGRAM SİNYAL & KÂR KİLİDİ BOTU (MT5 GEREKTİRMEZ)
# ============================================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "0"))  # 0 = Sonsuz, GitHub Actions için örn: 19800 (5.5 saat)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")

# Yerel çalışmada telegram_config.json varsa oradan oku
if not BOT_TOKEN or not CHAT_ID:
    local_cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "telegram_config.json")
    if os.path.exists(local_cfg):
        try:
            with open(local_cfg, "r", encoding="utf-8") as f:
                c = json.load(f)
                BOT_TOKEN = BOT_TOKEN or c.get("bot_token", "")
                CHAT_ID = CHAT_ID or c.get("chat_id", "")
        except Exception:
            pass

# Strateji Parametreleri (Claude Denetimli v4.3)
SWING_BARS = 5
MIN_SL_PCT = 0.15       # %0.15 minimum stop mesafesi (Bungee 3 bps komisyon koruması)
SLIPPAGE_CUSHION = 1.10 # Lot boyutlandırma kayma tamponu (SL * 1.10)
RISK_PCT_NORMAL = 1.35  # %1.35 baz risk

state = {
    "last_bar_ts": {"BTCUSD": 0, "XAUUSD": 0},
    "pending_orders": {},
    "open_positions": {},
    "utc_day": None,
    "daily_locked": False,
    "daily_lock_reason": "",
    "signals_sent": [],
    "update_offset": 0,
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                state.update(saved)
        except Exception as e:
            print("Durum yükleme uyarısı:", e)


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Durum kaydetme uyarısı:", e)


def tg_api(method, params=None, timeout=10):
    if not BOT_TOKEN:
        return {"ok": False, "error": "Missing BOT_TOKEN"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = None
    headers = {}
    if params is not None:
        data = json.dumps(params).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_tg(text):
    res = tg_api("sendMessage", {"chat_id": str(CHAT_ID), "text": text})
    state["signals_sent"].append(text)
    if len(state["signals_sent"]) > 20:
        state["signals_sent"] = state["signals_sent"][-20:]
    save_state()
    return res


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_btc_h1_bars():
    """Binance veya yedek olarak Bybit üzerinden son 40 adet H1 BTCUSDT mumunu çeker."""
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=40"
        raw = fetch_json(url)
        return [{
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        } for k in raw]
    except Exception:
        url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&limit=40"
        d = fetch_json(url)
        lst = list(reversed(d["result"]["list"]))
        return [{
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        } for k in lst]


def get_xau_bars(interval="15m", rng="5d"):
    """Yahoo Finance (GC=F / Altın) veya yedek olarak Binance PAXGUSDT mumlarını çeker."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range={rng}&interval={interval}"
        d = fetch_json(url)
        res = d["chart"]["result"][0]
        ts_list = res["timestamp"]
        q = res["indicators"]["quote"][0]
        bars = []
        for i in range(len(ts_list)):
            if q["open"][i] is None or q["close"][i] is None:
                continue
            bars.append({
                "time": int(ts_list[i]),
                "open": float(q["open"][i]),
                "high": float(q["high"][i]),
                "low": float(q["low"][i]),
                "close": float(q["close"][i]),
            })
        if len(bars) >= 20:
            return bars
    except Exception:
        pass

    b_int = "15m" if interval == "15m" else "1d"
    url = f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={b_int}&limit=40"
    raw = fetch_json(url)
    return [{
        "time": int(k[0]) // 1000,
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
    } for k in raw]


def compute_atr(bars, period=14):
    if len(bars) < period + 2:
        return 0.0
    trs = []
    for i in range(len(bars) - period - 1, len(bars) - 1):
        cur = bars[i]
        prev_c = bars[i - 1]["close"]
        tr = max(cur["high"] - cur["low"], abs(cur["high"] - prev_c), abs(cur["low"] - prev_c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def get_xau_daily_bias():
    try:
        d_bars = get_xau_bars(interval="1d", rng="1mo")
        if len(d_bars) < 2:
            return 0
        cur_d = d_bars[-1]
        prev_d = d_bars[-2]
        bull_sweep = (cur_d["low"] < prev_d["low"]) and (cur_d["close"] > prev_d["low"])
        bear_sweep = (cur_d["high"] > prev_d["high"]) and (cur_d["close"] < prev_d["high"])
        bull_body = cur_d["close"] > cur_d["open"]
        bear_body = cur_d["close"] < cur_d["open"]
        is_bull = bull_body or bull_sweep
        is_bear = bear_body or bear_sweep
        if is_bull and not is_bear:
            return 1
        if is_bear and not is_bull:
            return -1
        if bull_body:
            return 1
        if bear_body:
            return -1
    except Exception:
        pass
    return 0


def format_bungee_line(balance, entry_price, sl_dist, sym):
    risk_usd = balance * (RISK_PCT_NORMAL / 100.0)
    eff_sl = sl_dist * SLIPPAGE_CUSHION
    units = risk_usd / eff_sl if eff_sl > 0 else 0.0
    notional = units * entry_price
    max_notional = balance * 10.0 * 0.95
    capped = False
    if notional > max_notional:
        notional = max_notional
        units = notional / entry_price
        risk_usd = units * sl_dist
        capped = True
    eff_lev = notional / balance
    if sym == "BTCUSD":
        return f"{units:.3f} BTC (~{notional:.0f} USD | {eff_lev:.1f}x{' Tavan' if capped else ''} | Risk: {risk_usd:.0f} USD)"
    else:
        return f"{units:.2f} oz (~{notional:.0f} USD | {eff_lev:.1f}x{' Tavan' if capped else ''} | Risk: {risk_usd:.0f} USD)"


def check_utc_daily_reset():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    if state["utc_day"] != today_str:
        state["utc_day"] = today_str
        state["daily_locked"] = False
        state["daily_lock_reason"] = ""
        save_state()


def handle_tg_commands():
    res = tg_api("getUpdates", {"offset": state["update_offset"], "timeout": 1}, timeout=5)
    if not res.get("ok"):
        return
    for upd in res.get("result", []):
        state["update_offset"] = upd["update_id"] + 1
        msg = upd.get("message")
        if not msg or "text" not in msg:
            continue
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        cmd = msg["text"].lower().strip()
        if cmd.startswith("/durum"):
            send_tg(
                f"🌐 TTFM 7/24 BULUT BOTU: AKTİF\n"
                f"📅 UTC Gün: {state['utc_day']}\n"
                f"🔒 Günlük Kilit: {'AÇIK (' + state['daily_lock_reason'] + ')' if state['daily_locked'] else 'Yok (İşleme Açık)'}\n"
                f"⏳ Bekleyen Emirler: {list(state['pending_orders'].keys()) or 'Yok'}\n"
                f"📈 Açık Pozisyonlar: {list(state['open_positions'].keys()) or 'Yok'}"
            )
        elif cmd.startswith("/test"):
            send_tg(
                "🧪 [BULUT TEST SİNYALİ] 🟢 YENİ TTFM ALIŞ (BUY LIMIT) SİNYALİ!\n"
                "📊 Sembol: BTCUSD (PERIOD_H1)\n"
                "🎯 Giriş (Limit): 91250.00\n"
                "🛑 Stop Loss: 90450.00\n"
                "💰 Take Profit (2.00R): 92850.00\n"
                "🛡️ Kâr Kilidi (+1.0R @ 92050.00): SL -> 91450.00 (+0.25R)\n"
                "⚡ Bungee 10K: 0.077 BTC (~7008 USD | 0.7x | Risk: 61 USD)\n"
                "⏳ Geçerlilik: 2 Mum (C3-C4'te dolmazsa sil!)"
            )
        elif cmd.startswith("/son"):
            if not state["signals_sent"]:
                send_tg("📭 Henüz kayıtlı sinyal yok.")
            else:
                send_tg("📜 SON SİNYALLER:\n\n" + "\n\n---\n\n".join(state["signals_sent"][-5:]))
        elif cmd.startswith("/yardim") or cmd.startswith("/start"):
            send_tg(
                "📋 TTFM 7/24 BULUT BOTU KOMUTLARI:\n"
                "/durum - Bulut botunun anlık durumunu ve açık emirleri gör\n"
                "/test - Örnek sinyal kartı gönder\n"
                "/son - Son 5 bildirimi gör"
            )
    save_state()


def manage_symbol_tick(sym, bars, period_sec):
    if not bars or len(bars) < 10:
        return
    curr_bar = bars[-1]
    curr_price = curr_bar["close"]
    curr_high = curr_bar["high"]
    curr_low = curr_bar["low"]

    # 1) Bekleyen Emir Takibi
    if sym in state["pending_orders"]:
        po = state["pending_orders"][sym]
        if curr_bar["time"] >= po["expire_bar_ts"]:
            del state["pending_orders"][sym]
            send_tg(
                f"⏳ EMİR SÜRESİ DOLDU (C4 Kapandı)!\n"
                f"📊 {sym} ({po['side']} LIMIT @ {po['entry']:.2f}) dolmadan süre bitti.\n"
                f"🚨 BUNGEE AKSİYONU: Bungee'deki bekleyen limit emri SİL!"
            )
        else:
            if po["side"] == "BUY" and (curr_low <= po["sl"] or curr_high >= po["tp"]):
                del state["pending_orders"][sym]
                send_tg(f"⚠️ EMİR İPTAL EDİLDİ (xC2 / Kaçan Kurulum)!\n📊 {sym} BUY LIMIT ({po['entry']:.2f}) iptal oldu. Bungee'den SİL!")
            elif po["side"] == "SELL" and (curr_high >= po["sl"] or curr_low <= po["tp"]):
                del state["pending_orders"][sym]
                send_tg(f"⚠️ EMİR İPTAL EDİLDİ (xC2 / Kaçan Kurulum)!\n📊 {sym} SELL LIMIT ({po['entry']:.2f}) iptal oldu. Bungee'den SİL!")
            elif (po["side"] == "BUY" and curr_low <= po["entry"]) or (po["side"] == "SELL" and curr_high >= po["entry"]):
                state["open_positions"][sym] = po.copy()
                del state["pending_orders"][sym]
                send_tg(
                    f"⚡ LİMİT EMİR TETİKLENDİ (İŞLEM AKTİF)!\n"
                    f"📊 {sym} | {po['side']} @ {po['entry']:.2f}\n"
                    f"🛑 İlk SL: {po['sl']:.2f} | 🎯 TP: {po['tp']:.2f}\n"
                    f"🔔 Kâr Kilidi Tetik Fiyatı: {po['lock_trig']:.2f}"
                )

    # 2) Açık Pozisyon Takibi
    if sym in state["open_positions"]:
        pos = state["open_positions"][sym]
        if pos["side"] == "BUY":
            if not pos["be_locked"] and curr_high >= pos["lock_trig"]:
                pos["be_locked"] = True
                pos["sl"] = pos["lock_sl"]
                if sym == "BTCUSD":
                    send_tg(
                        f"🚨 ACİL BUNGEE AKSİYONU: KÂR KİLİDİ + %35 KISMİ KAPANIŞ!\n"
                        f"📊 {sym} (BUY @ {pos['entry']:.2f})\n"
                        f"✂️ 1. Adım: Bungee'de pozisyonun %35'ini ŞİMDİ kapat!\n"
                        f"🔒 2. Adım: Stop Loss'u {pos['lock_sl']:.2f} (+0.25R) seviyesine çek!\n"
                        f"🎯 Kalan %65 için Hedef TP: {pos['tp']:.2f}"
                    )
                else:
                    send_tg(
                        f"🚨 ACİL BUNGEE AKSİYONU: KÂR KİLİDİ AKTİF (+0.55R)!\n"
                        f"📊 {sym} (BUY @ {pos['entry']:.2f})\n"
                        f"🔒 Bungee'de Stop Loss'u ŞİMDİ {pos['lock_sl']:.2f} seviyesine çek!\n"
                        f"🎯 Hedef TP: {pos['tp']:.2f}"
                    )
            if curr_high >= pos["tp"]:
                del state["open_positions"][sym]
                state["daily_locked"] = True
                state["daily_lock_reason"] = f"{sym} Tam TP Aldı"
                send_tg(f"✅ HEDEF TP VURULDU! ({sym} BUY @ {pos['tp']:.2f})\n🔒 Günlük kâr kilidi devreye girdi.")
            elif curr_low <= pos["sl"]:
                was_be = pos["be_locked"]
                del state["open_positions"][sym]
                if was_be:
                    send_tg(f"🛡️ KÂR KİLİDİ STOPTAN KAPANDI! ({sym} BUY @ {pos['sl']:.2f} Kârlı Çıkış)")
                else:
                    state["daily_locked"] = True
                    state["daily_lock_reason"] = f"{sym} Stop Oldu"
                    send_tg(f"❌ STOP LOSS OLDU ({sym} BUY @ {pos['sl']:.2f}).\n🔒 Paylaşımlı günlük kilit aktif.")
        else:
            if not pos["be_locked"] and curr_low <= pos["lock_trig"]:
                pos["be_locked"] = True
                pos["sl"] = pos["lock_sl"]
                if sym == "BTCUSD":
                    send_tg(
                        f"🚨 ACİL BUNGEE AKSİYONU: KÂR KİLİDİ + %35 KISMİ KAPANIŞ!\n"
                        f"📊 {sym} (SELL @ {pos['entry']:.2f})\n"
                        f"✂️ 1. Adım: Bungee'de pozisyonun %35'ini ŞİMDİ kapat!\n"
                        f"🔒 2. Adım: Stop Loss'u {pos['lock_sl']:.2f} (+0.25R) seviyesine çek!\n"
                        f"🎯 Kalan %65 için Hedef TP: {pos['tp']:.2f}"
                    )
                else:
                    send_tg(
                        f"🚨 ACİL BUNGEE AKSİYONU: KÂR KİLİDİ AKTİF (+0.55R)!\n"
                        f"📊 {sym} (SELL @ {pos['entry']:.2f})\n"
                        f"🔒 Bungee'de Stop Loss'u ŞİMDİ {pos['lock_sl']:.2f} seviyesine çek!\n"
                        f"🎯 Hedef TP: {pos['tp']:.2f}"
                    )
            if curr_low <= pos["tp"]:
                del state["open_positions"][sym]
                state["daily_locked"] = True
                state["daily_lock_reason"] = f"{sym} Tam TP Aldı"
                send_tg(f"✅ HEDEF TP VURULDU! ({sym} SELL @ {pos['tp']:.2f})\n🔒 Günlük kâr kilidi devreye girdi.")
            elif curr_high >= pos["sl"]:
                was_be = pos["be_locked"]
                del state["open_positions"][sym]
                if was_be:
                    send_tg(f"🛡️ KÂR KİLİDİ STOPTAN KAPANDI! ({sym} SELL @ {pos['sl']:.2f} Kârlı Çıkış)")
                else:
                    state["daily_locked"] = True
                    state["daily_lock_reason"] = f"{sym} Stop Oldu"
                    send_tg(f"❌ STOP LOSS OLDU ({sym} SELL @ {pos['sl']:.2f}).\n🔒 Paylaşımlı günlük kilit aktif.")

    # 3) Yeni Mum Kapanışı ve Kurulum Kontrolü
    if curr_bar["time"] == state["last_bar_ts"].get(sym, 0):
        return
    state["last_bar_ts"][sym] = curr_bar["time"]
    save_state()

    if state["daily_locked"] or sym in state["open_positions"] or sym in state["pending_orders"]:
        return

    now_utc = datetime.now(timezone.utc)
    srv_hour = (now_utc.hour + 3) % 24
    weekday = now_utc.weekday()

    if sym == "BTCUSD":
        if weekday == 5 or (12 <= srv_hour < 17):
            return
        min_atr_mult, max_atr_mult = 0.35, 1.55
        buf_dist = 0.20
        target_rr = 2.00
        trig_r, lock_r = 1.00, 0.25
        bias = 0
    else:
        if weekday in (5, 6) or not (8 <= srv_hour < 13):
            return
        min_atr_mult, max_atr_mult = 0.40, 4.00
        atr_val = compute_atr(bars, 14)
        buf_dist = atr_val * 0.15
        target_rr = 1.95
        trig_r, lock_r = 1.30, 0.55
        bias = get_xau_daily_bias()

    atr = compute_atr(bars, 14)
    c2 = bars[-2]
    prev_c = bars[-3]
    c2_range = c2["high"] - c2["low"]
    if atr > 0 and (c2_range < atr * min_atr_mult or c2_range > atr * max_atr_mult):
        return

    c1_bars = bars[-(SWING_BARS + 2):-2]
    c1_high = max(b["high"] for b in c1_bars)
    c1_low = min(b["low"] for b in c1_bars)

    allow_buy = (bias >= 0)
    allow_sell = (bias <= 0)

    if allow_buy and c2["low"] < c1_low and c2["close"] > c1_low and c2["close"] > c2["open"]:
        cisd_level = max(prev_c["open"], prev_c["close"])
        if c2["close"] >= cisd_level:
            sl = round(c2["low"] - buf_dist, 2)
            eq = (c2["high"] + c2["low"]) / 2.0
            max_bull = c2["low"] + (c2_range * 0.58)
            entry = round(min(max(cisd_level, eq), max_bull), 2)
            if entry >= curr_price:
                entry = round(curr_price - 0.10, 2)
            risk_dist = entry - sl
            min_req = max(atr * 0.25, entry * (MIN_SL_PCT / 100.0))
            if risk_dist >= min_req and risk_dist > 0:
                tp = round(entry + risk_dist * target_rr, 2)
                lock_trig = round(entry + risk_dist * trig_r, 2)
                lock_sl = round(entry + risk_dist * lock_r, 2)
                b10k = format_bungee_line(10000.0, entry, risk_dist, sym)
                b25k = format_bungee_line(25000.0, entry, risk_dist, sym)
                tf_str = "PERIOD_H1" if sym == "BTCUSD" else "PERIOD_M15"
                state["pending_orders"][sym] = {
                    "side": "BUY", "entry": entry, "sl": sl, "tp": tp,
                    "lock_trig": lock_trig, "lock_sl": lock_sl,
                    "expire_bar_ts": curr_bar["time"] + period_sec * 2,
                    "be_locked": False
                }
                send_tg(
                    f"🟢 YENİ TTFM ALIŞ (BUY LIMIT) SİNYALİ! [7/24 Bulut]\n"
                    f"📊 Sembol: {sym} ({tf_str})\n"
                    f"🎯 Giriş (Limit): {entry:.2f}\n"
                    f"🛑 Stop Loss: {sl:.2f}\n"
                    f"💰 Take Profit ({target_rr:.2f}R): {tp:.2f}\n"
                    f"🛡️ Kâr Kilidi (+{trig_r:.1f}R @ {lock_trig:.2f}): SL -> {lock_sl:.2f} (+{lock_r:.2f}R)\n"
                    f"⚡ Bungee 10K: {b10k}\n"
                    f"⚡ Bungee 25K: {b25k}\n"
                    f"⏳ Geçerlilik: 2 Mum (C3-C4'te dolmazsa sil!)"
                )

    elif allow_sell and c2["high"] > c1_high and c2["close"] < c1_high and c2["close"] < c2["open"]:
        cisd_level = min(prev_c["open"], prev_c["close"])
        if c2["close"] <= cisd_level:
            sl = round(c2["high"] + buf_dist, 2)
            eq = (c2["high"] + c2["low"]) / 2.0
            min_bear = c2["high"] - (c2_range * 0.58)
            entry = round(max(min(cisd_level, eq), min_bear), 2)
            if entry <= curr_price:
                entry = round(curr_price + 0.10, 2)
            risk_dist = sl - entry
            min_req = max(atr * 0.25, entry * (MIN_SL_PCT / 100.0))
            if risk_dist >= min_req and risk_dist > 0:
                tp = round(entry - risk_dist * target_rr, 2)
                lock_trig = round(entry - risk_dist * trig_r, 2)
                lock_sl = round(entry - risk_dist * lock_r, 2)
                b10k = format_bungee_line(10000.0, entry, risk_dist, sym)
                b25k = format_bungee_line(25000.0, entry, risk_dist, sym)
                tf_str = "PERIOD_H1" if sym == "BTCUSD" else "PERIOD_M15"
                state["pending_orders"][sym] = {
                    "side": "SELL", "entry": entry, "sl": sl, "tp": tp,
                    "lock_trig": lock_trig, "lock_sl": lock_sl,
                    "expire_bar_ts": curr_bar["time"] + period_sec * 2,
                    "be_locked": False
                }
                send_tg(
                    f"🔴 YENİ TTFM SATIŞ (SELL LIMIT) SİNYALİ! [7/24 Bulut]\n"
                    f"📊 Sembol: {sym} ({tf_str})\n"
                    f"🎯 Giriş (Limit): {entry:.2f}\n"
                    f"🛑 Stop Loss: {sl:.2f}\n"
                    f"💰 Take Profit ({target_rr:.2f}R): {tp:.2f}\n"
                    f"🛡️ Kâr Kilidi (+{trig_r:.1f}R @ {lock_trig:.2f}): SL -> {lock_sl:.2f} (+{lock_r:.2f}R)\n"
                    f"⚡ Bungee 10K: {b10k}\n"
                    f"⚡ Bungee 25K: {b25k}\n"
                    f"⏳ Geçerlilik: 2 Mum (C3-C4'te dolmazsa sil!)"
                )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        payload = {
            "status": "ok",
            "utc_day": state["utc_day"],
            "daily_locked": state["daily_locked"],
            "pending_orders": list(state["pending_orders"].keys()),
            "open_positions": list(state["open_positions"].keys()),
        }
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, format, *args):
        return


def run_http_server():
    try:
        srv = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        srv.serve_forever()
    except Exception:
        pass


def main():
    first_cloud_boot = not os.path.exists(STATE_FILE)
    load_state()
    threading.Thread(target=run_http_server, daemon=True).start()
    start_ts = time.time()
    print(f"🌐 7/24 Bulut TTFM Sinyal Botu başlatıldı (Port {PORT})...")

    try:
        if state["last_bar_ts"].get("BTCUSD", 0) == 0:
            btc_b = get_btc_h1_bars()
            if btc_b:
                state["last_bar_ts"]["BTCUSD"] = btc_b[-1]["time"]
        if state["last_bar_ts"].get("XAUUSD", 0) == 0:
            xau_b = get_xau_bars("15m", "5d")
            if xau_b:
                state["last_bar_ts"]["XAUUSD"] = xau_b[-1]["time"]
        save_state()
        if first_cloud_boot:
            send_tg(
                "☁️ TTFM 7/24 BULUT SİNYAL BOTU DEVREDE! (GitHub Cloud)\n\n"
                "✅ Artık bilgisayarınız tamamen KAPALI olsa bile bulut sunucusu:\n"
                "• BTCUSD (H1) ve XAUUSD (M15) mumlarını 7/24 tarar,\n"
                "• Yeni Limit Emir Sinyallerini (Bungee 10K & 25K adetleriyle) gönderir,\n"
                "• Emir dolumlarını ve 🚨 ACİL Kâr Kilidi (+0.25R / +0.55R SL Taşıma & %35 Kısmi Kapanış) anlarını saniyesinde bildirir!\n\n"
                "📌 Komutlar: /durum | /test | /son | /yardim"
            )
    except Exception as e:
        print("İlk veri çekme uyarısı:", e)

    while True:
        if MAX_RUNTIME_SEC > 0 and (time.time() - start_ts) >= MAX_RUNTIME_SEC:
            print("⏱️ Maksimum çalışma süresine ulaşıldı, durum kaydedilip çıkılıyor...")
            save_state()
            break

        try:
            check_utc_daily_reset()
            handle_tg_commands()

            btc_bars = get_btc_h1_bars()
            manage_symbol_tick("BTCUSD", btc_bars, 3600)

            xau_bars = get_xau_bars("15m", "5d")
            manage_symbol_tick("XAUUSD", xau_bars, 900)
        except Exception as e:
            print("Tarama hatası:", e)

        time.sleep(10)


if __name__ == "__main__":
    main()
