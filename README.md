# Gold level alerts — GitHub Actions edition

Sends a Telegram push when a gold candle **closes** through a price level you set.
Runs free on GitHub Actions. No server, no credit card.

## Why this version exists

- GitHub's runners are outside Pakistan, so **Telegram works** from them even
  though `api.telegram.org` is blocked on your home ISP.
- No VM and no card needed.

**Limitation:** GitHub's minimum cron is 5 minutes and runs are frequently
5–15 minutes late. So:

| Timeframe | Reliable? |
|---|---|
| `1d`, `4h`, `1h` | ✅ yes |
| `15m` | ⚠️ alert may arrive late |
| `3m` | ❌ don't rely on it |

## Setup

### 1. Twelve Data API key (free, no card)
Sign up at <https://twelvedata.com/pricing> (Basic/free). Copy the API key.

### 2. Create the repo
Make a **PUBLIC** repo (public repos get unlimited free Actions minutes; private
would blow through the 2,000/month free quota at this cron rate). No secrets are
stored in the code — only levels, which are harmless.

Push these files to it.

### 3. Add repo Secrets
**Settings → Secrets and variables → Actions → New repository secret:**

| Name | Value |
|---|---|
| `TELEGRAM_TOKEN` | your BotFather token |
| `TELEGRAM_CHAT_ID` | your numeric chat id (from @userinfobot) |
| `TWELVEDATA_KEY` | your Twelve Data key |
| `DISCORD_WEBHOOK` | *(optional)* |

### 4. Test it
**Actions → Gold level alerts → Run workflow**, tick *test_notify*, Run.
Your phone should buzz.

### 5. Set your levels
Edit `config.yaml`, commit, push. Done — it checks every 5 minutes from then on.

## Day-to-day

- **Add/change a level:** edit `config.yaml`, commit.
- **Re-arm a fired level:** change its `price` or `label`, commit.
- **Re-arm everything:** empty `state.json` back to `{"fired": {}, "last_candle": {}}`.
- **See what fired:** read `state.json` — the bot commits it automatically.
- **Check it's alive:** the Actions tab shows every run.

## Gotchas

- GitHub **disables scheduled workflows after 60 days with no repo activity**.
  Push any commit occasionally, or re-enable it from the Actions tab.
- Twelve Data free allows 800 calls/day. This design costs at most ~415/day
  (it skips the API entirely when a timeframe's candle hasn't closed yet).
- Gold is shut Fri 21:00 → Sun 22:00 UTC. The script detects stale data and
  stays quiet.
- Feed order is Twelve Data → Deriv → Yahoo, with automatic fallback. Deriv is
  currently returning 520 errors; it'll be used again automatically if it recovers.
