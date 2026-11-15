"""eBay snipe scanner: finds underpriced listings from watchlist.yaml and sends Telegram alerts.

Env vars:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   eBay developer keys (production keyset)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID Telegram bot + your chat id
  DRY_RUN=1                            print alerts instead of sending them
  SEND_SUMMARY=1                       also send a "scanner is working" message to Telegram

Each run does a Buy It Now pass (newest listings first). About once an hour it also
does an auction pass (auctions ending soonest first).
"""
import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
SEEN_DAYS = 14
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
REQUIRED_ENV = ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
AUCTION_PASS_KEY = "_last_auction_pass"
AUCTION_PASS_EVERY_MIN = 50
GENDER_LEAD_KEY = "_gender_lead"      # women's alerts sent minus men's alerts sent

WOMEN_RE = re.compile(r"\b(women|womens|woman|ladies|lady|wmns|wmn|female|w's)\b")
MEN_RE = re.compile(r"\b(men|mens|man|male|m's)\b")
# "Carhartt style", "Arcteryx like shell": another brand borrowing the name. "like new" is fine.
COMPARE_RE = re.compile(r"\b(style|inspired|similar to)\b|\blike\b(?! new)")
SIZE_PATTERNS = [   # order matters: longer sizes first
    ("XXS", r"\b(xxs|2xs|xx-small)\b"),
    ("4XL+", r"\b(4xl|5xl|6xl|4x|5x|xxxxl)\b"),
    ("3XL", r"\b(xxxl|3xl|3x)\b"),
    ("XXL", r"\b(xxl|2xl|2x|xx-large|xx large)\b"),
    ("XS", r"\b(xs|x-small|x small|extra small)\b"),
    ("XL", r"\b(xl|x-large|x large|extra large)\b"),
    ("S", r"\b(s|sm|small)\b"),
    ("M", r"\b(m|med|medium)\b"),
    ("L", r"\b(l|lg|large)\b"),
]


# ---------- pure logic (tested in test_scanner.py) ----------

