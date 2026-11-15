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


# ---------- pure logic (tested in test_scanner.py) ----------

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
    title = (item.get("title") or "").lower()

    for word in settings.get("exclude_words", []) + watch.get("exclude_words", []):
        if word.lower() in title:
            return None
    required = watch.get("require_any", [])
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

    sale = float(watch["expected_sale"])
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


def format_alert(a):
    kind = (f"AUCTION, {a['hours_left']}h left, {a['bids'] or 0} bids" if a["auction"] else "BUY IT NOW")
    return (
        f"<b>{html.escape(a['watch'])}</b> | {kind}\n"
        f"{html.escape(a['title'])}\n"
        f"Cost with shipping: <b>${a['cost']:.2f}</b>\n"
        f"Expected sale: ${a['expected_sale']:.0f} | Est. profit: <b>${a['profit']:.2f}</b> ({a['roi_pct']}%)\n"
        f"Condition: {html.escape(a['condition'])}\n"
        f"{a['url']}"
    )


# ---------- network ----------

def ebay_token():
    creds = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}"
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
    base = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    chat = os.environ["TELEGRAM_CHAT_ID"]
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
    kept = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_FILE.write_text(json.dumps(kept, indent=0, sort_keys=True))


def main():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        # Not an error: the repo is simply not configured yet. Exit cleanly so the
        # schedule does not send a failure email every 10 minutes.
        print(f"::warning::Scanner skipped. Missing secrets: {', '.join(missing)}")
        return

    config = yaml.safe_load((ROOT / "watchlist.yaml").read_text())
    settings, watches = config["settings"], config["watches"]
    dry = os.environ.get("DRY_RUN") == "1"
    seen = load_seen()
    token = ebay_token()
    now = datetime.now(timezone.utc)
    passes = [False]
    if auction_pass_due(seen, now):
        passes.append(True)
        seen[AUCTION_PASS_KEY] = now.isoformat()
    sent = errors = searches = 0

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
                if not alert:
                    continue
                if sent >= settings["max_alerts_per_run"]:
                    continue          # not marked seen, so it is retried next run
                seen[alert["id"]] = datetime.now(timezone.utc).isoformat()
                text = format_alert(alert)
                if dry:
                    print(text + "\n")
                else:
                    send_telegram(text, alert["image"])
                    time.sleep(1)
                sent += 1

    save_seen(seen)
    summary = f"watch searches={searches} alerts={sent} errors={errors} auction_pass={len(passes) == 2}"
    print(summary)
    if os.environ.get("SEND_SUMMARY") == "1" and not dry:
        send_telegram(f"Scanner is working.\n{summary}")
    if errors == searches:
        print("::error::Every eBay search failed. See the [error] lines above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
