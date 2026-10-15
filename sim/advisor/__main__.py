"""automation/sim/advisor/__main__.py — CLI 入口（M6 區塊 E + M6.1 dispatch）

支援子命令：
- decide [--dry-run] [--force] [-v]：跑一輪 advisor 決策
  - dry_run=True：過 risk_guard 但不 submit_order；用於 W 看 prompt 質量
  - force=True：繞過 should_run_now（盤外、週末、假日仍跑）
  - 沒 --dry-run 也沒 --force：should_run_now 失敗就 noop（給 launchd 用）
- metrics [--since YYYY-MM-DD]：印四件套 + 0050 baseline 摘要到 stdout
- brief [--date YYYY-MM-DD] [--since YYYY-MM-DD] [--stdout] [--output PATH]：
  產出當日聚合 markdown
  - 預設寫入 logs/sim/daily_brief/brief_YYYY-MM-DD.md
  - --stdout 改印到終端、不寫檔
  - --output 自訂輸出路徑
- dispatch [--force] [--cancel-all-pending]：M6.1 跨進程撮合
  - 從 orders.jsonl replay PENDING orders 進記憶體
  - 跑一次 engine.match + apply_fills（LMT/STOP/TP 視 quote 撮合）
  - --cancel-all-pending：把所有 PENDING 改 CANCELLED（不撮合）；給清理孤立訂單用
  - 沒 --force：should_run_now 失敗就 noop（給 launchd 用）

執行：
    python3 -m sim.advisor decide --dry-run
    python3 -m sim.advisor decide -v --force
    python3 -m sim.advisor metrics
    python3 -m sim.advisor metrics --since 2026-04-01
    python3 -m sim.advisor brief --date 2026-05-06
    python3 -m sim.advisor brief --stdout
    python3 -m sim.advisor dispatch
    python3 -m sim.advisor dispatch --cancel-all-pending --force
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_decide(args) -> int:
    from sim.feed import QuoteFeed
    from sim.advisor.client import AdvisorClient, AdvisorClientError
    from sim.advisor.runner import (
        init_engine_from_fills,
        run_advisor_once,
        should_run_now,
    )

    if not args.dry_run and not args.force:
        if not should_run_now():
            print("[advisor] 非盤中時段或台股假日 — 本輪 noop（用 --force 繞過）")
            return 0

    try:
        client = AdvisorClient()
        client.ping()
    except AdvisorClientError as exc:
        print(f"[advisor] cc-switch / Anthropic 客戶端啟動失敗: {exc}", file=sys.stderr)
        print(
            "→ 提示：cc-switch GUI 切到不要求強制 1m context、無 server panic 的 provider",
            file=sys.stderr,
        )
        return 2

    engine = init_engine_from_fills()
    feed = QuoteFeed()

    print(f"[advisor] provider={client.provider_id} model={client.model}")
    print(
        f"[advisor] equity={engine.account.equity():.0f} "
        f"cash={engine.account.cash:.0f} "
        f"positions={len(engine.account.positions)} "
        f"realized_pnl={engine.account.total_realized_pnl:+.0f} "
        f"mode={'dry-run' if args.dry_run else 'live'}"
    )

    result = run_advisor_once(engine, feed, client, dry_run=args.dry_run)

    print(
        f"[advisor] status={result['status']} "
        f"decided={result['decided']} passed={result['passed']} "
        f"submitted={result['submitted']} blocked={result['blocked']} "
        f"hold={result['hold']}"
    )
    if result.get("circuit_triggered_this_round"):
        print("[advisor] ⚠️ 本輪觸發日內熔斷 — 至次日 09:00 paper_only")
    if result.get("summary"):
        print(f"[advisor.summary] {result['summary']}")
    if result.get("metrics_self_eval"):
        print(f"[advisor.self_eval] {result['metrics_self_eval']}")
    if result.get("error"):
        print(f"[advisor.error] {result['error']}", file=sys.stderr)

    print("\n--- decisions_log ---")
    for d in result.get("decisions_log", []):
        print(f"  {d}")

    return 0 if result["status"] == "ok" else 1


def _parse_date_or_none(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise SystemExit(f"[advisor] 日期格式錯誤（需 YYYY-MM-DD）：{s}") from exc


def _format_metrics_text(four: dict) -> str:
    wr = four.get("win_rate", {})
    pl = four.get("profit_loss", {})
    bl = four.get("baseline", {})
    rate = wr.get("rate") or 0.0
    bl_pct = bl.get("return_pct")
    bl_str = f"{bl_pct * 100:+.2f}%" if isinstance(bl_pct, (int, float)) else "—"
    ratio = pl.get("ratio", 0.0)
    ratio_str = "∞" if ratio == float("inf") else f"{ratio:.2f}"

    return "\n".join([
        f"[advisor.metrics] since={four.get('since', 'all_time')}",
        f"  勝率           : {rate * 100:.1f}% ({wr.get('win', 0)}/{wr.get('total', 0)})",
        f"  累積已實現 PnL : {four.get('cum_realized_pnl', 0.0):+,.0f}",
        f"  最大回撤       : {four.get('max_drawdown_pct', 0.0) * 100:.2f}%",
        f"  盈虧比         : {ratio_str} (avg_win={pl.get('avg_win', 0.0):,.0f} / avg_loss={pl.get('avg_loss', 0.0):,.0f})",
        f"  0050.TW baseline: {bl_str}",
    ])


def cmd_metrics(args) -> int:
    if not getattr(args, "force", False) and not getattr(args, "no_send", False):
        from sim.calendar import is_trading_day
        if not is_trading_day():
            print("[advisor.metrics] 今日非交易日（週末或台股假日）— skip（用 --force 繞過）")
            return 0
    from sim.advisor.metrics import compute_four_pillars

    since = _parse_date_or_none(getattr(args, "since", None))
    four = compute_four_pillars(since=since)
    four = {**four, "time": datetime.now().isoformat(timespec="seconds")}

    text = _format_metrics_text(four)
    print(text)

    out_path = Path("logs/sim/advisor_metrics.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(four, ensure_ascii=False) + "\n")

    if not getattr(args, "no_send", False):
        try:
            from sim._notify import send_dm
            title = f"📊 advisor 四件套 {datetime.now().strftime('%Y-%m-%d')}"
            if send_dm(title, text):
                print(f"[advisor.metrics] 已推送 DM: {title}")
            else:
                print(f"[advisor.metrics] DM 推送失敗: {title}", file=sys.stderr)
                return 1
        except Exception as exc:
            print(f"[advisor.metrics] DM 推送例外: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_brief(args) -> int:
    if not getattr(args, "force", False) and not getattr(args, "stdout", False):
        from sim.calendar import is_trading_day
        if not is_trading_day():
            print("[advisor.brief] 今日非交易日（週末或台股假日）— skip（用 --force 繞過）")
            return 0
    from sim.advisor.daily_brief import render_daily_brief, write_daily_brief

    brief_date = _parse_date_or_none(getattr(args, "date", None))
    since = _parse_date_or_none(getattr(args, "since", None))

    if args.stdout:
        md = render_daily_brief(brief_date=brief_date, since=since)
        sys.stdout.write(md)
        return 0

    output_path = write_daily_brief(
        output_path=args.output,
        brief_date=brief_date,
        since=since,
    )
    print(f"[advisor.brief] 已寫入 {output_path}")
    return 0


def cmd_stop_alerts(args) -> int:
    """聚合 stop 風控提醒；預設有新提醒才推 DM。"""
    from sim.advisor.stop_alerts import collect_stop_alerts, format_stop_alerts, mark_alerts_sent

    alerts, state = collect_stop_alerts(include_sent=args.include_sent)
    text = format_stop_alerts(alerts)
    print(text)

    if not alerts:
        return 0

    if args.mark_sent:
        mark_alerts_sent(alerts, state)
        print(f"[advisor.stop-alerts] marked {len(alerts)} alerts sent")
        return 0

    if args.no_send:
        print(f"[advisor.stop-alerts] no-send: {len(alerts)} alerts pending")
        return 0

    try:
        from sim._notify import send_dm
        title = f"sim stop 風控提醒（{len(alerts)}）"
        if send_dm(title, text):
            mark_alerts_sent(alerts, state)
            print(f"[advisor.stop-alerts] 已推送 DM 並標記 sent: {len(alerts)}")
            return 0
        print("[advisor.stop-alerts] DM 推送失敗", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[advisor.stop-alerts] DM 推送例外: {exc}", file=sys.stderr)
        return 1


def cmd_dispatch(args) -> int:
    """M6.1：replay PENDING orders + 撮合一輪 / 或 --cancel-all-pending 清孤立。"""
    from sim.feed import QuoteFeed
    from sim.advisor._audit import append_jsonl
    from sim.advisor.risk_guard import DEFAULT_AUDIT_PATH
    from sim.advisor.runner import init_engine_with_pending, should_run_now

    # 撮合 mode 受 should_run_now 約束（盤外不必撮合、無 quote）；
    # cancel-all-pending 是手動清理動作，不受時段限制；
    # cancel-stale-pending (EOD) 受 is_trading_day 約束（假日不需清理）。
    if not args.cancel_all_pending and not args.force:
        if args.cancel_stale_pending:
            from sim.calendar import is_trading_day
            if not is_trading_day():
                print("[advisor.dispatch] 今日非交易日（週末或台股假日）— EOD 清理 skip（用 --force 繞過）")
                return 0
        else:
            if not should_run_now():
                print("[advisor.dispatch] 非盤中時段或台股假日 — 本輪 noop（用 --force 繞過）")
                return 0

    from datetime import datetime
    ts = datetime.now().isoformat(timespec="seconds")

    engine = init_engine_with_pending()
    pending_count = len(engine.pending_orders)

    if args.cancel_all_pending:
        cancelled_ids = []
        for oid in list(engine.pending_orders.keys()):
            if engine.cancel_order(oid):
                cancelled_ids.append(oid)
        print(
            f"[advisor.dispatch] cancel-all-pending: replay={pending_count} "
            f"cancelled={len(cancelled_ids)}"
        )
        append_jsonl(DEFAULT_AUDIT_PATH, {
            "type": "dispatch_round",
            "time": ts,
            "mode": "cancel_all_pending",
            "pending_replayed": pending_count,
            "cancelled": len(cancelled_ids),
            "cancelled_ids": cancelled_ids[:50],  # 截斷避免巨量 audit
        })
        return 0

    if args.cancel_stale_pending:
        # EOD 清理：只清「日內 LMT/MKT」殘留；保留 STOP/TP 與 [auto-stop] 持倉保護單。
        # 設計依據：advisor 只下 LMT/MKT，STOP/TP 與 [auto-stop] 都是 runner 自動掛的
        # 持倉風控影子單，必須跨日存活。
        from sim.models import OrderType as _OT
        cancelled_ids: list[str] = []
        skipped_ids: list[str] = []
        skipped_reasons: dict[str, int] = {"is_stop_or_tp": 0, "is_auto_stop": 0}
        for oid in list(engine.pending_orders.keys()):
            o = engine.pending_orders.get(oid)
            if o is None:
                continue
            if o.type in (_OT.STOP, _OT.TP):
                skipped_ids.append(oid)
                skipped_reasons["is_stop_or_tp"] += 1
                continue
            if (o.notes or "").startswith("[auto-stop]"):
                skipped_ids.append(oid)
                skipped_reasons["is_auto_stop"] += 1
                continue
            if engine.cancel_order(oid):
                cancelled_ids.append(oid)
        print(
            f"[advisor.dispatch] cancel-stale-pending: replay={pending_count} "
            f"cancelled={len(cancelled_ids)} skipped={len(skipped_ids)} "
            f"(stop/tp={skipped_reasons['is_stop_or_tp']}, "
            f"auto-stop={skipped_reasons['is_auto_stop']})"
        )
        append_jsonl(DEFAULT_AUDIT_PATH, {
            "type": "dispatch_round",
            "time": ts,
            "mode": "cancel_stale_pending",
            "pending_replayed": pending_count,
            "cancelled": len(cancelled_ids),
            "skipped": len(skipped_ids),
            "skipped_reasons": skipped_reasons,
            "cancelled_ids": cancelled_ids[:50],
        })
        return 0

    # 正常 dispatch：拉 quote → match → apply_fills
    symbols = sorted({o.symbol for o in engine.pending_orders.values()})
    quotes: dict[str, float] = {}
    if symbols:
        feed = QuoteFeed()
        try:
            quotes = feed.get_quotes(symbols) or {}
        except Exception as exc:
            print(f"[advisor.dispatch] feed.get_quotes 失敗: {exc}", file=sys.stderr)

    matched = 0
    if quotes:
        fills = engine.match(quotes)
        if fills:
            engine.apply_fills(fills)
            matched = len(fills)
            try:
                from sim._notify import notify_fill
                snapshot = engine.snapshot(quotes)
                for fill in fills:
                    notify_fill(fill.to_dict(), snapshot)
            except Exception as exc:
                print(f"[advisor.dispatch] notify_fill 失敗（不阻塞）: {exc}", file=sys.stderr)

    # trailing stop 階梯上移（每輪 dispatch 都檢查，不只有 fill 發生時）
    trail_records: list = []
    if quotes:
        from sim.advisor.auto_trail import maybe_raise_trailing_stops
        try:
            # 需要所有持倉 symbol 的 quote（可能不在 pending symbols 裡）
            held_syms = sorted({
                s for s, p in (engine.account.positions or {}).items()
                if getattr(p, "qty", 0) > 0
            })
            missing = [s for s in held_syms if s not in quotes]
            if missing:
                extra = feed.get_quotes(missing) or {}
                quotes.update(extra)
            trail_records = maybe_raise_trailing_stops(engine, quotes)
        except Exception as exc:
            print(f"[advisor.dispatch] trailing stop 失敗（不阻塞）: {exc}", file=sys.stderr)

    print(
        f"[advisor.dispatch] pending_replay={pending_count} symbols={len(symbols)} "
        f"quotes={len(quotes)} matched={matched} trail_raised={len(trail_records)}"
    )
    append_jsonl(DEFAULT_AUDIT_PATH, {
        "type": "dispatch_round",
        "time": ts,
        "mode": "match",
        "pending_replayed": pending_count,
        "symbols": len(symbols),
        "quotes": len(quotes),
        "matched": matched,
        "trail_raised": len(trail_records),
        "trail_details": trail_records[:10],
    })
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sim.advisor")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_decide = sub.add_parser("decide", help="跑一輪 advisor 決策")
    p_decide.add_argument("--dry-run", action="store_true", help="過 risk_guard 但不 submit_order")
    p_decide.add_argument("--force", action="store_true", help="繞過 should_run_now（盤外仍跑）")
    p_decide.set_defaults(func=cmd_decide)

    p_metrics = sub.add_parser("metrics", help="盤後算四件套（M6 階段二）")
    p_metrics.add_argument("--since", help="起算日期 YYYY-MM-DD；預設全期")
    p_metrics.add_argument("--no-send", action="store_true", help="只落 advisor_metrics.jsonl + stdout，不推飛書 DM")
    p_metrics.add_argument("--force", action="store_true", help="繞過休市日檢查（假日仍跑）")
    p_metrics.set_defaults(func=cmd_metrics)

    p_brief = sub.add_parser("brief", help="產出當日聚合 markdown 報告")
    p_brief.add_argument("--date", help="報告日期 YYYY-MM-DD；預設今天")
    p_brief.add_argument("--since", help="起算日期 YYYY-MM-DD；預設近 30 天")
    p_brief.add_argument("--stdout", action="store_true", help="印到終端、不寫檔")
    p_brief.add_argument("--output", help="自訂輸出路徑；預設 logs/sim/daily_brief/brief_YYYY-MM-DD.md")
    p_brief.add_argument("--force", action="store_true", help="繞過休市日檢查（假日仍跑）")
    p_brief.set_defaults(func=cmd_brief)

    p_stop_alerts = sub.add_parser("stop-alerts", help="推送 stop 風控提醒（ACTION NEEDED / auto-stop terminal）")
    p_stop_alerts.add_argument("--no-send", action="store_true", help="只印出提醒，不推 DM、不標記 sent")
    p_stop_alerts.add_argument("--include-sent", action="store_true", help="包含已推送過的提醒（除錯用）")
    p_stop_alerts.add_argument("--mark-sent", action="store_true", help="不推 DM，直接把目前提醒標記為已送（除錯/降噪用）")
    p_stop_alerts.set_defaults(func=cmd_stop_alerts)

    p_dispatch = sub.add_parser("dispatch", help="M6.1 跨進程撮合 / 清孤立 PENDING")
    p_dispatch.add_argument("--force", action="store_true", help="繞過 should_run_now")
    p_dispatch.add_argument(
        "--cancel-all-pending", action="store_true",
        help="把 orders.jsonl 所有 PENDING 改 CANCELLED（不撮合，清孤立用，含 [auto-stop]）",
    )
    p_dispatch.add_argument(
        "--cancel-stale-pending", action="store_true",
        help="EOD 清理：只清日內 LMT/MKT 殘留，保留 STOP/TP 與 [auto-stop] 持倉保護單",
    )
    p_dispatch.set_defaults(func=cmd_dispatch)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except ImportError:
        pass
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
