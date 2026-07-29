# Setup, step by step

From nothing to alerts landing on your phone. Roughly 30 minutes, most of it
waiting for accounts. Follow it in order — each step is checkable, so you find
out something is wrong at the step that broke it rather than three steps later.

**Steps 1 and 2 alone give you a working console with real alerts**, no accounts
and no credentials. Start there.

---

## Contents

1. [Get the code running locally](#1-get-the-code-running-locally)
2. [Try it before setting anything up](#2-try-it-before-setting-anything-up)
3. [Choose your data sources](#3-choose-your-data-sources)
4. [Telegram](#4-telegram)
5. [SEC EDGAR](#5-sec-edgar-free)
6. [Financial Modeling Prep](#6-financial-modeling-prep)
7. [Interactive Brokers, for open interest](#7-interactive-brokers-for-open-interest)
8. [Tune the thresholds to your tickers](#8-tune-the-thresholds-to-your-tickers)
9. [Install it as a service](#9-install-it-as-a-service)
10. [Day to day](#10-day-to-day)
11. [When something breaks](#when-something-breaks)

---

## 1. Get the code running locally

Python 3.11 or newer.

```bash
git clone https://github.com/thewongdirection/stock-movement-monitor
cd stock-movement-monitor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
export PYTHONPATH=src
```

Check it imports and the schema is intact:

```bash
python -m monitor params rvol
```

You should see `signals.volume.rvol_threshold` with its valid range. Every
tunable setting is listed by `monitor params` with no argument.

---

## 2. Try it before setting anything up

The monitor can replay captured market data, so you can see real alerts with no
accounts at all.

```bash
python -m monitor --config config.replay.yaml run --dry-run --as-of 2026-07-24T12:00:00
```

That prints genuine alerts built from real NVDA and MSFT bars and option chains.
Then open the interactive surface — the same command set the Telegram bot
exposes, with no bot token:

```bash
python -m monitor --config config.replay.yaml console
```

```
> /status
> /list
> /params rvol
> /set signals.volume.rvol_threshold 1.5 MSFT
> /scan NVDA
> /quit
```

If `/set signals.volume.rvol_threshold 99` gives you an error rather than a
confirmation, everything is wired correctly — that refusal is deliberate.

---

## 3. Choose your data sources

This decides which of the next four steps you need.

| You want | Signal | Source | Cost |
|---|---|---|---|
| To know a position was **actually taken** | `open_interest` | IBKR gateway | Free with a funded IBKR account |
| Insider buys and sells | `insider` | SEC EDGAR | Free |
| Unusual volume for the time of day | `volume` | FMP **or** IBKR | FMP Starter, or free via IBKR |
| CAN SLIM scorecards | — | FMP | Starter plan |
| Alerts on your phone | — | Telegram | Free |

**The recommended combination** is FMP for bars plus IBKR for option chains,
which is what `config.example.yaml` ships with. If you would rather not pay for
FMP, set `sources.bars: ibkr` and `canslim.enabled: false` — you lose the CAN
SLIM scorecard and keep everything else.

Edit `config.yaml`:

```yaml
watchlist:
  - NVDA
  - MSFT

sources:
  bars: fmp        # or ibkr
  options: ibkr    # or off, if you skip step 7
  insider: sec
  trades: off
```

Then create your credentials file:

```bash
cp deploy/env.example .env
chmod 600 .env
```

Fill it in over the next four steps. `monitor verify` will tell you at any point
which entries your particular configuration actually needs — it does not demand
an FMP key when your bars come from IBKR.

---

## 4. Telegram

**Get a bot token.** In Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
```

Answer its two questions (a display name, then a username ending in `bot`). It
replies with a token that looks like `1234567890:AAFakeTokenValue...`.

**Get your chat id.** Send your new bot any message — say `hello`. It will not
reply yet; that is expected, the bot is not running. Then open this URL in a
browser, with your token pasted in:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Find `"chat":{"id":987654321,...}`. That number is your chat id.

> **You must message the bot first.** Telegram does not let a bot start a
> conversation. Until you do, `getUpdates` returns an empty list and the monitor
> reports that it cannot see the chat.

Put both in `.env`:

```
TELEGRAM_BOT_TOKEN=1234567890:AAFakeTokenValue...
TELEGRAM_CHAT_ID=987654321
```

Set `notify.channel: telegram` in `config.yaml`, then confirm:

```bash
python -m monitor verify
```

Treat the bot token like a password — anyone holding it can send messages as
your bot. The monitor itself only ever answers the one chat id you configured.

---

## 5. SEC EDGAR (free)

No key, no account. EDGAR asks that automated requests identify themselves, and
blocks the ones that do not.

```
SEC_USER_AGENT=stock-movement-monitor you@example.com
```

It must be an address that reaches you — the monitor refuses to start the SEC
source without an `@`, because a bad user agent gets your IP blocked rather than
producing an error you can read.

---

## 6. Financial Modeling Prep

Sign up at [financialmodelingprep.com](https://financialmodelingprep.com) and
copy your key into `.env`:

```
FMP_API_KEY=...
```

> **Plan requirement.** The endpoints this project uses — `historical-chart` for
> intraday bars, and the statement endpoints for CAN SLIM — need the **Starter
> plan or above**. A free key authenticates successfully and then returns an
> entitlement error on exactly those calls. `monitor verify` reports it as such
> rather than leaving you with a mysteriously empty watchlist.

If you would rather not pay for FMP:

```yaml
sources:
  bars: ibkr
canslim:
  enabled: false
```

---

## 7. Interactive Brokers, for open interest

**This is the step that gets you the headline signal.** Open interest is the
only figure in this project that shows a position was actually opened and held
rather than merely traded, and IBKR is the one source at this price point that
publishes it per contract.

IBKR has no API key. It runs a **Client Portal Gateway** on your machine that
proxies your own account, authenticated by a browser login.

**Install and start the gateway:**

```bash
mkdir -p ~/ibkr-gateway && cd ~/ibkr-gateway
curl -O https://download2.interactivebrokers.com/portal/clientportal.gw.zip
unzip clientportal.gw.zip
./bin/run.sh root/conf.yaml
```

**Log in:** open <https://localhost:5000> in a browser. You will get a
certificate warning — the gateway serves a self-signed certificate for
`localhost`, which is why `ibkr.verify_tls` is `false` in the config. Accept it,
sign in with your IBKR credentials, and complete two-factor if you use it.

**Confirm the monitor can reach it:**

```bash
python -m monitor verify --raw
```

`--raw` prints one real contract row so you can check the open-interest figure
came through. If it is empty, see
[the field-id note](#open-interest-comes-back-empty) below.

Three things to know about the gateway:

- **The session expires**, roughly daily. When it does, the gateway keeps
  answering with empty results rather than a clear error — which is why the
  monitor checks `/iserver/auth/status` before every request and reports an
  expired session as a source problem instead of a quiet market.
- **Only one session at a time.** Logging into IBKR's website or TWS elsewhere
  takes the session over, and the monitor reports that too.
- **TLS verification is off for this source only**, and only because the
  certificate is self-signed for localhost. If you ever point `ibkr.base_url` at
  a remote host, turn `ibkr.verify_tls` back on.

If you do not have an IBKR account, set `sources.options: off` and
`signals.open_interest.enabled: false`. Everything else still works.

---

## 8. Tune the thresholds to your tickers

Skipping this is the most common reason a monitor is either silent or
unbearable.

**Capture some real data:**

```bash
python -m monitor capture
```

That writes today's bars and option chain into `state/replay/`. Run it daily for
a week to build history — or just use the NVDA and MSFT captures already in the
repo.

**See what the defaults would have fired on:**

```bash
python -m monitor --config config.replay.yaml run --dry-run --as-of 2026-07-24T16:00:00
```

**What to expect.** Measured over thirteen sessions of real 30-minute bars:

- NVDA crossed the default `rvol_threshold: 2.0` **twice**.
- MSFT crossed it **not once**.
- Sweeping `zscore_threshold` from 1.5 to 4.0 changed **nothing** — every bar
  that cleared RVOL also cleared z. RVOL is the binding constraint; the z-score
  is a guard against a distorted median, not a second opinion.

The lesson is that one global number cannot serve both a volatile name and a
quiet one. Use per-ticker overrides:

```yaml
overrides:
  MSFT:
    signals:
      volume:
        rvol_threshold: 1.6
        min_price_move_pct: 0.35
```

Or from chat, without editing the file:

```
/set signals.volume.rvol_threshold 1.6 MSFT
```

**Rules of thumb.** Too quiet: lower `rvol_threshold` first, then
`min_price_move_pct`. Too noisy: raise `min_notional` before raising
`rvol_threshold` — it cuts small prints without making you miss the big moves.
For open interest, `min_oi_change_pct` is the scale-free knob; `min_oi_change`
biases toward heavily traded names.

`monitor params` lists every setting with its valid range. Values outside those
ranges are clamped when they come from `config.yaml` — and reported, so you know
it happened — but **refused** when typed into the bot.

---

## 9. Install it as a service

On the machine that will run it:

```bash
sudo ./deploy/install.sh
```

The installer is idempotent — re-run it after a `git pull` and it picks up new
units and dependencies without clobbering your `config.yaml`, your `.env`, or
your state database.

It installs to `/opt/stock-movement-monitor`, builds a virtualenv, writes
starter config files if they do not exist, `chmod 600`s the `.env`, and installs
three systemd units. **Nothing starts automatically** — it needs your
credentials first.

```bash
# 1. Copy your working config and credentials across
sudo cp config.yaml /opt/stock-movement-monitor/config.yaml
sudo cp .env        /opt/stock-movement-monitor/.env
sudo chmod 600      /opt/stock-movement-monitor/.env

# 2. Check it from where it will actually run
cd /opt/stock-movement-monitor
sudo -u $USER .venv/bin/python -m monitor verify

# 3. Start the hourly poll
sudo systemctl enable --now monitor.timer

# 4. Optional: the Telegram bot, so you can talk to it
sudo systemctl enable --now monitor-bot
```

Confirm:

```bash
systemctl list-timers monitor.timer     # when it next fires
journalctl -u monitor -f                # what it is doing
sudo systemctl start monitor            # poll right now, don't wait
```

**Why the timer fires hourly all day**, rather than only during market hours:
encoding 09:30–16:00 America/New_York in a unit file means getting daylight
saving right in two places, and getting it wrong means silently not polling for
a week each spring. The application already knows the market calendar —
holidays, half days, early closes — and decides for itself what is meaningful.
It also means Form 4 filings, which land until roughly 22:00 ET, are still
picked up in the evening.

---

## 10. Day to day

You should not have to do anything. When you do want to:

```
/status              is it alive, what has it seen lately
/health              probe every source right now
/scan NVDA           fetch fresh and evaluate immediately
/grade NVDA          CAN SLIM scorecard
/brief NVDA          a request to paste into the can-slim-grader skill
/history             recent alerts
/watch TSLA          add to the watchlist
/set PATH VALUE      change a threshold; add a ticker to scope it
/reset               undo every runtime change
```

Every one of these refetches. Nothing is answered from the last run's result.

Changes made from chat live in `state/runtime.json` and apply from the next
scheduled run. `config.yaml` is never rewritten by a machine, so the file you
hand-edited stays the file you hand-edited.

**Getting the full CAN SLIM picture.** The monitor computes the letters
arithmetic can settle. For the two it cannot — the "new product or management"
half of **N**, and institutional sponsorship **I** — run:

```bash
python -m monitor grade NVDA --brief
```

and paste the output into Claude with the
[`can-slim-grader`](https://github.com/thewongdirection/can-slim-grader) skill
installed. It arrives pre-loaded with everything the monitor already fetched, so
the skill spends its effort on the judgement rather than re-deriving EPS growth.

**Housekeeping.** The state database prunes itself when you ask:

```bash
sudo -u $USER /opt/stock-movement-monitor/.venv/bin/python -m monitor prune
```

Add it to a weekly cron if you like. It is not urgent — the database grows by a
few kilobytes a day.

---

## When something breaks

### Nothing has arrived for days

Check in this order:

```bash
systemctl list-timers monitor.timer     # is the timer even enabled
journalctl -u monitor --since today     # did the runs happen
```

If runs are happening and finding nothing, your thresholds are too high — see
[step 8](#8-tune-the-thresholds-to-your-tickers). A `/scan NVDA` tells you
immediately whether data is arriving.

### "Data source problems" in every message

That block is the monitor telling you it could not see, rather than that it saw
nothing. Read the line: it names the source, the ticker, and one of
`unreachable`, `stale`, `corrupt`, `empty` or `unconfigured`.

`monitor verify` probes everything and prints exactly what came back.

### The IBKR session keeps expiring

Expected — roughly daily. Re-open <https://localhost:5000> and sign in. If it
happens more often, check whether you are logging into IBKR's website or TWS
elsewhere; only one session can hold the connection.

### Open interest comes back empty

The gateway's market-data field ids have been renumbered between builds. Run:

```bash
python -m monitor verify --raw
```

If the sample row has no open-interest value, set `ibkr.oi_field` in
`config.yaml` to the correct id for your build. The default is `"7638"`.

### FMP returns an entitlement error

The intraday and statement endpoints need the Starter plan. Either upgrade, or
switch to `sources.bars: ibkr` and set `canslim.enabled: false`.

### Telegram says it cannot see the chat

Message your bot from your own Telegram account first. A bot cannot open a
conversation, so until you do, the chat does not exist from its side.

### Alerts stopped after a config change

```bash
python -m monitor validate
```

That reports hard errors and prints any value that was clamped for being out of
range. A clamped threshold is the usual cause: the run continued, but not with
the number you wrote.

### I want to start over

```
/reset                                              # runtime changes only
rm /opt/stock-movement-monitor/state/monitor.db     # dedup and history too
```

Deleting the database means the first run afterwards has no open-interest
baseline, so the OI signal stays quiet for one day while it rebuilds. That is
reported as a note, not an error.
