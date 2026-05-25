"""
bot.py — NEXUS SMC BOT — Deriv WebSocket API
Stratégie : M15 tendance → M5 OB/FVG → confirmation bougie → trade
Tourne sur le cloud 24h/24 sans PC
"""

import asyncio
import json
import websockets
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from collections import deque
import os

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_TOKEN     = os.environ.get("DERIV_TOKEN", "VOTRE_TOKEN_ICI")
APP_ID        = "1089"
WS_URL        = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
SYMBOL        = "frxXAUUSD"   # Gold sur Deriv WebSocket
RISK_USD      = 1.0
RR_RATIO      = 2.0
MAX_DAILY_LOSS = 5.0
MAX_TRADES_DAY = 5

# Stockage des prix
prices_m1  = deque(maxlen=500)
prices_m5  = deque(maxlen=500)
prices_m15 = deque(maxlen=500)

# État du bot
state = {
    "balance":       0,
    "daily_pnl":     0,
    "daily_trades":  0,
    "in_trade":      False,
    "wins":          0,
    "losses":        0,
    "last_contract": None,
}


# ─────────────────────────────────────────────
# INDICATEURS SMC
# ─────────────────────────────────────────────

def to_df(prices_deque) -> pd.DataFrame:
    data = list(prices_deque)
    if len(data) < 2:
        return pd.DataFrame()
    df = pd.DataFrame(data, columns=["time", "Open", "High", "Low", "Close"])
    df.set_index("time", inplace=True)
    return df


def get_trend(df: pd.DataFrame) -> str:
    if len(df) < 50:
        return "ranging"
    ema20 = df["Close"].ewm(span=20).mean()
    ema50 = df["Close"].ewm(span=50).mean()
    price = df["Close"].iloc[-1]
    if ema20.iloc[-1] > ema50.iloc[-1] and price > ema50.iloc[-1]:
        return "bullish"
    elif ema20.iloc[-1] < ema50.iloc[-1] and price < ema50.iloc[-1]:
        return "bearish"
    return "ranging"


def detect_ob(df: pd.DataFrame, bias: str) -> list:
    obs = []
    for i in range(2, len(df) - 2):
        c = df.iloc[i]
        n = df.iloc[i + 1]
        rng = n["High"] - n["Low"]
        if rng == 0:
            continue
        if bias == "bullish":
            if c["Close"] < c["Open"]:
                if n["Close"] > n["Open"] and (n["Close"] - n["Open"]) / rng > 0.5:
                    obs.append({"top": c["High"], "bottom": c["Low"],
                                "mid": (c["High"] + c["Low"]) / 2,
                                "zone_type": "OB"})
        else:
            if c["Close"] > c["Open"]:
                if n["Close"] < n["Open"] and (n["Open"] - n["Close"]) / rng > 0.5:
                    obs.append({"top": c["High"], "bottom": c["Low"],
                                "mid": (c["High"] + c["Low"]) / 2,
                                "zone_type": "OB"})
    return obs[-3:] if obs else []


def detect_fvg(df: pd.DataFrame, bias: str) -> list:
    fvgs = []
    pip = 0.01
    for i in range(1, len(df) - 1):
        prev = df.iloc[i - 1]
        nxt  = df.iloc[i + 1]
        if bias == "bullish":
            gap = nxt["Low"] - prev["High"]
            if gap >= 3 * pip:
                fvgs.append({"top": nxt["Low"], "bottom": prev["High"],
                             "mid": (nxt["Low"] + prev["High"]) / 2,
                             "zone_type": "FVG"})
        else:
            gap = prev["Low"] - nxt["High"]
            if gap >= 3 * pip:
                fvgs.append({"top": prev["Low"], "bottom": nxt["High"],
                             "mid": (prev["Low"] + nxt["High"]) / 2,
                             "zone_type": "FVG"})
    return fvgs[-3:] if fvgs else []


def get_best_zone(obs, fvgs, price, bias):
    candidates = obs + fvgs
    if not candidates:
        return None
    if bias == "bullish":
        below = [z for z in candidates if z["top"] < price]
        return max(below, key=lambda z: z["top"]) if below else None
    else:
        above = [z for z in candidates if z["bottom"] > price]
        return min(above, key=lambda z: z["bottom"]) if above else None


