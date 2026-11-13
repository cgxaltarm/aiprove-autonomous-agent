# aiprove-autonomous-agent

Fully autonomous agent for the AIProve `best-autonomous-agent` bounty
(agent: OmegaSignal, wallet `0x1BBa41288C1EC9244Aadafe949cd56Ee8c490EBc`).

One run, no human in the loop: reads `skill.md`, authenticates with a
**fresh throwaway keypair generated at runtime**, lists open bounties, and
completes every bounty it can do on its own — then writes a run log.

## What it completes

Binary direction forecasts (Brier score) on crypto it can price from the
free public CoinGecko feed — `btc-tomorrow`, `forecast-eth-24h`,
`forecast-btc-7d`. The call is a documented naive momentum baseline: 24h
change up → `{"answer": "yes", "confidence": 0.55}`, down → `no` @ 0.55.
Confidence is capped at 0.55 deliberately — a 24h momentum read is a weak
signal and the agent does not pretend otherwise.

## What it skips, and why (logged per bounty in `run_log.json`)

- PosterJudge content/art/meme bounties — need a poster/human judge.
- Code bounties — a code-only agent cannot write, test, and ship a repo
  by itself; it will not fabricate one.
- Abs-error forecasts on stocks/gold — no free public price feed for
  those assets from this environment (Binance public API is geo-blocked
  here; CoinGecko covers crypto only).
- Referrals — need other agents, not a solo loop.

## Run

```bash
pip install -r requirements.txt
python3 agent.py                     # live: up to 3 submits, 8s apart
python3 agent.py --max-submits 2     # live, capped at 2
python3 agent.py --dry-run           # decide + log only, submit nothing
```

Exit `0` = loop finished cleanly (completed + skipped both logged).
The throwaway keypair exists only in memory for one run — never printed,
logged, or saved.

## Verified live run

See `transcript.txt` — a real run against the live API on 2026-11-13
that authenticated with a fresh throwaway address, listed 21 open
bounties, and submitted its naive-momentum calls. `run_log.json` is the
machine-readable record of every decision.
