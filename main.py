#!/usr/bin/env python3
"""
MoonBot AGR - MultiCoin (Render-ready, headless)
- Default: DOGE,SOL,BTC,ETH with per-coin amounts (editable)
- Uses ccxt for OKX spot trading
- PAPER mode by ENV PAPER=true (default)
- Configure via config.json or ENV variables
"""

import os
import time
import json
import math
import signal
import threading
from collections import deque, defaultdict
from datetime import datetime
import ccxt
import requests

# ---------- CONFIG (can be overridden by config.json or ENV) ----------
DEFAULT_CONFIG = {
    "coins": ["DOGE/USDT", "SOL/USDT", "BTC/USDT", "ETH/USDT"],
    "amounts": {"DOGE/USDT": 5.0, "SOL/USDT": 3.0, "BTC/USDT": 1.0, "ETH/USDT": 1.0},
    "interval": 15,            # seconds
    "sma_period": 12,
    "buy_dip_pct": 0.35,       # percent (positive number -> meaning buy when price <= SMA*(1 - buy_dip_pct/100))
    "sell_pump_pct": 0.55,     # percent
    "take_profit_pct": 0.75,   # percent
    "stop_loss_pct": 2.0,      # percent
    "max_positions": 4,
    "paper": True,
    "okx_api_key_env": "OKX_API_KEY",
    "okx_secret_env": "OKX_API_SECRET",
    "okx_pass_env": "OKX_API_PASSPHRASE",
    "log_file": "moonbot_agr.log",
    "state_file": "moonbot_state.json"
}

# ---------- helpers ----------
def now_ts():
    return int(time.time())

def ts_human(ts=None):
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def log(msg, *a):
    s = f"[{ts_human()}] " + (msg % a if a else msg)
    print(s, flush=True)
    try:
        with open(cfg.get("log_file"), "a", encoding="utf8") as f:
            f.write(s + "\n")
    except Exception:
        pass

# ---------- load config ----------
cfg = DEFAULT_CONFIG.copy()
# try load config.json if exists
if os.path.exists("config.json"):
    try:
        usercfg = json.load(open("config.json", "r", encoding="utf8"))
        cfg.update(usercfg)
        log("Loaded config.json")
    except Exception as e:
        log("Failed to load config.json: %s", repr(e))

# allow ENV override for PAPER and interval etc.
cfg["paper"] = os.getenv("PAPER", str(cfg.get("paper"))).lower() in ("1","true","yes","on")
env_interval = os.getenv("INTERVAL")
if env_interval:
    try:
        cfg["interval"] = int(env_interval)
    except:
        pass

# ---------- init exchange ----------
api_key = os.getenv(cfg["okx_api_key_env"], "")
secret = os.getenv(cfg["okx_secret_env"], "")
passphrase = os.getenv(cfg["okx_pass_env"], "")

exchange = None
if api_key and secret:
    try:
        exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret,
            'password': passphrase,
            'enableRateLimit': True,
        })
        log("OKX client created (live keys provided). PAPER=%s", cfg["paper"])
    except Exception as e:
        log("Failed to init OKX client: %s", repr(e))
        exchange = None
else:
    log("OKX API keys not provided via environment. Running in PAPER mode.")
    exchange = None

# ---------- persistent state ----------
state = {
    "positions": {},   # symbol -> {qty, avg_price, entries: [...]}
    "trades": []       # list of trade records (for audit)
}
state_lock = threading.Lock()

def load_state():
    if os.path.exists(cfg["state_file"]):
        try:
            s = json.load(open(cfg["state_file"], "r", encoding="utf8"))
            state.update(s)
            log("Loaded state from %s", cfg["state_file"])
        except Exception as e:
            log("Failed to load state: %s", repr(e))

def save_state():
    try:
        with state_lock:
            json.dump(state, open(cfg["state_file"], "w", encoding="utf8"), indent=2)
    except Exception as e:
        log("Failed to save state: %s", repr(e))

# ---------- market data buffers ----------
price_buffers = {}
sma_buffers = {}
last_checked = {}

for sym in cfg["coins"]:
    price_buffers[sym] = deque(maxlen=cfg["sma_period"]*3)
    sma_buffers[sym] = deque(maxlen=cfg["sma_period"])

# ---------- utility functions ----------
def safe_fetch_ticker(symbol):
    # returns (timestamp, price) or (None, None) on fail
    try:
        if exchange:
            t = exchange.fetch_ticker(symbol)
            return int(time.time()), float(t['last'])
        else:
            # paper mode: use public OKX REST via CCXT without keys or public fallback
            t = ccxt.okx().fetch_ticker(symbol)
            return int(time.time()), float(t['last'])
    except Exception as e:
        log("fetch_ticker %s failed: %s", symbol, repr(e))
        return None, None

def compute_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(list(prices)[-period:]) / period

def market_buy(symbol, usdt_amount):
    """
    market buy: convert USDT -> quote currency amount calculation,
    we will create market buy with ccxt if not paper.
    Returns (filled_qty, avg_price) or (None, None) on fail.
    """
    try:
        ts, last = safe_fetch_ticker(symbol)
        if last is None:
            return None, None
        base, quote = symbol.split("/")
        qty = usdt_amount / last
        qty = float(ccxt.Exchange.priceToPrecision(exchange if exchange else ccxt.Exchange(), symbol, qty)) if exchange else qty
    except Exception:
        qty = usdt_amount / last
    if cfg["paper"] or exchange is None:
        log("[SIM] BUY %s %s @ %s (simulated)", round(qty,8), symbol, last)
        return qty, last
    try:
        order = exchange.create_market_buy_order(symbol, qty)
        filled = float(order.get('filled', order.get('amount', 0.0)))
        avg = float(order.get('average', last))
        log("[LIVE] BUY %s %s @ %s", filled, symbol, avg)
        return filled, avg
    except Exception as e:
        log("market_buy fail: %s", repr(e))
        return None, None