def norm(title):
    """Lowercase and straighten curly apostrophes so word checks are reliable."""
    return (title or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")


def detect_gender(title):
    """'women', 'men', or None when the title does not say."""
    t = norm(title)
    if WOMEN_RE.search(t):
        return "women"
    if MEN_RE.search(t):
        return "men"
    return None


def detect_size(title):
    """Letter size (XXS to 3XL) from the title, or None."""
    t = re.sub(r"'s\b", "s", norm(title))      # "men's" -> "mens", so the "s" is not read as Small
    for size, pattern in SIZE_PATTERNS:
        if re.search(pattern, t):
            return size
    return None


def has_word(title, word):
    """True if `word` starts a word in the title: 'stain' matches 'stained', 'vest' does not match 'harvest'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()), title) is not None


def total_cost(item):
    """Price (or current bid) plus the cheapest listed shipping. None if no price."""
    if is_auction_only(item):
        price = item.get("currentBidPrice") or item.get("price")
    else:
        price = item.get("price") or item.get("currentBidPrice")
    if not price:
        return None
    cost = float(price["value"])
    options = item.get("shippingOptions") or []
    ship = [float(o["shippingCost"]["value"]) for o in options if o.get("shippingCost")]
    return cost + (min(ship) if ship else 0.0)


def is_auction_only(item):
    opts = item.get("buyingOptions") or []
    return "AUCTION" in opts and "FIXED_PRICE" not in opts


def hours_left(item, now=None):
    end = item.get("itemEndDate")
    if not end:
        return None
    now = now or datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (end_dt - now).total_seconds() / 3600


def evaluate(item, watch, settings, now=None):
    """Return an alert dict if the listing is a buy candidate, else None."""
    title = norm(item.get("title"))

    # The brand must be in the title. eBay also matches on descriptions, which lets
    # other brands through (e.g. a jacket that only mentions the brand as a comparison).
    brand = watch.get("brand", [])
    if brand and not any(b.lower() in title for b in brand):
        return None

    if COMPARE_RE.search(title):
        return None
    for word in settings.get("exclude_words", []) + watch.get("exclude_words", []):
        if has_word(title, word):
            return None
    for group in ("require_any", "must_also_have"):     # both groups must match when present
        required = watch.get(group, [])
        if required and not any(w.lower() in title for w in required):
            return None
    # "vague" watches only want listings that DON'T name a known model
    if any(w.lower() in title for w in watch.get("skip_if_title_has", [])):
        return None

    cost = total_cost(item)
    if cost is None:
        return None

    auction = is_auction_only(item)
    if auction:
        left = hours_left(item, now)
        if left is None or left <= 0 or left > settings["auction_window_hours"]:
            return None

    gender = detect_gender(title)
    size = detect_size(title)
    size_key = "women" if gender == "women" else "men"     # unlabeled listings are sized like men's
    best_size = size in settings.get("best_sizes", {}).get(size_key, [])
    off_size = size in settings.get("off_sizes", {}).get(size_key, [])

    sale = float(watch["expected_sale"])
    if gender == "women":
        sale *= settings.get("womens_sale_factor", 1.0)
    if off_size:
        sale *= settings.get("off_size_sale_factor", 1.0)
    sale = round(sale, 2)
    net_sale = sale * (1 - settings["sell_fee_pct"] / 100) - settings["ship_out_cost"]
    profit = net_sale - cost
    roi = profit / cost if cost > 0 else 0.0
    if profit < settings["min_profit"] or roi < settings["min_roi_pct"] / 100:
        return None

    return {
        "id": item["itemId"],
        "watch": watch["name"],
        "title": item.get("title", ""),
        "cost": round(cost, 2),
        "expected_sale": sale,
        "profit": round(profit, 2),
        "roi_pct": round(roi * 100),
        "auction": auction,
        "bids": item.get("bidCount"),
        "hours_left": round(hours_left(item, now), 1) if auction else None,
        "condition": item.get("condition", "?"),
        "url": item.get("itemWebUrl", ""),
        "image": (item.get("image") or {}).get("imageUrl"),
        "gender": gender,
        "size": size,
        "best_size": best_size,
    }


def max_buy(watch, settings):
    """Highest total cost that still clears both the profit and ROI floors."""
    net = float(watch["expected_sale"]) * (1 - settings["sell_fee_pct"] / 100) - settings["ship_out_cost"]
    return round(min(net - settings["min_profit"], net / (1 + settings["min_roi_pct"] / 100)), 2)


def auction_pass_due(seen, now=None):
    """True if the hourly auction pass has not run in the last AUCTION_PASS_EVERY_MIN minutes."""
    last = seen.get(AUCTION_PASS_KEY)
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(last)).total_seconds() >= AUCTION_PASS_EVERY_MIN * 60


def select_alerts(candidates, lead, settings):
    """Pick which candidates to send this run, keeping men's and women's alerts balanced.

    lead = women's alerts sent minus men's alerts sent so far. A women's alert is held
    once women's lead by max_gender_lead, and the same for men's the other way. Held
    alerts are retried on later runs. Anything with profit >= always_alert_profit is sent
    regardless. Best sizes go first, then highest profit.
    Returns (chosen, new_lead).
    """
    max_lead = settings.get("max_gender_lead", 10**9)
    always = settings.get("always_alert_profit", float("inf"))
    chosen = []
    waiting = sorted(candidates, key=lambda c: (not c["best_size"], -c["profit"]))
    progress = True
    while progress and waiting:      # repeat: a men's alert can free up a held women's one
        progress, still_waiting = False, []
        for c in waiting:
            if len(chosen) >= settings["max_alerts_per_run"]:
                break
            step = {"women": 1, "men": -1}.get(c["gender"], 0)
            if c["profit"] < always and abs(lead + step) > max_lead:
                still_waiting.append(c)
                continue
            lead = max(-max_lead, min(max_lead, lead + step))
            chosen.append(c)
            progress = True
        waiting = still_waiting
    return chosen, lead


def format_alert(a):
    kind = (f"AUCTION, {a['hours_left']}h left, {a['bids'] or 0} bids" if a["auction"] else "BUY IT NOW")
    who = {"men": "Men's", "women": "Women's"}.get(a.get("gender"), "Gender not stated")
    size = a.get("size") or "size not stated"
    star = " (top size)" if a.get("best_size") else ""
    return (
        f"<b>{html.escape(a['watch'])}</b> | {kind}\n"
        f"{html.escape(a['title'])}\n"
        f"{who}, {size}{star}\n"
        f"Cost with shipping: <b>${a['cost']:.2f}</b>\n"
        f"Expected sale: ${a['expected_sale']:.0f} | Est. profit: <b>${a['profit']:.2f}</b> ({a['roi_pct']}%)\n"
        f"Condition: {html.escape(a['condition'])}\n"
        f"{a['url']}"
    )


# ---------- network ----------

def ebay_token():
    creds = f"{os.environ['EBAY_CLIENT_ID'].strip()}:{os.environ['EBAY_CLIENT_SECRET'].strip()}"
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    if not r.ok:
        raise EbayError(f"eBay sign-in failed ({r.status_code}). Check EBAY_CLIENT_ID and "
                        f"EBAY_CLIENT_SECRET are the PRODUCTION keys. Details: {r.text[:300]}")
    return r.json()["access_token"]


class EbayError(Exception):
    pass


def search(token, watch, settings, auctions=False):
    """Buy It Now pass: newest listings. Auction pass: auctions ending soonest."""
    cap = max_buy(watch, settings)
    if cap <= 0:
        return []
    filters = [
        f"price:[{watch.get('min_price', settings['min_price'])}..{cap}]",
        "priceCurrency:USD",
        "buyingOptions:{AUCTION}" if auctions else "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
        "itemLocationCountry:US",
    ]
    if settings.get("condition_ids"):
        filters.append("conditionIds:{" + "|".join(str(c) for c in settings["condition_ids"]) + "}")
    queries = watch["query"] if isinstance(watch["query"], list) else [watch["query"]]
    items = []
    for q in queries:
        params = {"q": q, "filter": ",".join(filters),
                  "sort": "endingSoonest" if auctions else "newlyListed",
                  "limit": settings["results_per_search"]}
        if watch.get("category_id"):
            params["category_ids"] = str(watch["category_id"])
        r = requests.get(SEARCH_URL, params=params, timeout=30,
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        if not r.ok:
            raise EbayError(f"eBay search failed ({r.status_code}) for '{q}': {r.text[:300]}")
        items.extend(r.json().get("itemSummaries", []))
    return items


def send_telegram(text, image=None):
    base = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN'].strip()}"
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    if image:
        r = requests.post(f"{base}/sendPhoto", timeout=30,
                          data={"chat_id": chat, "photo": image, "caption": text[:1024], "parse_mode": "HTML"})
        if r.ok:
            return
    r = requests.post(f"{base}/sendMessage", timeout=30,
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML"})
    if not r.ok:
        raise RuntimeError(f"Telegram send failed ({r.status_code}). Check TELEGRAM_BOT_TOKEN and "
                           f"TELEGRAM_CHAT_ID, and that you have messaged your bot once. Details: {r.text[:300]}")


# ---------- state ----------

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_DAYS)).isoformat()
    kept = {k: v for k, v in seen.items() if k.startswith("_") or v > cutoff}
    SEEN_FILE.write_text(json.dumps(kept, indent=0, sort_keys=True))


def main():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing:
        # Not an error: the repo is simply not configured yet. Exit cleanly so the
        # schedule does not send a failure email every 10 minutes.
        print(f"::warning::Scanner skipped. Missing secrets: {', '.join(missing)}")
        return

    config = yaml.safe_load((ROOT / "watchlist.yaml").read_text())
    settings, watches = config["settings"], config["watches"]
    dry = os.environ.get("DRY_RUN") == "1"
    seen = load_seen()
    manual = os.environ.get("SEND_SUMMARY") == "1"
    try:
        token = ebay_token()
    except EbayError as e:
        # Fail loudly on a manual run. On the 10-minute schedule, warn and stop so a
        # wrong key does not send a failure email every 10 minutes.
        print(f"::{'error' if manual else 'warning'}::{e}")
        sys.exit(1 if manual else 0)
    now = datetime.now(timezone.utc)
    passes = [False]
    if auction_pass_due(seen, now):
        passes.append(True)
        seen[AUCTION_PASS_KEY] = now.isoformat()
    errors = searches = 0
    candidates = {}

    for auctions in passes:
        for watch in watches:
            searches += 1
            try:
                items = search(token, watch, settings, auctions=auctions)
            except (requests.RequestException, EbayError) as e:
                errors += 1
                print(f"[error] {watch['name']}: {e}", file=sys.stderr)
                continue
            for item in items:
                if item.get("itemId") in seen:
                    continue
                alert = evaluate(item, watch, settings)
                if alert and alert["profit"] > candidates.get(alert["id"], {}).get("profit", -1):
                    candidates[alert["id"]] = alert

    lead = int(seen.get(GENDER_LEAD_KEY, "0"))
    chosen, lead = select_alerts(list(candidates.values()), lead, settings)
    seen[GENDER_LEAD_KEY] = str(lead)
    for alert in chosen:            # anything not chosen stays unseen and is retried next run
        seen[alert["id"]] = datetime.now(timezone.utc).isoformat()
        text = format_alert(alert)
        if dry:
            print(text + "\n")
        else:
            send_telegram(text, alert["image"])
            time.sleep(1)
    sent = len(chosen)

    def count(rows, g):
        return sum(1 for r in rows if r["gender"] == g)
    cands = list(candidates.values())
    print(f"candidates: men={count(cands, 'men')} women={count(cands, 'women')} unstated={count(cands, None)}")
    print(f"sent:       men={count(chosen, 'men')} women={count(chosen, 'women')} unstated={count(chosen, None)}"
          f" held={len(cands) - sent} gender_lead={lead}")

    if dry:
        print("--- all candidates (dry run: nothing sent, nothing saved) ---")
        for c in sorted(cands, key=lambda c: (c["watch"], -c["profit"])):
            print(f"{c['watch'][:28]:28} | ${c['cost']:>6.2f} -> ${c['expected_sale']:>5.0f} | "
                  f"{(c['gender'] or '-'):5} {(c['size'] or '-'):3} | {'AUC' if c['auction'] else 'BIN'} | {c['title'][:90]}")
    else:
        save_seen(seen)
    summary = f"watch searches={searches} alerts={sent} errors={errors} auction_pass={len(passes) == 2}"
    print(summary)
    if manual and not dry:
        send_telegram(f"Scanner is working.\n{summary}")
    if errors == searches:
        print("::error::Every eBay search failed. See the [error] lines above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
