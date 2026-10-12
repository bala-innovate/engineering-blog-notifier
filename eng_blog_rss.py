import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import feedparser
import requests

BASE_DIR = Path(__file__).parent
BLOGS_FILE = BASE_DIR / "blogs.md"
STATE_FILE = BASE_DIR / "seen.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
MAX_NEW_PER_FEED = 5  # cap per feed per run so one noisy blog can't flood the channel
EMBEDS_PER_MESSAGE = 10  # Discord limit
FETCH_TIMEOUT = 15
HEADERS = {"User-Agent": "engineering-blog-notifier/1.0"}


def load_feeds(path=BLOGS_FILE):
    """Parse blogs.md into {blog name: rss url}."""
    feeds = {}
    name = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            name = line[3:].strip().rstrip(":")
        elif name and (m := re.match(r"- RSS:\s*(\S+)", line)):
            feeds[name] = m.group(1)
    return feeds


def fetch_feed(item):
    name, url = item
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"[skip] {name}: {e}")
        return name, None
    if not parsed.entries:
        print(f"[skip] {name}: no entries (not a valid feed?)")
        return name, None
    return name, parsed


def entry_id(entry):
    return entry.get("id") or entry.get("link") or entry.get("title")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def to_embed(blog, entry):
    summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
    embed = {
        "title": entry.get("title", "No Title")[:256],
        "url": entry.get("link"),
        "description": summary[:300] + ("..." if len(summary) > 300 else ""),
        "author": {"name": blog[:256]},
    }
    if entry.get("published"):
        embed["footer"] = {"text": entry["published"]}
    return embed


def send_to_discord(embeds):
    for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        chunk = embeds[i:i + EMBEDS_PER_MESSAGE]
        while True:
            resp = requests.post(WEBHOOK_URL, json={"embeds": chunk}, timeout=15)
            if resp.status_code == 429:  # rate limited, wait and retry
                time.sleep(resp.json().get("retry_after", 1) + 0.5)
                continue
            resp.raise_for_status()
            break
        time.sleep(1)


def main():
    feeds = load_feeds()
    state = load_state()
    first_run = state is None
    state = state or {}

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(fetch_feed, feeds.items()))

    embeds = []
    for name, parsed in results:
        if parsed is None:
            continue  # keep previous state for feeds that failed this run
        ids = [entry_id(e) for e in parsed.entries if entry_id(e)]
        seen = set(state.get(name, []))
        new = [e for e in parsed.entries if entry_id(e) and entry_id(e) not in seen]
        # oldest first so Discord shows them in chronological order
        for entry in reversed(new[:MAX_NEW_PER_FEED]):
            embeds.append(to_embed(name, entry))
        state[name] = ids

    if first_run:
        print(f"First run: seeded {len(state)} feeds, nothing sent.")
    elif embeds:
        if not WEBHOOK_URL:
            sys.exit("DISCORD_WEBHOOK_URL is not set")
        send_to_discord(embeds)
        print(f"Sent {len(embeds)} new posts to Discord.")
    else:
        print("No new posts.")

    save_state(state)


if __name__ == "__main__":
    main()
