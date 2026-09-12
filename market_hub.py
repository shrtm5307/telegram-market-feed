"""Public market snapshots. Standard library only; no login/cookie scraping."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UTC = timezone.utc
SYMBOLS = ("NVDA", "MU", "AVGO", "AMD", "TSM", "SMH", "SOXX")
NEWS_SYMBOLS = ("NVDA", "MU", "AVGO", "AMD", "TSM", "SMH", "SOXX", "SKHY", "SNDK", "WDC", "STX")
TICKERTICK_QUERIES = (
    ("watchlist", "(or " + " ".join(f"tt:{symbol.lower()}" for symbol in NEWS_SYMBOLS) + ")"),
    ("curated_market", "(or T:curated T:market T:trade T:industry)"),
    ("analysis", "T:analysis"),
    ("disclosures", "(or T:earning T:sec)"),
)
TICKERTICK_HOST = "api.tickertick.com"
SIGNALS = ("ai_semiconductor", "memory_hbm", "optical_networking", "power_infrastructure", "macro_rates_risk")
MAX_BYTES = 8_000_000


class DataError(ValueError):
    pass


def now():
    return datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise DataError("timestamp_not_string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DataError("invalid_timestamp") from None
    if dt.tzinfo is None:
        raise DataError("timezone_required")
    return dt.astimezone(UTC)


def fresh(value, clock, hours):
    age = (clock - timestamp(value)).total_seconds()
    if age < -300:
        raise DataError("future_timestamp")
    if age > hours * 3600:
        raise DataError("stale_data")


def number(value, nullable=False, integer=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError("invalid_number")
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        raise DataError("invalid_number")
    return int(value) if integer else float(value)


def safe_text(value, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DataError("invalid_text")
    return " ".join(value.split())


def public_url(value):
    if not isinstance(value, str):
        raise DataError("invalid_url")
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.query:
        raise DataError("public_https_url_without_query_required")
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def secret_scan(data):
    # Check decoded strings, never print values or exception payloads.
    secrets = [v for k, v in os.environ.items()
               if v and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|API_HASH|SESSION_STRING", k)]
    forbidden = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|-----BEGIN .*PRIVATE KEY-----)")
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.fullmatch(r"(?i)(token|cookie|authorization|password|api_key|api_hash|session_string)", key):
                    raise DataError("sensitive_field")
                walk(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            if forbidden.search(value) or any(secret in value for secret in secrets):
                raise DataError("sensitive_value")
    walk(data)


def read_json(path):
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            raise DataError("oversized_json")
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          parse_constant=lambda _: (_ for _ in ()).throw(DataError("nonfinite_json")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DataError("missing_or_malformed_json") from None


def write_json(path, data):
    secret_scan(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not commit a new fetch timestamp when the data itself is unchanged.
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in {"timestamp", "fetched_at", "checked_at"}}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    if path.exists() and stable(read_json(path)) == stable(data):
        return False
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return True


def parse_tickertick(data, clock, hours=24):
    """Normalize TickerTick responses while retaining only recent link metadata."""
    if not isinstance(data, list):
        raise DataError("news_schema_changed")
    result, seen_id, seen_url, seen_story = [], set(), set(), set()
    for item in data:
        if not isinstance(item, dict):
            raise DataError("invalid_news_item")
        title = safe_text(item.get("title"))
        millis = number(item.get("time"))
        try:
            published = datetime.fromtimestamp(millis / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            raise DataError("invalid_timestamp") from None
        if published > clock + timedelta(minutes=5):
            raise DataError("future_news")
        if published < clock - timedelta(hours=hours):
            continue
        # Remove all query parameters and fragments before publishing. They are not
        # needed for attribution and can carry tracking or signed values.
        raw_url = item.get("url")
        if not isinstance(raw_url, str):
            raise DataError("invalid_url")
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise DataError("invalid_url")
        url = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
        if item.get("id") is None:
            raise DataError("invalid_news_id")
        news_id = safe_text(str(item["id"]), 100)
        source = safe_text(item.get("site"), 100)
        story = hashlib.sha256((source.casefold() + title.casefold()).encode()).hexdigest()
        if news_id in seen_id or url in seen_url or story in seen_story:
            continue
        raw_tickers = item.get("tickers", item.get("tags", []))
        if not isinstance(raw_tickers, list):
            raise DataError("invalid_tickers")
        tickers = sorted({ticker.upper() for ticker in raw_tickers
                          if isinstance(ticker, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,9}", ticker)})
        result.append({"news_id": news_id, "title": title, "published_at": iso(published),
                       "source": source,
                       "category": item.get("_category") if item.get("_category") in {q[0] for q in TICKERTICK_QUERIES} else None,
                       "tickers": tickers, "summary": None, "url": url, "fetched_at": iso(clock)})
        seen_id.add(news_id)
        seen_url.add(url)
        seen_story.add(story)
    if not result:
        raise DataError("empty_recent_news")
    result.sort(key=lambda item: (item["published_at"], item["news_id"]), reverse=True)
    latest = timestamp(result[0]["published_at"])
    source_lag_minutes = max(0, int((clock - latest).total_seconds() // 60))
    if source_lag_minutes <= 15:
        freshness_status = "ok"
    elif source_lag_minutes <= 60:
        freshness_status = "delayed"
    else:
        freshness_status = "stale"
    return {"timestamp": iso(clock), "status": "ok", "window_hours": hours,
            "source": "TickerTick", "freshness_status": freshness_status,
            "source_lag_minutes": source_lag_minutes,
            "latest_published_at": iso(latest), "count": len(result), "items": result}


def fetch_tickertick():
    """Fetch documented public feeds. No API key, cookie or article body is used."""
    items = []
    for category, query in TICKERTICK_QUERIES:
        url = "https://" + TICKERTICK_HOST + "/feed?" + urlencode({"q": query, "n": 200})
        try:
            with urlopen(Request(url, headers={"User-Agent": "telegram-market-feed/1.0", "Accept": "application/json"}), timeout=30) as response:
                redirected = urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname != TICKERTICK_HOST or redirected.path != "/feed":
                    raise DataError("redirect_not_approved")
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise DataError("oversized_response")
                decoded = json.loads(raw)
        except (HTTPError, URLError, UnicodeError, json.JSONDecodeError):
            raise DataError("source_http_or_json_error") from None
        if isinstance(decoded, dict):
            decoded = decoded.get("stories", decoded.get("items"))
        if not isinstance(decoded, list):
            raise DataError("news_schema_changed")
        for item in decoded:
            if not isinstance(item, dict):
                raise DataError("invalid_news_item")
            item = dict(item)
            item["_category"] = category
            items.append(item)
    return items


def normalize_options(data, symbol, clock):
    """One row per expiration/strike; call and put prices/IV stay distinct."""
    if symbol not in SYMBOLS or not isinstance(data, dict) or data.get("symbol") != symbol:
        raise DataError("symbol_mismatch")
    fresh(data.get("as_of"), clock, 24)  # Weekends can be unavailable; never imply old quotes are live.
    spot = number(data.get("spot_price"))
    if spot <= 0 or not isinstance(data.get("contracts"), list) or not data["contracts"]:
        raise DataError("empty_option_chain")
    rows, seen = {}, set()
    for contract in data["contracts"]:
        if not isinstance(contract, dict):
            raise DataError("invalid_contract")
        expiration = timestamp(contract.get("expiration"))
        if expiration <= clock:
            continue
        strike = number(contract.get("strike"))
        side = contract.get("side")
        if strike <= 0 or side not in {"call", "put"}:
            raise DataError("invalid_contract")
        key = (iso(expiration), strike)
        identity = key + (side,)
        if identity in seen:
            raise DataError("duplicate_contract")
        seen.add(identity)
        row = rows.setdefault(key, {"expiration": key[0], "strike": strike, "spot_price": spot,
                                    **{f"{s}_{f}": None for s in ("call", "put") for f in ("volume", "open_interest", "implied_volatility", "bid", "ask")}})
        for field in ("volume", "open_interest", "implied_volatility", "bid", "ask"):
            row[f"{side}_{field}"] = number(contract.get(field), nullable=True, integer=field in {"volume", "open_interest"})
        bid, ask = row[f"{side}_bid"], row[f"{side}_ask"]
        if bid is not None and ask is not None and bid > ask:
            raise DataError("crossed_quote")
    if not rows:
        raise DataError("no_unexpired_contracts")
    return {"symbol": symbol, "as_of": iso(timestamp(data["as_of"])), "spot_price": spot,
            "status": "ok", "rows": [rows[k] for k in sorted(rows)],
            "coverage": "all supplied unexpired contracts; not necessarily the full market"}


def summarize_options(chain, clock):
    rows = chain["rows"]
    def total(field, selected=rows):
        values = [row[field] for row in selected]
        return sum(values) if values and all(v is not None for v in values) else None
    def ratio(a, b):
        return a / b if a is not None and b is not None and b > 0 else None
    result = {"status": "ok", "as_of": chain["as_of"], "spot": chain["spot_price"],
              "quote_age_seconds": max(0, int((clock - timestamp(chain["as_of"])).total_seconds())),
              "coverage": chain["coverage"]}
    for side in ("call", "put"):
        result[f"total_{side}_volume"] = total(f"{side}_volume")
        result[f"total_{side}_oi"] = total(f"{side}_open_interest")
        for field, name in (("open_interest", "oi"), ("volume", "volume")):
            ranked = sorted((r for r in rows if r[f"{side}_{field}"] is not None),
                            key=lambda r: (-r[f"{side}_{field}"], r["expiration"], r["strike"]))[:5]
            result[f"top_{side}_{name}_strikes"] = [{"expiration": r["expiration"], "strike": r["strike"], "value": r[f"{side}_{field}"]} for r in ranked]
    for field in ("volume", "oi"):
        result[f"put_call_{field}_ratio"] = ratio(result[f"total_put_{field}"], result[f"total_call_{field}"])
    expirations = sorted({r["expiration"] for r in rows})
    target = clock + timedelta(days=7)
    chosen = min(expirations, key=lambda exp: abs((timestamp(exp) - target).total_seconds()))
    atm = min((r for r in rows if r["expiration"] == chosen), key=lambda r: (abs(r["strike"] - chain["spot_price"]), r["strike"]))
    ivs = [atm[f"{side}_implied_volatility"] for side in ("call", "put")]
    iv = sum(ivs) / 2 if all(v is not None and v > 0 for v in ivs) else None
    days = (timestamp(chosen) - clock).total_seconds() / 86400
    move = iv * math.sqrt(days / 365) if iv is not None and days <= 14 else None
    result.update(atm_iv=iv, nearest_atm_strike=atm["strike"], expected_move_expiration=chosen,
                  expected_move_days=days, expected_move_pct=100 * move if move is not None else None,
                  expected_move_absolute=chain["spot_price"] * move if move is not None else None,
                  expected_move_method="mean call/put ATM annualized IV * sqrt(actual calendar days / 365); one-sigma estimate, not a forecast")
    result["unusual_strikes"] = [{"expiration": r["expiration"], "strike": r["strike"], "side": side,
                                  "volume_oi_ratio": r[f"{side}_volume"] / r[f"{side}_open_interest"]}
                                 for r in rows for side in ("call", "put")
                                 if r[f"{side}_volume"] is not None and r[f"{side}_open_interest"] is not None
                                 and r[f"{side}_volume"] >= 100 and r[f"{side}_open_interest"] > 0
                                 and r[f"{side}_volume"] / r[f"{side}_open_interest"] >= 3]
    all_oi = None if any(result[f"total_{s}_oi"] is None for s in ("call", "put")) else sum(result[f"total_{s}_oi"] for s in ("call", "put"))
    concentrations = []
    for exp in expirations:
        selected = [r for r in rows if r["expiration"] == exp]
        call, put = total("call_open_interest", selected), total("put_open_interest", selected)
        concentrations.append({"expiration": exp, "call_oi": call, "put_oi": put,
                               "share": ratio(call + put, all_oi) if call is not None and put is not None else None})
    result["oi_concentration"] = concentrations
    result["interpretation"] = "OI concentration only; dealer direction and signed GEX unknown. Rankings are not confirmed call/put walls. Volume/OI compares intraday flow with last published OI."
    return result


def build_market(root, clock):
    root = Path(root)
    result = {"timestamp": iso(clock), "status": "ok", "signals": dict.fromkeys(SIGNALS)}
    errors = []
    for name, path, hours in (("telegram", "latest_snapshot.json", 48),
                              ("news", "data/news/latest_news.json", 24)):
        try:
            if name == "news" and read_json(root / "data/health/tickertick.json").get("status") != "ok":
                raise DataError("last_collection_failed")
            data = read_json(root / path)
            if data.get("status") not in {"ok", "running", "backfilled"}:
                raise DataError("source_unavailable")
            if not isinstance(data.get("items"), list) or not data["items"] or data.get("count") != len(data["items"]):
                raise DataError("empty_or_count_mismatch")
            field = "date_utc" if name == "telegram" else "published_at"
            times = [timestamp(item.get(field)) for item in data["items"]]
            latest = iso(max(times))
            fresh(latest, clock, hours)
            fresh(data.get("timestamp"), clock, hours)
            result[name] = {"status": "ok", "source_file": path, "source_timestamp": data["timestamp"],
                            "latest_item_time": latest, "item_count": len(times)}
        except (DataError, AttributeError, TypeError):
            errors.append(name)
            result[name] = {"status": "unavailable", "source_file": path, "latest_item_time": None, "item_count": None}
    try:
        if read_json(root / "data/health/options.json").get("status") != "ok":
            raise DataError("last_collection_failed")
        data = read_json(root / "data/options/options_summary.json")
        if data.get("status") != "ok" or set(data.get("symbols", {})) != set(SYMBOLS):
            raise DataError("options_unavailable")
        for symbol in SYMBOLS:
            if data["symbols"][symbol].get("status") != "ok":
                raise DataError("symbol_unavailable")
            fresh(data["symbols"][symbol].get("as_of"), clock, 24)
        result["options"] = {"status": "ok", "source_file": "data/options/options_summary.json", "symbols": data["symbols"]}
    except (DataError, AttributeError, TypeError):
        errors.append("options")
        result["options"] = {"status": "unavailable", "symbols": list(SYMBOLS)}
    if errors:
        result["status"] = "degraded"
    result["unavailable_sources"] = errors
    return result


def get_public_json(url):
    url = public_url(url)
    # Explicit HTTPS, no URL credentials/query secrets, bounded response, no cookies.
    host = urlsplit(url).hostname
    if host != "raw.githubusercontent.com":
        raise DataError("unapproved_source_host")
    try:
        with urlopen(Request(url, headers={"User-Agent": "MarketSnapshotBot/1.0", "Accept": "application/json"}), timeout=30) as response:
            if response.geturl() != url:
                raise DataError("redirect_not_approved")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise DataError("oversized_response")
            return json.loads(raw)
    except (HTTPError, URLError, UnicodeError, json.JSONDecodeError):
        raise DataError("source_http_or_json_error") from None


def collect(kind, root, config, clock):
    cfg = config[kind]
    permitted = cfg.get("enabled") is True and cfg.get("automated_collection_permitted") is True and cfg.get("public_redistribution_permitted") is True and cfg.get("policy_evidence_url")
    status = {"timestamp": iso(clock), "status": "unavailable", "reason": "source_not_authorized", "source": kind}
    try:
        if not permitted:
            raise DataError("source_not_authorized")
        if kind == "tickertick":
            result = parse_tickertick(fetch_tickertick(), clock, hours=24)
            write_json(Path(root) / "data/news/latest_news.json", result)
            status.update(freshness_status=result["freshness_status"],
                          source_lag_minutes=result["source_lag_minutes"],
                          latest_published_at=result["latest_published_at"])
        else:
            summaries, chains = {}, {}
            for symbol in SYMBOLS:
                # URL must be explicitly configured per symbol; never probe guessed endpoints.
                chain = normalize_options(get_public_json(cfg["public_json_urls"][symbol]), symbol, clock)
                chains[symbol] = chain
                summaries[symbol] = summarize_options(chain, clock)
            secret_scan(chains)
            secret_scan(summaries)
            for symbol, chain in chains.items():
                write_json(Path(root) / f"data/options/{symbol}.json", chain)
            write_json(Path(root) / "data/options/options_summary.json", {"timestamp": iso(clock), "status": "ok", "symbols": summaries})
        status.update(status="ok", reason=None)
    except (DataError, KeyError, TypeError) as exc:
        # Only emit our fixed error identifiers, never upstream body or URLs.
        status["reason"] = str(exc) if isinstance(exc, DataError) else "source_configuration_error"
    write_json(Path(root) / f"data/health/{kind}.json", status)
    if status["status"] != "ok":
        print(f"::warning::{kind}: {status['reason']}; previous data retained, health is unavailable")
        return 2
    if status.get("freshness_status") in {"delayed", "stale"}:
        print(f"::warning::{kind}: provider freshness is {status['freshness_status']} "
              f"({status['source_lag_minutes']} minutes behind)")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["tickertick", "options", "market", "scan"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="market_sources.json")
    args = parser.parse_args()
    if args.command == "scan":
        for path in Path(args.root).rglob("*.json"):
            secret_scan(read_json(path))
        return 0
    if args.command == "market":
        data = build_market(args.root, now())
        write_json(Path(args.root) / "data/market/market_snapshot.json", data)
        if data["status"] != "ok":
            print("::warning::Market snapshot is degraded: " + ", ".join(data["unavailable_sources"]))
            return 2
        return 0
    return collect(args.command, args.root, read_json(args.config), now())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("::error::Market hub failed validation; no raw error or credentials logged")
        sys.exit(1)
