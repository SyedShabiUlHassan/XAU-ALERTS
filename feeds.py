#!/usr/bin/env python3
"""Gold price feeds with automatic fallback.

Tries each source in order until one returns usable candles:
  1. Twelve Data  - real-time spot XAU/USD, free key, 800 calls/day
  2. Deriv        - frxXAUUSD over WebSocket, no key (currently returning 520)
  3. Yahoo        - GC=F gold FUTURES, no key (a few $ off spot - last resort)

Every source returns the same shape, oldest first:
    [{"epoch": <candle open, unix secs>, "open","high","low","close": float}, ...]
"""
from __future__ import annotations

import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone

TF_SECONDS = {"3m": 180, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def _get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _resample(candles, src_secs, dst_secs):
    """Aggregate finer candles into coarser ones, aligned to the UTC epoch."""
    out, cur, b = [], None, None
    for c in candles:
        k = (c["epoch"] // dst_secs) * dst_secs
        if k != cur:
            if b:
                out.append(b)
            cur, b = k, {**c, "epoch": k}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
    if b:
        out.append(b)
    return out


# ------------------------------------------------------------ Twelve Data ---
TD_INTERVAL = {"3m": "1min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}


def twelvedata(tf, count, key):
    if not key:
        raise RuntimeError("no TWELVEDATA_KEY set")
    interval = TD_INTERVAL[tf]
    size = count * 3 if tf == "3m" else count          # 3m is built from 1min bars
    q = urllib.parse.urlencode({
        "symbol": "XAU/USD", "interval": interval, "outputsize": min(size + 5, 5000),
        "timezone": "UTC", "apikey": key,
    })
    d = _get(f"https://api.twelvedata.com/time_series?{q}")
    if d.get("status") == "error":
        raise RuntimeError(f"twelvedata: {d.get('message')}")
    rows = []
    for v in reversed(d.get("values", [])):            # API returns newest first
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"
                              if len(v["datetime"]) > 10 else "%Y-%m-%d")
        rows.append({"epoch": int(t.replace(tzinfo=timezone.utc).timestamp()),
                     "open": float(v["open"]), "high": float(v["high"]),
                     "low": float(v["low"]), "close": float(v["close"])})
    if tf == "3m":
        rows = _resample(rows, 60, 180)
    return rows


# ------------------------------------------------------------------ Deriv ---
def deriv(tf, count, app_id="1089"):
    import websocket                                   # only needed for this source
    ws = websocket.create_connection(
        f"wss://ws.derivws.com/websockets/v3?app_id={app_id}&l=EN", timeout=25)
    try:
        ws.send(json.dumps({"ticks_history": "frxXAUUSD", "style": "candles",
                            "granularity": TF_SECONDS[tf], "count": count, "end": "latest"}))
        for _ in range(5):
            m = json.loads(ws.recv())
            if m.get("error"):
                raise RuntimeError(f"deriv: {m['error'].get('message')}")
            if "candles" in m:
                return sorted(({"epoch": int(c["epoch"]), "open": float(c["open"]),
                                "high": float(c["high"]), "low": float(c["low"]),
                                "close": float(c["close"])} for c in m["candles"]),
                              key=lambda c: c["epoch"])
        raise RuntimeError("deriv: no candle payload")
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------ Yahoo ---
YF_INTERVAL = {"3m": ("1m", "5d"), "15m": ("15m", "1mo"), "1h": ("1h", "3mo"),
               "4h": ("1h", "3mo"), "1d": ("1d", "1y")}


def yahoo(tf, count):
    iv, rng = YF_INTERVAL[tf]
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
             f"?interval={iv}&range={rng}")
    res = d["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    rows = [{"epoch": int(t), "open": float(q["open"][i]), "high": float(q["high"][i]),
             "low": float(q["low"][i]), "close": float(q["close"][i])}
            for i, t in enumerate(ts) if q["close"][i] is not None]
    if tf == "3m":
        rows = _resample(rows, 60, 180)
    elif tf == "4h":
        rows = _resample(rows, 3600, 14400)
    return rows[-count:]


# ------------------------------------------------------------- dispatcher ---
def get_candles(tf, count=10):
    """Return candles for `tf`, trying each source until one works."""
    errors = []
    for name, fn in (
        ("twelvedata", lambda: twelvedata(tf, count, os.environ.get("TWELVEDATA_KEY", ""))),
        ("deriv",      lambda: deriv(tf, count)),
        ("yahoo",      lambda: yahoo(tf, count)),
    ):
        try:
            rows = fn()
            if len(rows) >= 2:
                return rows, name
            errors.append(f"{name}: only {len(rows)} candles")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:90]}")
    raise RuntimeError("all feeds failed -> " + " | ".join(errors))
