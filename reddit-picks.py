#!/usr/bin/env python3
"""Reddit cashtag harvester for Alpha Picks chatter — the best-effort, community layer.

Reality (verified 2026-07-04 from the author's IP): Reddit's `.json` endpoints are bot-walled, but `.rss`
is NOT — it returns real Atom (HTTP 200) with no key. The dedicated r/AlphaPicks sub is ~dead, but a
Reddit-WIDE search for "alpha picks" is lively with cashtags ($MU, $BE, $SNDK...). What surfaces is
PARTIAL + DELAYED — older picks, discussion, cashtag spikes — NOT the current-week pick on release
day (subscribers don't post that). So: a lagging signal, not a source of truth. Phil Town's 13F
(philtown-13f.py) stays the deterministic anchor; this is the soft layer.

Auth: uses Reddit OAuth if REDDIT_CLIENT_ID / REDDIT_SECRET are in ~/.config/pa-secrets.env (→ 100
req/min + comment search); otherwise falls back to keyless RSS automatically. Never fetches
seekingalpha.com directly (project rule).

Usage:  python3 reddit-picks.py [--config ../config.yaml] [--days 45]
Output: one JSON object per candidate ticker (jsonl), then a summary line.
        {"ticker","source":"reddit","via":"oauth|rss","mentions":N,"sample","permalink","label"}
"""
import base64, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "config.yaml"
LEDGER = HERE.parent / "log" / "picks-ledger.md"
SECRETS = Path.home() / ".config" / "pa-secrets.env"
UA = "pa-alpha-picks/1.0 (personal research; github.com/thejeff77)"
DAYS = 45

# Uppercase tokens that look like tickers but aren't — keep precision high.
STOP = {"A","I","AI","US","USA","UK","EU","SA","CEO","CFO","ETF","IPO","EPS","PE","PEG","YOY","QOQ",
        "AM","PM","EST","EDT","USD","FAQ","TV","DD","YOLO","WSB","IMO","IMHO","ATH","EOD","FOMO",
        "HODL","GDP","FED","SEC","IRS","OK","NEW","BUY","SELL","HOLD","THE","AND","FOR","ALL","API",
        "M1","YTD","ROI","AI","LLM","GPU","CPU","OK","TL","DR","Q1","Q2","Q3","Q4","EV","PT","FYI"}

if "--config" in sys.argv:
    CONFIG = Path(sys.argv[sys.argv.index("--config") + 1])
if "--days" in sys.argv:
    DAYS = int(sys.argv[sys.argv.index("--days") + 1])

# --- config (tolerant: PyYAML if present, else defaults) ----------------------------------------
def load_config():
    defaults = {"wide_search": True, "subs": ["AlphaPicks", "StockMarket", "ValueInvesting", "stocks"],
                "throttle_seconds": 8}
    try:
        import yaml
        cfg = (yaml.safe_load(CONFIG.read_text()) or {}).get("reddit", {})
        return {**defaults, **cfg}
    except Exception:
        return defaults

def load_secrets():
    env = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

# --- HTTP with throttle + 429 backoff -----------------------------------------------------------
_last = [0.0]
def http(url, headers=None, throttle=8, retries=2):
    for attempt in range(retries + 1):
        wait = throttle - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                _last[0] = time.monotonic()
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            _last[0] = time.monotonic()
            if e.code == 429 and attempt < retries:
                time.sleep(throttle * (attempt + 2))   # back off harder on rate-limit
                continue
            raise
        except Exception:
            if attempt < retries:
                time.sleep(throttle)
                continue
            raise

