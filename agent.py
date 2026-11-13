#!/usr/bin/env python3
"""aiprove-autonomous-agent — a fully autonomous AIProve agent.

No human in the loop. One run does the whole loop from skill.md:

1. Reads https://aiprove.fun/skill.md.
2. Generates a FRESH throwaway keypair at runtime and authenticates with it
   (challenge -> EIP-191 personal_sign -> verify). The key never leaves
   memory and is never printed, logged, or saved.
3. Lists open bounties.
4. For every bounty it can complete on its own, it completes it:
   - Binary direction forecasts (Brier score, e.g. "Will BTC close higher
     tomorrow?"): submits a naive momentum call from the public CoinGecko
     24h change — up -> {"answer": "yes", "confidence": 0.55},
     down -> {"answer": "no", "confidence": 0.55}. Confidence is capped at
     0.55 on purpose: a 24h momentum read is a weak signal and the agent
     does not pretend otherwise. This is a documented baseline, not an edge.
   - Anything else (code tasks, PosterJudge content, abs-error forecasts on
     assets with no free public price feed, referrals): logged as skipped
     with an explicit reason. A code-only agent cannot write and test a
     repo by itself, and it will not fabricate content or prices.
5. Writes a machine-readable run log (run_log.json) and prints a summary.

Usage:
    pip install -r requirements.txt
    python3 agent.py [--max-submits N] [--dry-run] [--handle H]

Defaults: --max-submits 3, live submits (entry is free in the beta).
--dry-run lists what it WOULD do without entering or submitting anything.

Exit 0 = loop finished (completed >= 0 bounties, every decision logged).
Exit 1 = the loop itself broke (skill.md unreadable, auth failed, no
open bounties listed).

Transport note: HTTP goes through curl (subprocess) with a urllib
fallback. Plain urllib suffers intermittent truncated reads
(http.client.IncompleteRead) behind some egress proxies, while curl
completes the same requests reliably. Retries cover the rest.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from eth_account import Account
from eth_account.messages import encode_defunct

DEFAULT_API = "https://aiprove.fun"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
COINGECKO = ("https://api.coingecko.com/api/v3/simple/price"
             "?ids=bitcoin,ethereum&vs_currencies=usd"
             "&include_24hr_change=true")
SUBMIT_DELAY_S = 8  # pause between live submits; be a polite competitor

# slug -> CoinGecko id, for the binary direction forecasts this agent can do.
BINARY_FORECASTS = {
    "btc-tomorrow": "bitcoin",
    "forecast-eth-24h": "ethereum",
    "forecast-btc-7d": "bitcoin",
}


def _curl_request(method, url, token=None, payload=None):
    """One HTTP request via curl. Returns (status, text). Raises on failure."""
    cmd = ["curl", "-sS", "-w", "\n%{http_code}", "-X", method, url,
           "-H", f"User-Agent: {UA}", "--max-time", "60"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if payload is not None:
        cmd += ["-H", "content-type: application/json",
                "--data", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
    if out.returncode != 0:
        raise RuntimeError(f"curl failed rc={out.returncode}: "
                           f"{out.stderr.strip()[:200]}")
    body, _, code = out.stdout.rpartition("\n")
    return int(code), body


def _urllib_request(method, url, token=None, payload=None):
    """Fallback transport via urllib. Returns (status, text)."""
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def http(api, method, path, token=None, payload=None, tries=3):
    """JSON request with retries. Returns (status, parsed-dict)."""
    last = None
    for attempt in range(1, tries + 1):
        for transport in (_curl_request, _urllib_request):
            try:
                status, text = transport(method, api + path, token, payload)
                if not text.strip():
                    return status, {}
                try:
                    return status, json.loads(text)
                except json.JSONDecodeError:
                    return status, {"raw": text[:300]}
            except Exception as e:  # noqa: BLE001 - try next transport/retry
                last = e
        print(f"      {method} {path} attempt {attempt} failed ({last}); "
              f"retrying...")
    return 0, {"error": f"transient network failure after {tries} tries: {last}"}


def fetch_text(api, path, tries=3):
    """GET a text resource with retries. Returns the text."""
    last = None
    for attempt in range(1, tries + 1):
        for transport in (_curl_request, _urllib_request):
            try:
                status, text = transport("GET", api + path)
                if status == 200 and text:
                    return text
                last = RuntimeError(f"HTTP {status}")
            except Exception as e:  # noqa: BLE001
                last = e
        print(f"      fetch {path} attempt {attempt} failed ({last}); "
              f"retrying...")
    raise RuntimeError(f"could not fetch {path}: {last}")


def get_prices():
    """Public 24h-change snapshot from CoinGecko (no key). Returns dict or {}."""
    try:
        status, text = _curl_request("GET", COINGECKO)
        if status != 200:
            print(f"      CoinGecko HTTP {status}; no price signal")
            return {}
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"      CoinGecko unreachable ({e}); no price signal")
        return {}


def decide(bounty, prices):
    """Return (action, detail): 'submit' with a prediction, or 'skip' + reason."""
    slug = bounty.get("slug", "?")
    lane = bounty.get("lane", "")
    metric = bounty.get("metric", "")
    if lane != "AutoGrade":
        return "skip", f"lane {lane} needs a poster/human judge"
    if slug in BINARY_FORECASTS and "Brier" in metric:
        coin = BINARY_FORECASTS[slug]
        info = prices.get(coin)
        if not info or "usd_24h_change" not in info:
            return "skip", "no public 24h price signal available"
        chg = info["usd_24h_change"]
        answer = "yes" if chg > 0 else "no"
        pred = {"answer": answer, "confidence": 0.55}
        return "submit", {"prediction": pred,
                          "reason": f"naive momentum: {coin} 24h {chg:+.2f}% "
                                    f"-> {answer} @ 0.55 (weak signal, capped)"}
    if "error" in metric.lower() and "brier" not in metric.lower():
        return "skip", "abs-error forecast on an asset with no free public feed"
    return "skip", f"metric '{metric}' needs code/content this agent cannot produce"


def main():
    ap = argparse.ArgumentParser(description="AIProve autonomous agent")
    ap.add_argument("--max-submits", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--handle", default="omega-auto")
    args = ap.parse_args()
    log = {"decisions": [], "completed": [], "skipped": []}

    api = os.environ.get("AIPROVE_API_BASE", DEFAULT_API)
    skill = fetch_text(api, "/skill.md")
    print(f"[1/5] read skill.md ({len(skill)} chars)")
    for needle in ("/api/auth/challenge", "/api/auth/verify",
                   "/api/bounties", "/enter"):
        if needle not in skill:
            print(f"ERROR: skill.md does not mention {needle}")
            return 1

    acct = Account.create()
    print(f"[2/5] fresh throwaway keypair, address {acct.address}")
    status, ch = http(api, "GET", f"/api/auth/challenge?address={acct.address}")
    if status != 200 or "challenge" not in ch:
        print(f"ERROR: challenge failed HTTP {status}: {ch}")
        return 1
    sig = acct.sign_message(encode_defunct(text=ch["challenge"])).signature.hex()
    status, sess = http(api, "POST", "/api/auth/verify",
                        payload={"address": acct.address, "signature": sig})
    if status != 200 or "token" not in sess:
        print(f"ERROR: verify failed HTTP {status}: {sess}")
        return 1
    token = sess["token"]
    print(f"[3/5] authenticated, session expiresAt={sess.get('expiresAt')}")

    status, data = http(api, "GET", "/api/bounties?status=open", token=token)
    bounties = data.get("bounties", data if isinstance(data, list) else [])
    if status != 200 or not bounties:
        print(f"ERROR: list bounties failed HTTP {status}: {str(data)[:300]}")
        return 1
    print(f"[4/5] {len(bounties)} open bounties")

    prices = get_prices()
    print(f"      price signal: "
          + (", ".join(f"{c} {v.get('usd_24h_change', 0):+.2f}%"
                       for c, v in prices.items()) or "none"))
    submitted = 0
    for bounty in bounties:
        slug = bounty.get("slug", "?")
        action, detail = decide(bounty, prices)
        if action == "skip":
            print(f"  SKIP {slug}: {detail}")
            log["skipped"].append({"slug": slug, "reason": detail})
            log["decisions"].append({"slug": slug, "action": "skip",
                                      "reason": detail})
            continue
        if submitted >= args.max_submits:
            print(f"  SKIP {slug}: max-submits ({args.max_submits}) reached")
            log["skipped"].append({"slug": slug,
                                    "reason": "max-submits reached"})
            continue
        if args.dry_run:
            print(f"  WOULD SUBMIT {slug}: {detail['prediction']} "
                  f"({detail['reason']})")
            log["decisions"].append({"slug": slug, "action": "dry-run",
                                      "prediction": detail["prediction"]})
            continue
        payload = {"prediction": detail["prediction"],
                   "handle": args.handle}
        status, res = http(api, "POST", f"/api/bounties/{slug}/submit",
                           token=token, payload=payload)
        if status in (200, 201) and "id" in res:
            print(f"  SUBMITTED {slug}: {detail['prediction']} "
                  f"({detail['reason']}) -> submission {res['id']}")
            log["completed"].append({"slug": slug,
                                      "prediction": detail["prediction"],
                                      "submission_id": res["id"],
                                      "reason": detail["reason"]})
            submitted += 1
            time.sleep(SUBMIT_DELAY_S)
        else:
            print(f"  FAILED {slug}: HTTP {status}: {str(res)[:200]}")
            log["skipped"].append({"slug": slug,
                                    "reason": f"submit HTTP {status}: "
                                              f"{str(res)[:150]}"})
        log["decisions"].append({"slug": slug, "action": action,
                                  "prediction": detail["prediction"]})
    with open("run_log.json", "w") as f:
        json.dump(log, f, indent=2)
    print(f"[5/5] done: {len(log['completed'])} completed, "
          f"{len(log['skipped'])} skipped. Run log -> run_log.json")
    print("LOOP COMPLETE: skill.md -> fresh keypair -> auth -> list -> "
          "decide -> submit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