def check_signal(df_m15, df_m5) -> dict:
    """Analyse complète et retourne le signal."""
    if len(df_m15) < 50 or len(df_m5) < 50:
        return {"signal": None, "reason": "Pas assez de données"}

    trend = get_trend(df_m15)
    if trend == "ranging":
        return {"signal": None, "reason": "Marché ranging M15"}

    # Impulsion M5
    last5 = df_m5.iloc[-5:]
    best_body = 0
    best_dir  = None
    for _, c in last5.iterrows():
        rng = c["High"] - c["Low"]
        if rng == 0:
            continue
        body = abs(c["Close"] - c["Open"])
        if body / rng > best_body:
            best_body = body / rng
            best_dir  = "bullish" if c["Close"] > c["Open"] else "bearish"

    if best_body < 0.55 or best_dir != trend:
        return {"signal": None, "reason": f"Pas d'impulsion M5 alignée ({trend})"}

    price = df_m5["Close"].iloc[-1]
    obs   = detect_ob(df_m5, trend)
    fvgs  = detect_fvg(df_m5, trend)
    zone  = get_best_zone(obs, fvgs, price, trend)

    if zone is None:
        return {"signal": None, "reason": "Aucune zone OB/FVG"}

    # Vérifier que le prix est proche de la zone
    distance = abs(price - zone["mid"])
    if distance > 5.0:
        return {"signal": None, "reason": f"Zone trop loin ({distance:.1f})"}

    signal = "buy" if trend == "bullish" else "sell"
    pip    = 0.01
    margin = 3 * pip

    if signal == "buy":
        sl = zone["bottom"] - margin
        tp = price + (price - sl) * RR_RATIO
    else:
        sl = zone["top"] + margin
        tp = price - (sl - price) * RR_RATIO

    sl_pips = abs(price - sl) / pip
    if sl_pips < 5:
        return {"signal": None, "reason": "SL trop petit"}

    # Calcul du lot pour risquer RISK_USD
    lot = round(RISK_USD / (sl_pips * 1.0), 2)
    lot = max(0.01, min(lot, 1.0))

    return {
        "signal":    signal,
        "price":     price,
        "sl":        round(sl, 2),
        "tp":        round(tp, 2),
        "sl_pips":   round(sl_pips, 1),
        "lot":       lot,
        "zone_type": zone["zone_type"],
        "trend":     trend,
        "reason":    f"✅ Setup {signal.upper()} | {zone['zone_type']} | trend {trend}",
    }


# ─────────────────────────────────────────────
# WEBSOCKET DERIV
# ─────────────────────────────────────────────

async def send(ws, data: dict):
    await ws.send(json.dumps(data))


def log(msg: str):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")