# --- OAuth (optional; silent fallback to RSS) ---------------------------------------------------
def get_token(env):
    cid, sec = env.get("REDDIT_CLIENT_ID"), env.get("REDDIT_SECRET")
    if not (cid and sec):
        return None
    try:
        # userless "application-only" grant needs just client_id/secret; if a password is present,
        # use the script (password) grant so we also get authed search/comment depth.
        if env.get("REDDIT_USERNAME") and env.get("REDDIT_PASSWORD"):
            data = urllib.parse.urlencode({"grant_type": "password",
                                           "username": env["REDDIT_USERNAME"],
                                           "password": env["REDDIT_PASSWORD"]}).encode()
        else:
            data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        req = urllib.request.Request("https://www.reddit.com/api/v1/access_token", data=data,
                                     headers={"User-Agent": UA, "Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r).get("access_token")
    except Exception:
        return None

# --- ticker extraction (precision-first: $TICK and EXCHANGE:TICK only) ---------------------------
CASH = re.compile(r"\$([A-Za-z]{1,5})\b")
EXCH = re.compile(r"\b(?:NYSE|NASDAQ|NYSEARCA|AMEX)\s*:\s*([A-Za-z]{1,5})\b", re.I)
def tickers(text):
    found = set()
    for m in CASH.findall(text) + EXCH.findall(text):
        t = m.upper()
        if t not in STOP and not t.isdigit():
            found.add(t)
    return found

# --- ledger dedup (skip a reddit ticker already surfaced this month) -----------------------------
def seen_keys():
    keys = set()
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if line.startswith("|") and "reddit" in line.lower():
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 5:
                    keys.add((cells[2].upper(), cells[4]))   # (ticker, as_of)
    return keys

# --- feed sources -------------------------------------------------------------------------------
def rss_entries(xml):
    """Parse an Atom feed → [{title, summary, permalink}]."""
    xml = re.sub(r'xmlns="[^"]+"', "", xml, count=1)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall(".//entry"):
        title = (e.findtext("title") or "").strip()
        content = (e.findtext("content") or "")
        link_el = e.find("link")
        href = link_el.get("href") if link_el is not None else ""
        out.append({"title": title, "summary": content, "permalink": href})
    return out

def gather(cfg, token, throttle):
    hdr = {"User-Agent": UA}
    base = "https://www.reddit.com"
    if token:
        hdr["Authorization"] = f"Bearer {token}"
        base = "https://oauth.reddit.com"
    entries, via = [], ("oauth" if token else "rss")
    sources = [f"{base}/r/AlphaPicks/new/.rss?limit=25"]
    if cfg.get("wide_search"):
        q = urllib.parse.quote('"alpha picks"')
        sources.append(f"{base}/search/.rss?q={q}&sort=new&limit=25")
    for url in sources:
        try:
            entries += rss_entries(http(url, hdr, throttle))
        except Exception as e:
            print(json.dumps({"warn": f"feed failed: {url.split('?')[0]} ({type(e).__name__})"}),
                  file=sys.stderr)
    return entries, via

# --- main ---------------------------------------------------------------------------------------
def main():
    cfg = load_config()
    throttle = int(cfg.get("throttle_seconds", 8))
    env = load_secrets()
    token = get_token(env)
    entries, via = gather(cfg, token, throttle)

    agg = {}   # ticker -> {"mentions":N, "sample":title, "permalink":url}
    for e in entries:
        blob = f"{e['title']} {e['summary']}"
        # only mine entries actually about the service, to cut generic-cashtag noise
        if "alpha pick" not in blob.lower() and "/r/AlphaPicks/" not in e["permalink"]:
            continue
        for t in tickers(blob):
            a = agg.setdefault(t, {"mentions": 0, "sample": e["title"][:120], "permalink": e["permalink"]})
            a["mentions"] += 1

    seen = seen_keys()
    as_of = time.strftime("%Y-%m")
    surfaced = 0
    for t, a in sorted(agg.items(), key=lambda kv: -kv[1]["mentions"]):
        if (t, as_of) in seen:
            continue
        print(json.dumps({"ticker": t, "source": "reddit", "via": via,
                          "mentions": a["mentions"], "sample": a["sample"],
                          "permalink": a["permalink"], "as_of": as_of,
                          "label": "reddit-sourced / unofficial / delayed"}))
        surfaced += 1

    print(json.dumps({"auth": via, "entries_scanned": len(entries),
                      "candidates": surfaced,
                      "note": "lagging/partial signal — not the release-day pick; verify before surfacing"}))

if __name__ == "__main__":
    main()
