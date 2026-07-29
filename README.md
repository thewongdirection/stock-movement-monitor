# stock-movement-monitor

Watches a short list of stocks about once an hour and tells you when something
happened that suggests **somebody actually took a position** — not merely that
the tape was busy.

It runs on a VPS or a home box under a systemd timer, sends to Telegram, and
answers questions from the same chat. Setup instructions are in
**[SETUP.md](SETUP.md)**.

---

## The one idea this is built around

Most market-alert tools measure **activity**. Activity is ambiguous. A stock can
trade 40 million shares in an hour because one institution is building a
position, or because market makers passed the same shares back and forth all
morning. Option "volume" has exactly the same problem: contracts that open and
close the same day leave no trace by the evening.

**Open interest is different.** It counts contracts that still exist at the end
of the day, because somebody opened them and *kept* them overnight — capital
committed, time decay running, through a weekend if it is Friday. That is as
close to proof of intent as public market data gets, and it is why this project
treats it as the headline signal rather than a footnote.

Everything else here is ordered by the same standard: how much does it actually
prove?

| Signal | What it proves | Cadence | Needs |
|---|---|---|---|
| **`open_interest`** | Contracts were opened *and held overnight*. A position was taken. | Once a day | IBKR gateway |
| **`insider`** | A named person bought or sold and filed a form saying so. The only **stated** direction. | Every run | SEC EDGAR (free) |
| **`blocks`** | One print large enough that a human authorised it. Size, but no direction. | Every run | A tick feed (off by default) |
| **`volume`** | More is trading this hour than usually does. Tells you where to look, not what to think. | Every run | FMP or IBKR bars |

Hourly is a deliberate cadence, not a limitation. At this rate delayed data is
fine, nothing needs to stay resident, and the job is *position management* —
"something happened in a name you hold, go look" — rather than trading.

---

## What an alert looks like

```
🔴 NVDA — 18.35K call contracts opened at $200 (2026-08-21)

• Opened 18.35K × 2026-08-21 $200 CALL — $366.98M notional,
  open interest 34,051 → 52,400
• Opened 8.8K × 2026-08-21 $205 CALL — $180.4M notional,
  open interest 21,000 → 29,800
• Net across the chain: calls +27,399, puts -1,740
• Snapshot 2026-07-24 against the previous stored chain — open interest is
  settled overnight, so this is end-of-day positioning

👉 18.35K new call contracts at $200 expiring 2026-08-21 were opened and held
   overnight — already 5% in the money. Someone is paying to be right about
   upside on a deadline. If you are long, that is mild confirmation; if you are
   short or holding covered calls near this strike, size for the possibility
   they are right.

⚠ Open interest is computed overnight by the OCC, so this is yesterday's
  positioning, not live.
⚠ A change cannot distinguish a directional bet from a hedge against something
  you cannot see.

📊 CAN SLIM A- (84%, 6/7 letters measurable) — BUY-RANGE
```

Three parts, and the split is deliberate:

- **Facts** are checkable numbers, so you can disagree with the conclusion.
- **The read** (`👉`) is one line saying what this is evidence of and what to do
  about it. It is always position management — trim, hold, tighten, size, check
  — never "buy" or "sell", because the tool does not know your position and does
  not have a view.
- **Caveats** (`⚠`) say what the alert *cannot* prove. They are never omitted.

Where direction is genuinely unknowable, the read says so rather than inventing
one:

> *3.4x normal volume with almost no net move (+0.05%) — buyers and sellers are
> fighting at this level, which often precedes a decisive move. No action yet;
> watch which side gives way, and note the level.*

And where it is knowable, it is specific:

> *Heavy selling pressure — price fell 1.8% on 3.4x normal volume into the
> close, which is where institutional orders finish. This is what distribution
> looks like: if you hold this, treat it as a prompt to review your stop or
> trim, not to average down.*

All of it lives in one file, [`signals/reads.py`](src/monitor/signals/reads.py),
which is the first place to look if you disagree with the tone.

---

## Honest limitations

Worth reading before you rely on it.

- **The consolidated tape carries no aggressor flag.** A trade print has price,
  size and venue. It does not say who initiated. Every "institutional buying"
  claim you see anywhere is an inference. Here, side defaults to `unknown` and
  the alert says so.
- **Open interest is inherently T+1.** The OCC settles it overnight. The
  freshest figure available at 11am is yesterday's close. That is how the
  clearing system works, not an implementation shortcut.
- **Form 4 has a two-business-day reporting deadline.** Insider alerts are
  always news about a completed trade. Every alert prints the lag.
- **Block detection ships disabled.** It needs individual trade prints, and
  neither FMP nor the IBKR gateway publishes them. A heavy bar with a narrow
  range *suggests* a cross — inferring one would mean labelling a guess as an
  observation, so the signal stays off until `sources.trades` points at real
  tick data.