async def run_bot():
    log("🚀 NEXUS SMC BOT démarré — connexion à Deriv...")

    async with websockets.connect(WS_URL) as ws:

        # ── Authentification ──
        await send(ws, {"authorize": API_TOKEN})
        resp = json.loads(await ws.recv())
        if "error" in resp:
            log(f"❌ Erreur auth: {resp['error']['message']}")
            return

        info = resp["authorize"]
        state["balance"] = info["balance"]
        log(f"✅ Connecté — {info['loginid']} | Balance: ${info['balance']:.2f}")

        # ── Charger historique M5 ──
        log("📊 Chargement historique M5 et M15...")
        await send(ws, {
            "ticks_history": SYMBOL,
            "granularity":   300,   # M5 = 300 secondes
            "count":         500,
            "style":         "candles",
            "end":           "latest",
        })
        resp = json.loads(await ws.recv())
        if "candles" in resp:
            for c in resp["candles"]:
                t = datetime.fromtimestamp(c["epoch"])
                prices_m5.append([t, c["open"], c["high"], c["low"], c["close"]])
            log(f"  M5 : {len(prices_m5)} bougies chargées")

        await send(ws, {
            "ticks_history": SYMBOL,
            "granularity":   900,   # M15 = 900 secondes
            "count":         500,
            "style":         "candles",
            "end":           "latest",
        })
        resp = json.loads(await ws.recv())
        if "candles" in resp:
            for c in resp["candles"]:
                t = datetime.fromtimestamp(c["epoch"])
                prices_m15.append([t, c["open"], c["high"], c["low"], c["close"]])
            log(f"  M15: {len(prices_m15)} bougies chargées")

        # ── Abonnement aux ticks en temps réel ──
        await send(ws, {"ticks": SYMBOL, "subscribe": 1})
        log(f"📡 Abonné aux ticks {SYMBOL} — scan toutes les 5 min\n")

        tick_count = 0
        last_analysis = datetime.now()

        async for message in ws:
            data = json.loads(message)

            # ── Mise à jour du prix ──
            if data.get("msg_type") == "tick":
                tick = data["tick"]
                price = tick["quote"]
                tick_count += 1

                now = datetime.now()

                # Analyse toutes les 5 minutes
                elapsed = (now - last_analysis).total_seconds()
                if elapsed < 300:
                    continue

                last_analysis = now

                # Vérifications risque
                if state["daily_pnl"] <= -MAX_DAILY_LOSS:
                    log(f"⛔ Stop loss journalier atteint (${state['daily_pnl']:.2f})")
                    continue

                if state["daily_trades"] >= MAX_TRADES_DAY:
                    log(f"⛔ Max trades journaliers atteints ({MAX_TRADES_DAY})")
                    continue

                if state["in_trade"]:
                    log("⏳ Position en cours...")
                    continue

                # Filtre session
                hour = now.hour
                if not (7 <= hour <= 17):
                    log(f"😴 Hors session ({hour}h UTC) — attente...")
                    continue

                # Analyse SMC
                df_m5  = to_df(prices_m5)
                df_m15 = to_df(prices_m15)

                result = check_signal(df_m15, df_m5)
                log(f"🔍 Analyse | Prix: {price:.2f} | {result['reason']}")

                if result["signal"] is None:
                    continue

                # Placer le trade
                await place_trade(ws, result)

            # ── Résultat du contrat ──
            elif data.get("msg_type") == "proposal_open_contract":
                contract = data.get("proposal_open_contract", {})
                if contract.get("is_sold"):
                    profit = float(contract.get("profit", 0))
                    state["in_trade"]     = False
                    state["daily_pnl"]   += profit
                    state["balance"]     += profit

                    if profit > 0:
                        state["wins"] += 1
                        log(f"✅ WIN +${profit:.2f} | Balance: ${state['balance']:.2f} | W:{state['wins']} L:{state['losses']}")
                    else:
                        state["losses"] += 1
                        log(f"❌ LOSS ${profit:.2f} | Balance: ${state['balance']:.2f} | W:{state['wins']} L:{state['losses']}")

            elif data.get("msg_type") == "buy":
                if "error" in data:
                    log(f"❌ Erreur trade: {data['error']['message']}")
                    state["in_trade"] = False
                else:
                    contract_id = data["buy"]["contract_id"]
                    state["last_contract"] = contract_id
                    state["daily_trades"] += 1
                    log(f"📤 Trade ouvert — Contrat #{contract_id}")
                    # Suivre le contrat
                    await send(ws, {
                        "proposal_open_contract": 1,
                        "contract_id": contract_id,
                        "subscribe": 1,
                    })


async def place_trade(ws, signal: dict):
    """Place un trade via l'API Deriv."""
    log(f"\n🎯 SIGNAL DÉTECTÉ !")
    log(f"   Direction : {signal['signal'].upper()}")
    log(f"   Prix      : {signal['price']:.2f}")
    log(f"   SL        : {signal['sl']:.2f} ({signal['sl_pips']:.0f} pips)")
    log(f"   TP        : {signal['tp']:.2f}")
    log(f"   Zone      : {signal['zone_type']}")
    log(f"   Risque    : ${RISK_USD} → Reward: ${RISK_USD * RR_RATIO}")

    contract_type = "CALL" if signal["signal"] == "buy" else "PUT"

    # Proposal d'abord
    await send(ws, {
        "proposal": 1,
        "amount":   RISK_USD,
        "basis":    "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "duration": 5,
        "duration_unit": "m",
        "symbol":   SYMBOL,
    })

    resp = json.loads(await ws.recv())

    if "error" in resp:
        log(f"❌ Proposal error: {resp['error']['message']}")
        return

    if "proposal" not in resp:
        log("❌ Pas de proposal reçu")
        return

    proposal_id = resp["proposal"]["id"]
    payout      = resp["proposal"]["payout"]
    log(f"   Payout estimé: ${payout:.2f}")

    # Acheter le contrat
    state["in_trade"] = True
    await send(ws, {
        "buy":   proposal_id,
        "price": RISK_USD,
    })


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("═"*55)
    print("  NEXUS SMC BOT — Deriv WebSocket")
    print("  Gold XAUUSD | RR 1:2 | $1 risque")
    print("═"*55)
    asyncio.run(run_bot())
