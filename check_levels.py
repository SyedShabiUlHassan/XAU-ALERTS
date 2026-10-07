#!/usr/bin/env python3
"""
One-shot gold candle-close level check, designed for GitHub Actions cron.

For each level it looks at the most recently CLOSED candle of that level's
timeframe. If that candle closed through your price (and the candle before it
was on the other side), it sends a Telegram/Discord alert, marks the level as
fired in state.json, and never fires it again.

Secrets come from the environment, never from config.yaml:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK, TWELVEDATA_KEY
"""
from __future__ import annotations

import hashlib, json, os, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from feeds import TF_SECONDS, get_candles

HERE = Path(__file__).parent
CONFIG = HERE / "config.yaml"
STATE = HERE / "state.json"
# Accept a candle that closed within this long ago. Generous because GitHub's
# scheduler is frequently 5-15 minutes late.
STALE_SLACK = 45 * 60


def load_state():
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            s.setdefault("fired", {})
            s.setdefault("last_candle", {})
            return s
        except json.JSONDecodeError:
            pass
    return {"fired": {}, "last_candle": {}}


def level_id(lv):
    seed = f"{lv['label']}|{lv['price']}|{lv['timeframe']}|{lv.get('direction','both')}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def notify(text):
    sent = False
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            body = json.dumps({"chat_id": chat, "text": text,
                               "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                         data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            print(f"  !! telegram failed: {e}", file=sys.stderr)

    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"content": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            print(f"  !! discord failed: {e}", file=sys.stderr)

    if not sent:
        print(f"  !! NOT DELIVERED:\n{text}", file=sys.stderr)
    return sent


def check(levels, state, now):
    changed = False
    by_tf = {}
    for lv in levels:
        by_tf.setdefault(lv["timeframe"], []).append(lv)

    for tf, group in by_tf.items():
        secs = TF_SECONDS[tf]
        # Newest candle that is definitely finished, derived from the clock alone.
        latest_closed = (int(now) // secs) * secs - secs
        if state["last_candle"].get(tf) == latest_closed:
            print(f"[{tf}] candle {latest_closed} already processed - skipping (no API call)")
            continue

        try:
            candles, src = get_candles(tf, count=10)
        except Exception as e:
            print(f"[{tf}] FEED ERROR: {e}", file=sys.stderr)
            continue

        closed = [c for c in candles if c["epoch"] + secs <= now - 1]
        if len(closed) < 2:
            print(f"[{tf}] not enough closed candles ({len(closed)}) from {src}")
            continue

        prev_c, last_c = closed[-2], closed[-1]
        age = now - (last_c["epoch"] + secs)
        if age > STALE_SLACK:
            ended = datetime.fromtimestamp(last_c["epoch"] + secs, timezone.utc)
            print(f"[{tf}] stale via {src} (last close {ended:%Y-%m-%d %H:%M} UTC) "
                  f"- market closed, skipping")
            continue

        print(f"[{tf}] via {src}: prev {prev_c['close']:.2f} -> last {last_c['close']:.2f}")
        state["last_candle"][tf] = latest_closed
        changed = True

        for lv in group:
            lid = level_id(lv)
            if lid in state["fired"]:
                continue
            p, c, L = prev_c["close"], last_c["close"], float(lv["price"])
            direction = lv.get("direction", "both")
            side = None
            if c > L and p <= L and direction in ("above", "both"):
                side = "above"
            elif c < L and p >= L and direction in ("below", "both"):
                side = "below"
            if not side:
                continue

            closed_at = datetime.fromtimestamp(last_c["epoch"] + secs, timezone.utc)
            text = (f"{'🟢 ABOVE' if side == 'above' else '🔴 BELOW'}  —  "
                    f"GOLD closed {side} {L:g}\n"
                    f"Timeframe : {tf}\n"
                    f"Candle close : {c:.2f}\n"
                    f"Closed at : {closed_at:%Y-%m-%d %H:%M} UTC\n"
                    f'Level : "{lv["label"]}"')
            print(f"  TRIGGER {lv['label']}")
            notify(text)
            state["fired"][lid] = {
                "label": lv["label"], "price": L, "timeframe": tf, "side": side,
                "close": round(c, 3), "candle_epoch": last_c["epoch"],
                "fired_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
    return changed


def main():
    cfg = yaml.safe_load(CONFIG.read_text()) or {}
    levels = cfg.get("levels") or []
    if not levels:
        sys.exit("config.yaml has no levels")

    state = load_state()
    now = time.time()

    armed = [lv for lv in levels if level_id(lv) not in state["fired"]]
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"{len(armed)} armed / {len(levels)} total")
    for lv in levels:
        tag = "FIRED" if level_id(lv) in state["fired"] else "armed"
        print(f"  [{tag}] {lv['label']:<22} {lv['price']:<10g} {lv['timeframe']:<4} "
              f"{lv.get('direction','both')}")

    if "--test-notify" in sys.argv:
        notify("🔔 Test from GitHub Actions — gold alerts are wired up correctly.")
        return

    if armed and check(armed, state, now):
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print("state.json updated")
    elif not armed:
        print("All levels have fired. Edit config.yaml to re-arm.")


if __name__ == "__main__":
    main()