def market_sell(symbol, qty):
    try:
        ts, last = safe_fetch_ticker(symbol)
        if last is None:
            return None, None
    except Exception:
        last = None
    if cfg["paper"] or exchange is None:
        log("[SIM] SELL %s %s @ %s (simulated)", qty, symbol, last)
        return qty, last
    try:
        order = exchange.create_market_sell_order(symbol, qty)
        filled = float(order.get('filled', order.get('amount', 0.0)))
        avg = float(order.get('average', last))
        log("[LIVE] SELL %s %s @ %s", filled, symbol, avg)
        return filled, avg
    except Exception as e:
        log("market_sell fail: %s", repr(e))
        return None, None

# ---------- trade logic per symbol ----------
def symbol_loop(symbol):
    period = int(cfg["sma_period"])
    buy_pct = float(cfg["buy_dip_pct"])
    sell_pct = float(cfg["sell_pump_pct"])
    tp_pct = float(cfg["take_profit_pct"])
    sl_pct = float(cfg["stop_loss_pct"])
    interval = int(cfg["interval"])
    max_pos = int(cfg["max_positions"])
    amount_usdt = float(cfg["amounts"].get(symbol, 1.0))

    log("Starting loop for %s (interval %ds, SMA %d)", symbol, interval, period)
    while True:
        try:
            ts, price = safe_fetch_ticker(symbol)
            if price is None:
                time.sleep(2)
                continue

            # update buffers
            buf = price_buffers[symbol]
            buf.append(price)
            sma = compute_sma(buf, period)
            last_checked[symbol] = ts

            # calculate position state
            with state_lock:
                pos = state["positions"].get(symbol)

            # Trading rule:
            # if no open position -> consider BUY when price <= sma*(1 - buy_pct/100)
            # if has position -> consider SELL when price >= sma*(1 + sell_pct/100) OR TP reached OR SL triggered
            if sma is not None:
                dev_pct = (price - sma) / sma * 100.0
                # BUY condition
                if (pos is None or pos.get("qty", 0) == 0):
                    if dev_pct <= -buy_pct:
                        # make buy
                        qty, avg = market_buy(symbol, amount_usdt)
                        if qty:
                            with state_lock:
                                state["positions"].setdefault(symbol, {"qty":0.0,"avg":0.0,"entries":[]})
                                p = state["positions"][symbol]
                                # update weighted average
                                total_cost = p["avg"] * p["qty"] + avg * qty
                                p["qty"] = p["qty"] + qty
                                p["avg"] = total_cost / p["qty"]
                                p["entries"].append({"ts": now_ts(), "side":"buy", "qty":qty, "price":avg, "usdt": amount_usdt, "paper": cfg["paper"]})
                                state["trades"].append({"ts":now_ts(), "symbol":symbol, "side":"buy", "qty":qty, "price":avg, "usdt": amount_usdt, "paper": cfg["paper"]})
                                save_state()
                else:
                    # pos exists -> check TP / sell_pump / SL
                    entry_avg = pos["avg"]
                    unreal_pct = (price - entry_avg) / entry_avg * 100.0
                    # TP
                    if unreal_pct >= tp_pct or dev_pct >= sell_pct:
                        # sell all pos
                        qty_to_sell = pos["qty"]
                        filled, sell_price = market_sell(symbol, qty_to_sell)
                        if filled:
                            with state_lock:
                                state["trades"].append({"ts": now_ts(), "symbol":symbol, "side":"sell", "qty":filled, "price":sell_price, "paper": cfg["paper"]})
                                # clear position
                                state["positions"][symbol] = {"qty":0.0,"avg":0.0,"entries":[]}
                                save_state()
                    # STOP LOSS
                    elif unreal_pct <= -sl_pct:
                        qty_to_sell = pos["qty"]
                        filled, sell_price = market_sell(symbol, qty_to_sell)
                        if filled:
                            with state_lock:
                                state["trades"].append({"ts": now_ts(), "symbol":symbol, "side":"sell_stop", "qty":filled, "price":sell_price, "paper": cfg["paper"]})
                                state["positions"][symbol] = {"qty":0.0,"avg":0.0,"entries":[]}
                                save_state()

            time.sleep(interval)
        except Exception as e:
            log("Exception in loop %s: %s", symbol, repr(e))
            time.sleep(5)

# ---------- health / runner ----------
threads = []
def start_all():
    load_state()
    log("MoonBot AGR multi-coins starting. PAPER=%s", cfg["paper"])
    for sym in cfg["coins"]:
        t = threading.Thread(target=symbol_loop, args=(sym,), daemon=True)
        t.start()
        threads.append(t)

def graceful_shutdown(signum, frame):
    log("Shutdown signal received (%s). Saving state...", signum)
    save_state()
    log("State saved. Exiting.")
    os._exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

if __name__ == "__main__":
    start_all()
    # keep running
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        graceful_shutdown(None, None)
