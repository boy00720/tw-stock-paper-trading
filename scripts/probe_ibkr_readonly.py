#!/usr/bin/env python3
import argparse
import json
import math
from datetime import datetime, timezone

from ib_insync import IB, Stock


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def mask_account(a):
    if not a:
        return "***"
    return f"{a[:2]}***{a[-2:]}" if len(a) >= 4 else "***"


def has_price(q):
    return any(q.get(k) is not None for k in ["bid", "ask", "last", "close", "market_price"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=201)
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--test-live", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refuse non-local IBKR host")

    ib = IB()
    errors = []

    def on_error(req_id, code, msg, contract):
        errors.append({"req_id": req_id, "code": code, "message": msg})

    ib.errorEvent += on_error

    result = {
        "ok": False,
        "asof": now(),
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
        "connected": False,
        "account": {},
        "quotes": {},
        "warnings": [],
    }

    def fetch_quote(market_data_type):
        start = len(errors)
        name = {1: "live", 3: "delayed"}.get(market_data_type, str(market_data_type))

        ib.reqMarketDataType(market_data_type)
        c = Stock(args.symbol, "SMART", "USD")
        ib.qualifyContracts(c)
        t = ib.reqMktData(c, "", False, False)
        ib.sleep(6)

        stage_errors = errors[start:]
        q = {
            "symbol": args.symbol,
            "market_data_type": name,
            "bid": clean(t.bid),
            "ask": clean(t.ask),
            "last": clean(t.last),
            "close": clean(t.close),
            "market_price": clean(t.marketPrice()),
            "errors": stage_errors,
            "warnings": [],
        }

        if any(e.get("code") == 10089 for e in stage_errors):
            q["permission_status"] = "no_live_subscription"
            q["is_realtime"] = False
            q["warnings"].append("Error 10089: live market data subscription required")
        elif has_price(q):
            q["permission_status"] = "ok"
            q["is_realtime"] = market_data_type == 1
        else:
            q["permission_status"] = "unavailable"
            q["is_realtime"] = False
            q["warnings"].append("quote fields are empty")

        try:
            ib.cancelMktData(c)
        except Exception:
            pass

        return q

    try:
        ib.connect(args.host, args.port, clientId=args.client_id, readonly=True, timeout=10)
        result["connected"] = ib.isConnected()

        values = ib.accountValues()
        accounts = sorted({v.account for v in values if v.account})
        result["account"] = {
            "account_values_count": len(values),
            "accounts_masked": [mask_account(a) for a in accounts],
        }

        result["quotes"]["delayed"] = fetch_quote(3)

        if args.test_live:
            result["quotes"]["live"] = fetch_quote(1)
            if result["quotes"]["live"].get("permission_status") == "no_live_subscription":
                result["warnings"].append("live market data subscription required")

        result["ok"] = (
            result["connected"]
            and result["quotes"]["delayed"].get("permission_status") == "ok"
        )

    except Exception as e:
        result["error"] = type(e).__name__
        result["error_message"] = str(e)
        result["warnings"].append("probe failed")
    finally:
        if ib.isConnected():
            ib.disconnect()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("IBKR read-only probe")
        print("=" * 24)
        print("ok:", result["ok"])
        print("connected:", result["connected"])
        print("readonly:", result["readonly"])
        print("account:", result.get("account"))

        for name, q in result.get("quotes", {}).items():
            print()
            print("Quote:", name)
            print("-" * 24)
            print("permission_status:", q.get("permission_status"))
            print("is_realtime:", q.get("is_realtime"))
            print("bid:", q.get("bid"))
            print("ask:", q.get("ask"))
            print("last:", q.get("last"))
            print("close:", q.get("close"))
            print("market_price:", q.get("market_price"))
            if q.get("warnings"):
                print("warnings:", q.get("warnings"))
            if q.get("errors"):
                print("errors:", [{"code": e.get("code"), "message": e.get("message")} for e in q.get("errors")])

        if result.get("warnings"):
            print()
            print("warnings:", result["warnings"])


if __name__ == "__main__":
    main()
