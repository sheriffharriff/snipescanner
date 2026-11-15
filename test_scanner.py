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

# ---- brand, gender, size, balance ----

def test_brand_must_be_in_title():
    w = W["Arc'teryx no model named"]
    assert s.evaluate(item("Green SLAM yellow gortex jacket", 30), w, ST, NOW) is None
    assert s.evaluate(item("Arc’teryx gore-tex jacket mens L", 30), w, ST, NOW)

def test_detect_gender():
    assert s.detect_gender("Arc'teryx Beta AR Women’s Medium") == "women"
    assert s.detect_gender("Arcteryx Beta AR Womens M") == "women"
    assert s.detect_gender("Arcteryx Beta AR W's small") == "women"
    assert s.detect_gender("Arcteryx Beta AR Men's L") == "men"
    assert s.detect_gender("Arcteryx Beta AR mens large") == "men"
    assert s.detect_gender("Arcteryx Beta AR jacket L") is None

def test_detect_size():
    cases = {"Arcteryx Beta AR Men's L": "L", "Beta LT jacket womens medium": "M",
             "Alpha SV Size S": "S", "Arc'teryx Atom Men's XL black": "XL",
             "Retro-X fleece X-Large": "XL", "jacket mens x-small": "XS", "Beta AR XXL": "XXL",
             "Beta SL jacket 2XL": "XXL", "Carhartt Detroit jacket 44": None,
             "Arc'teryx Beta AR Men's jacket": None, "Women's Beta AR XS": "XS", "atom lt sz m": "M"}
    for title, want in cases.items():
        assert s.detect_size(title) == want, title

def test_womens_and_off_size_lower_expected_sale():
    w = W["Arc'teryx Beta AR"]
    men = s.evaluate(item("Arcteryx Beta AR mens L", 100), w, ST, NOW)
    women = s.evaluate(item("Arcteryx Beta AR womens M", 100), w, ST, NOW)
    off = s.evaluate(item("Arcteryx Beta AR mens XXL", 100), w, ST, NOW)
    assert men["expected_sale"] == 300 and men["best_size"] and men["gender"] == "men"
    assert women["expected_sale"] == 255 and women["best_size"] and women["gender"] == "women"
    assert off["expected_sale"] == 255 and not off["best_size"]
    # a women's piece that only cleared the bar at the men's price is now skipped
    assert s.evaluate(item("Arcteryx Beta AR mens L", 160), w, ST, NOW)
    assert s.evaluate(item("Arcteryx Beta AR womens M", 160), w, ST, NOW) is None
    assert "Women's, M (top size)" in s.format_alert(women)

def cand(i, gender, profit, best=False):
    return {"id": str(i), "gender": gender, "profit": profit, "best_size": best}

def test_select_balances_gender():
    st = dict(ST, max_alerts_per_run=10, max_gender_lead=4, always_alert_profit=100)
    women = [cand(i, "women", 50) for i in range(8)]
    chosen, lead = s.select_alerts(women, 0, st)
    assert len(chosen) == 4 and lead == 4                    # women's pause at a lead of 4
    chosen, lead = s.select_alerts(women, 4, st)
    assert chosen == [] and lead == 4
    mixed = women + [cand(100 + i, "men", 40) for i in range(3)]
    chosen, lead = s.select_alerts(mixed, 4, st)             # men's alerts free up women's
    assert sum(c["gender"] == "men" for c in chosen) == 3
    assert sum(c["gender"] == "women" for c in chosen) == 3 and lead == 4
    big = [cand(200, "women", 150)]
    chosen, lead = s.select_alerts(big, 4, st)               # big profit always goes through
    assert len(chosen) == 1 and lead == 4
    chosen, _ = s.select_alerts([cand(1, None, 30), cand(2, "men", 30, best=True), cand(3, "men", 90)], 0, st)
    assert [c["id"] for c in chosen] == ["2", "3", "1"]      # top size first, then profit

def test_select_respects_cap():
    st = dict(ST, max_alerts_per_run=3, max_gender_lead=4, always_alert_profit=100)
    chosen, _ = s.select_alerts([cand(i, None, 30 + i) for i in range(9)], 0, st)
    assert len(chosen) == 3 and chosen[0]["profit"] == 38
