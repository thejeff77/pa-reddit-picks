# pa-reddit-picks

A small, **read-only**, personal tool. On a daily schedule it reads *public* posts and
comments from a few finance subreddits (e.g. r/AlphaPicks, r/stocks, r/ValueInvesting) and
extracts stock-ticker mentions ("cashtags") to include in a **private daily summary email the
author sends only to himself**.

## What it does NOT do
- No posting, commenting, voting, or messaging.
- No storing or redistributing Reddit content publicly.
- Not commercial. Low volume — a few dozen requests per day at most.

## How it works
- Prefers Reddit's OAuth Data API when `REDDIT_CLIENT_ID` / `REDDIT_SECRET`
  (+ optional `REDDIT_USERNAME` / `REDDIT_PASSWORD` for a dedicated bot account) are present
  in the environment; otherwise falls back to keyless public RSS.
- Extracts `$TICK` and `NASDAQ:/NYSE:TICK` cashtags from posts that mention "alpha picks",
  with a stopword filter to keep precision high.
- Read-only; credentials are loaded from a local env file, never hard-coded.

## Usage
```
python3 reddit-picks.py [--config config.yaml] [--days 45]
```

Part of a personal daily-assistant pipeline. Read-only client of the Reddit Data API.