- **"Off-exchange" is not "dark pool"** in the way the phrase is usually meant.
  Off-exchange prints are reported through a FINRA TRF and include internalised
  retail flow, which is the opposite of institutional intent.
- **Two CAN SLIM letters cannot be computed.** The "new product or management"
  half of **N** needs somebody to read the news, and **I** needs 13F holder
  counts. They are marked *unknown* and excluded from the score rather than
  counted as failures — a 4/7 that is really "3 of 7 measured" is a lie with a
  decimal point on it. `monitor grade TICKER --brief` produces a paste-ready
  request for the full [`can-slim-grader`](https://github.com/thewongdirection/can-slim-grader)
  skill, pre-loaded with everything already fetched.
- **This is decision support, not advice.** It has no idea what you own, what
  you paid, or what you can afford to lose.

---

## How it behaves

**It always uses fresh data.** Every scheduled run and every chat command
refetches. Nothing is ever answered from the previous run's result.

**Silence is never ambiguous.** A source that is unreachable, stale, corrupt or
empty produces a visible warning delivered alongside the alerts. A monitor that
goes quiet because its feed died looks identical to one that is quiet because
nothing happened — and feeds break under exactly the load a volatile day
produces. It also detects a *frozen* feed: if nothing on the whole watchlist
advances during market hours, that is not a calm market.

**Every threshold has a declared range.** `rvol_threshold: 0.1` fires on every
bar of every day; `rvol_threshold: 500` never fires at all. Both look like a
working config. Out-of-range values in `config.yaml` are clamped and reported so
the run still happens; the same value typed into the chat bot is **refused**,
because silently storing 20 when you typed 99 means debugging a monitor that is
not using the threshold you think it is.

**Per-ticker overrides are first class.** Measured over thirteen sessions of
real 30-minute bars, NVDA crossed `rvol_threshold: 2.0` twice and MSFT never
crossed it at all. One global number either spams you about the volatile name or
never mentions the quiet one.

**Nothing alerts twice.** A bar that was interesting at 11:00 is still in the
data at 12:00, 13:00 and 14:00. Delivery is recorded *after* a successful send,
so a Telegram outage means a retry rather than a lost alert.

**Credentials never reach a log line.** Every message this project emits passes
through a redactor, so a log file can be pasted into a bug report without
laundering it first.

---

## Commands

```
monitor run                one scheduled pass (what the systemd timer calls)
monitor run --dry-run      print to the console, record nothing as sent
monitor validate           check config.yaml and stop
monitor verify             probe every configured source right now
monitor verify --raw       ...and dump a raw IBKR row, to confirm field ids
monitor console            REPL over the same command set as the bot
monitor bot                run the Telegram bot in the foreground
monitor grade TICKER       CAN SLIM scorecard
monitor grade TICKER --brief   a request to paste into the grader skill
monitor capture            save today's data for replay and threshold tuning
monitor params [filter]    every tunable setting with its valid range
monitor prune              drop history past the retention window
```

From Telegram (or `monitor console`, which needs no bot token):

```
/status  /health  /scan [TICKER]  /grade TICKER  /brief TICKER  /history
/list  /watch TICKER  /unwatch TICKER
/params [filter]  /get PATH  /set PATH VALUE [TICKER]  /unset PATH  /reset
```

`/set` writes to a runtime overlay that the next scheduled run reads.
`config.yaml` is never rewritten by a machine, and `/reset` clears everything.
The bot answers only the configured chat id.

---

## Layout

```
src/monitor/
  models.py      domain types, and what each is honestly able to say
  clock.py       market calendar; every timestamp is US/Eastern
  config.py      bounded schema, per-ticker overrides, runtime overlay
  store.py       SQLite: dedup, OI snapshots, cluster history
  health.py      quiet market vs broken feed
  engine.py      one pass: fetch, evaluate, dedup, hand back
  cli.py         command line entry points
  sources/       fmp · ibkr · sec · replay
  signals/       open_interest · insider · blocks · volume · reads
  notify/        one message layout, telegram + console
  canslim/       deterministic scoring, optional Claude narrator, skill handoff
  bot/           one command set, two transports
deploy/          systemd units and an idempotent installer
```

Two details worth knowing:

**Every timestamp is US/Eastern.** The volume baseline compares a bar against
the *same clock slot* on previous sessions. The market opens at 09:30 Eastern
all year, but its UTC offset moves twice a year — so a UTC-bucketed baseline
would compare the January open against July mid-morning and call the difference
a signal.

**Replay is not a test fixture.** `monitor capture` saves real market data, and
the replay source refuses to reveal anything after its `as_of` position — a
sweep positioned at 11:00 cannot see the 14:00 bar. Lookahead bias makes every
strategy look brilliant, and it is easy to introduce by accident.

---

## Testing

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python -m pytest -q
```

441 tests, no network access required.
