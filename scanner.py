"""eBay snipe scanner: finds underpriced listings from watchlist.yaml and sends Telegram alerts.

Env vars:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   eBay developer keys (production keyset)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID Telegram bot + your chat id
  DRY_RUN=1                            print alerts instead of sending them
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


# ---------- pure logic (tested in test_scanner.py) ----------

def total_cost(item):
    """Price (or current bid) plus the cheapest listed shipping. None if no price."""
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
    r.raise_for_status()
    return r.json()["access_token"]


def search(token, watch, settings):
    cap = max_buy(watch, settings)
    if cap <= 0:
        return []
    filters = [
        f"price:[{watch.get('min_price', settings['min_price'])}..{cap}]",
        "priceCurrency:USD",
        "buyingOptions:{FIXED_PRICE|AUCTION}",
        "itemLocationCountry:US",
    ]
    if settings.get("condition_ids"):
        filters.append("conditionIds:{" + "|".join(str(c) for c in settings["condition_ids"]) + "}")
    queries = watch["query"] if isinstance(watch["query"], list) else [watch["query"]]
    items = []
    for q in queries:
        params = {"q": q, "filter": ",".join(filters), "sort": "newlyListed",
                  "limit": settings["results_per_search"]}
        if watch.get("category_id"):
            params["category_ids"] = str(watch["category_id"])
        r = requests.get(SEARCH_URL, params=params, timeout=30,
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        r.raise_for_status()
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
    r.raise_for_status()


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
    config = yaml.safe_load((ROOT / "watchlist.yaml").read_text())
    settings, watches = config["settings"], config["watches"]
    dry = os.environ.get("DRY_RUN") == "1"
    seen = load_seen()
    token = ebay_token()
    sent = errors = 0

    for watch in watches:
        try:
            items = search(token, watch, settings)
        except requests.RequestException as e:
            errors += 1
            print(f"[error] {watch['name']}: {e}", file=sys.stderr)
            continue
        for item in items:
            if item.get("itemId") in seen:
                continue
            alert = evaluate(item, watch, settings)
            if not alert:
                continue
            seen[alert["id"]] = datetime.now(timezone.utc).isoformat()
            if sent >= settings["max_alerts_per_run"]:
                continue
            text = format_alert(alert)
            if dry:
                print(text + "\n")
            else:
                send_telegram(text, alert["image"])
                time.sleep(1)
            sent += 1

    save_seen(seen)
    print(f"searches={len(watches)} alerts={sent} errors={errors}")
    if errors == len(watches):
        sys.exit(1)


if __name__ == "__main__":
    main()
