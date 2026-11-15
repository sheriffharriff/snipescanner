from datetime import datetime, timezone
import yaml
from pathlib import Path
import scanner as s

CFG = yaml.safe_load((Path(__file__).parent / "watchlist.yaml").read_text())
ST = CFG["settings"]
W = {w["name"]: w for w in CFG["watches"]}
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)

def item(title, price, ship=0, opts=("FIXED_PRICE",), end=None, bid=None):
    d = {"itemId": "v1|1|0", "title": title, "buyingOptions": list(opts),
         "shippingOptions": [{"shippingCost": {"value": str(ship)}}],
         "itemWebUrl": "https://www.ebay.com/itm/1", "condition": "Pre-owned"}
    if bid is not None: d["currentBidPrice"] = {"value": str(bid)}
    else: d["price"] = {"value": str(price)}
    if end: d["itemEndDate"] = end
    return d

def test_config_loads_and_caps_positive():
    for w in CFG["watches"]:
        assert s.max_buy(w, ST) > ST["min_price"], w["name"]

def test_cheap_beta_ar_alerts():
    a = s.evaluate(item("Arcteryx Beta AR jacket mens M", 90, 10), W["Arc'teryx Beta AR"], ST, NOW)
    assert a and a["cost"] == 100 and a["profit"] == 148.0   # 300*0.86-10-100

def test_fair_priced_skipped():
    assert s.evaluate(item("Arcteryx Beta AR jacket", 230), W["Arc'teryx Beta AR"], ST, NOW) is None

def test_exclude_and_require():
    w = W["Arc'teryx Beta AR"]
    assert s.evaluate(item("Arcteryx Beta AR jacket kids", 50), w, ST, NOW) is None
    assert s.evaluate(item("Arcteryx Beta SL jacket", 50), w, ST, NOW) is None

def test_vague_watch_skips_named_models():
    w = W["Arc'teryx no model named"]
    assert s.evaluate(item("Arcteryx gore-tex jacket green L", 40), w, ST, NOW)
    assert s.evaluate(item("Arcteryx Beta gore-tex jacket", 40), w, ST, NOW) is None

def test_misspelling_only():
    w = W["Carhartt misspelled"]
    assert s.evaluate(item("Carhart jacket brown XL", 20), w, ST, NOW)
    assert s.evaluate(item("Carhartt jacket brown XL", 20), w, ST, NOW) is None

def test_auction_window():
    w = W["Arc'teryx Beta LT"]
    soon = item("Arcteryx Beta LT", 0, 0, ("AUCTION",), "2026-11-12T07:30:00.000Z", bid=30)
    late = item("Arcteryx Beta LT", 0, 0, ("AUCTION",), "2026-11-13T07:30:00.000Z", bid=30)
    a = s.evaluate(soon, w, ST, NOW)
    assert a and a["auction"] and a["hours_left"] == 1.5
    assert s.evaluate(late, w, ST, NOW) is None
    assert "AUCTION" in s.format_alert(a)

def test_roi_floor():
    # Retro-X: net 85*0.86-10 = 63.1; at cost 40 profit 23.1 < 25 -> skip; at 30 -> alert
    w = W["Patagonia Retro-X"]
    assert s.evaluate(item("Patagonia Retro-X fleece", 40), w, ST, NOW) is None
    assert s.evaluate(item("Patagonia Retro-X fleece", 30), w, ST, NOW)

def test_auction_uses_current_bid_not_price():
    it = item("Arcteryx Beta LT", 0, 5, ("AUCTION",), "2026-11-12T07:00:00.000Z", bid=40)
    it["price"] = {"value": "999"}
    assert s.total_cost(it) == 45

def test_auction_pass_due():
    assert s.auction_pass_due({}, NOW)
    assert not s.auction_pass_due({s.AUCTION_PASS_KEY: "2026-11-12T05:30:00+00:00"}, NOW)
    assert s.auction_pass_due({s.AUCTION_PASS_KEY: "2026-11-12T05:00:00+00:00"}, NOW)

def test_missing_secrets_exits_cleanly(monkeypatch, capsys):
    for k in s.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    s.main()
    assert "Missing secrets" in capsys.readouterr().out

def test_full_run_with_fake_ebay(monkeypatch, tmp_path, capsys):
    """End to end with eBay and Telegram stubbed: one BIN deal, one auction deal, one dud."""
    for k in s.REQUIRED_ENV:
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("SEND_SUMMARY", "1")
    monkeypatch.setattr(s, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(s, "ebay_token", lambda: "tok")
    monkeypatch.setattr(s.time, "sleep", lambda _: None)
    from datetime import timedelta
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    calls, sent = [], []
    def fake_search(token, watch, settings, auctions=False):
        calls.append((watch["name"], auctions))
        if watch["name"] != "Arc'teryx Beta AR":
            return []
        if auctions:
            a = item("Arcteryx Beta AR auction", 0, 0, ("AUCTION",), soon, bid=60); a["itemId"] = "A"
            return [a]
        good = item("Arcteryx Beta AR jacket", 90, 10); good["itemId"] = "B"
        dud = item("Arcteryx Beta AR jacket", 160, 10); dud["itemId"] = "C"
        return [good, dud]
    monkeypatch.setattr(s, "search", fake_search)
    monkeypatch.setattr(s, "send_telegram", lambda text, image=None: sent.append(text))
    s.main()
    n = len(CFG["watches"])
    assert len(calls) == 2 * n                       # BIN pass + auction pass
    assert len(sent) == 3 and "Scanner is working" in sent[-1]
    seen = s.load_seen()
    assert "A" in seen and "B" in seen and "C" not in seen
    # second run right away: nothing new, no auction pass
    calls.clear(); sent.clear()
    s.main()
    assert len(calls) == n and sent == ["Scanner is working.\n" + capsys.readouterr().out.strip().splitlines()[-1]]

def test_bad_ebay_keys(monkeypatch, capsys):
    import pytest
    for k in s.REQUIRED_ENV:
        monkeypatch.setenv(k, "x")
    def boom(): raise s.EbayError("eBay sign-in failed (401)")
    monkeypatch.setattr(s, "ebay_token", boom)
    monkeypatch.setenv("SEND_SUMMARY", "1")
    with pytest.raises(SystemExit) as e: s.main()
    assert e.value.code == 1 and "::error::" in capsys.readouterr().out
    monkeypatch.setenv("SEND_SUMMARY", "0")
    with pytest.raises(SystemExit) as e: s.main()
    assert e.value.code == 0 and "::warning::" in capsys.readouterr().out
