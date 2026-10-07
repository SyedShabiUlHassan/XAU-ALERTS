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

import hashlib, json, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from feeds import TF_SECONDS, get_candles

HERE = Path(__file__).parent
CONFIG = HERE / "config.yaml"
STATE = HERE / "state.json"
# A candle is stale only if a NEWER one should have closed by now and hasn't
# (i.e. the market is shut). The allowance therefore scales with the timeframe:
# a daily candle stays current for a day, a 1h candle for an hour. The extra
# slack absorbs GitHub's 5-15 minute scheduling lag.
STALE_SLACK = 45 * 60


def summary(md):
    """Append markdown to the GitHub Actions run summary (visible on the run page)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")
    print(md.replace("**", ""))


def tg_api(method, payload=None):
    """Call a Telegram Bot API method; return (ok, parsed_response_or_error_text)."""
    tok = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not tok:
        return False, "TELEGRAM_TOKEN secret is empty or missing"
    url = f"https://api.telegram.org/bot{tok}/{method}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read())
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def diagnose():
    """Check token and chat reachability, and report what is actually wrong."""
    summary("## Telegram diagnostics\n")
    tok = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    summary(f"- TELEGRAM_TOKEN: {'set, ' + str(len(tok)) + ' chars' if tok else '**MISSING**'}")
    summary(f"- TELEGRAM_CHAT_ID: {'`' + chat + '`' if chat else '**MISSING**'}")

    ok, res = tg_api("getMe")
    bot_user = None
    if ok:
        bot_user = res["result"].get("username")
        summary(f"- getMe: **OK** - bot is `@{bot_user}` (token is valid)")
    else:
        summary(f"- getMe: **FAILED** - `{res}`")
        summary("\n**Diagnosis: your bot token is invalid or revoked.** "
                "Get a fresh one from @BotFather (`/mybots` -> your bot -> API Token) "
                "and update the `TELEGRAM_TOKEN` secret.")
        return False

    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    if raw != raw.strip():
        summary(f"- note: chat id had surrounding whitespace ({len(raw)} chars "
                f"vs {len(raw.strip())} trimmed) - now stripped")

    ok, res = tg_api("getChat", {"chat_id": chat})
    if ok:
        summary(f"- getChat: **OK** - chat reachable")
    else:
        summary(f"- getChat: **FAILED** - `{res}`")

        # Find out which chat ids this bot has genuinely received messages from.
        ok2, upd = tg_api("getUpdates")
        seen = []
        if ok2:
            for u in upd.get("result", []):
                m = u.get("message") or u.get("edited_message") or {}
                c = m.get("chat") or {}
                if c.get("id") and c["id"] not in seen:
                    seen.append(c["id"])
        if seen:
            def mask(i):
                t = str(i)
                return t[:2] + "*" * max(0, len(t) - 6) + t[-4:]
            summary(f"- the bot has actually received messages from: "
                    + ", ".join(f"`{mask(i)}` ({len(str(i))} digits)" for i in seen))
            summary(f"- your configured chat id: `{mask(chat)}` ({len(chat)} digits)")
            if str(seen[0]) != str(chat):
                summary(f"\n**Diagnosis: TELEGRAM_CHAT_ID does not match.** The bot is "
                        f"talking to a different id than the one in your secret. "
                        f"Re-copy your id from @userinfobot and update the secret.")
                return False
        else:
            summary("- getUpdates shows no messages - the bot has not been messaged "
                    "from this account, or the messages are older than 24h "
                    "(send it another `hi` and re-run)")

        desc = str(res).lower()
        if "not found" in desc or "initiate" in desc or "blocked" in desc:
            summary(f"\n**Diagnosis: you have never opened a chat with the bot.** "
                    f"Telegram does not let a bot message you first. Open "
                    f"https://t.me/{bot_user} in Telegram, press **START**, then re-run.")
        else:
            summary(f"\n**Diagnosis: TELEGRAM_CHAT_ID `{chat}` is wrong.** "
                    "Get the right number from @userinfobot.")
        return False
    return True


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
    tok = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        try:
            body = json.dumps({"chat_id": chat, "text": text,
                               "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                         data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except urllib.error.HTTPError as e:
            print(f"  !! telegram HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        except Exception as e:
            print(f"  !! telegram failed: {type(e).__name__}: {e}", file=sys.stderr)

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
        if age > secs + STALE_SLACK:
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
            if not notify(text):
                raise SystemExit(f"ALERT UNDELIVERED for {lv['label']} - not marking as fired")
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

    tok = os.environ.get("TELEGRAM_TOKEN", "")
    print(f"telegram token: {'set (' + str(len(tok)) + ' chars)' if tok else 'MISSING'}  "
          f"chat id: {'set' if os.environ.get('TELEGRAM_CHAT_ID') else 'MISSING'}  "
          f"twelvedata key: {'set' if os.environ.get('TWELVEDATA_KEY') else 'MISSING'}")
    armed = [lv for lv in levels if level_id(lv) not in state["fired"]]
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"{len(armed)} armed / {len(levels)} total")
    for lv in levels:
        tag = "FIRED" if level_id(lv) in state["fired"] else "armed"
        print(f"  [{tag}] {lv['label']:<22} {lv['price']:<10g} {lv['timeframe']:<4} "
              f"{lv.get('direction','both')}")

    if "--test-notify" in sys.argv:
        if not diagnose():
            sys.exit(1)
        if not notify("🔔 Test from GitHub Actions — gold alerts are wired up correctly."):
            summary("\n- sendMessage: **FAILED** (see log)")
            sys.exit(1)
        summary("\n- sendMessage: **OK** - check your phone ✅")
        return

    if armed and check(armed, state, now):
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print("state.json updated")
    elif not armed:
        print("All levels have fired. Edit config.yaml to re-arm.")


if __name__ == "__main__":
    main()
