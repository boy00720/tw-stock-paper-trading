#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload
from utils.watchlist_schema import cross_signals_count
from utils.wechat_notifier import WechatNotifier
from utils.screener_tracker import get_screener_tracker

_BASE_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "logs" / "screener_review.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _resolve_week_start(trade_date: Optional[datetime] = None) -> datetime:
    today = trade_date or datetime.now()
    week_start = today - timedelta(days=today.weekday())
    return datetime(week_start.year, week_start.month, week_start.day)



def _load_cross_signals_count() -> int:
    try:
        return cross_signals_count()
    except Exception as e:
        logger.warning("讀取 watchlist/current.json 失敗: %s", e)
        return 0


def _build_distributions(week_start: datetime) -> Dict[str, Dict[str, int]]:
    tracker = get_screener_tracker()
    month_str = week_start.strftime("%Y-%m")
    history_file = tracker.history_dir / f"{month_str}.jsonl"
    week_end = week_start + timedelta(days=6)

    engine_dist: Dict[str, int] = {}
    inst_dist: Dict[str, int] = {}

    if not history_file.exists():
        return {"engine": engine_dist, "inst_signal": inst_dist}

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line.strip())
                pick_date = datetime.fromisoformat(record["pick_date"])
                if not (week_start <= pick_date <= week_end):
                    continue
                if record.get("outcome") not in ("win", "loss"):
                    continue
                engine = str(record.get("engine") or "unknown")
                inst_signal = str(record.get("inst_signal") or "未知")
                engine_dist[engine] = engine_dist.get(engine, 0) + 1
                inst_dist[inst_signal] = inst_dist.get(inst_signal, 0) + 1
    except Exception as e:
        logger.warning("計算分布失敗: %s", e)

    return {"engine": engine_dist, "inst_signal": inst_dist}


def _normalize_metric_value(value: Any) -> Any:
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return value


def _build_metrics(report: Dict[str, Any], week_start: datetime) -> Dict[str, Any]:
    dist = _build_distributions(week_start)
    completed_trades = int(report.get("completed_trades", 0) or 0)
    win_rate = _normalize_metric_value(float(report.get("win_rate", 0.0) or 0.0))
    profit_loss_ratio = _normalize_metric_value(float(report.get("profit_factor", 0.0) or 0.0))
    cross_signals_count = _load_cross_signals_count()
    week_range = f"{week_start.strftime('%Y-%m-%d')} ~ {(week_start + timedelta(days=6)).strftime('%Y-%m-%d')}"
    return {
        "week_range": week_range,
        "completed_trades": completed_trades,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "engine_distribution": dist["engine"],
        "inst_signal_distribution": dist["inst_signal"],
        "cross_signals_count": cross_signals_count,
        "display": {
            "週別區間": week_range,
            "完成交易筆數": completed_trades,
            "勝率": win_rate,
            "盈虧比": profit_loss_ratio,
            "引擎分布": dist["engine"],
            "法人訊號分布": dist["inst_signal"],
            "跨池共振數": cross_signals_count,
        },
    }


def _humanize_engine_label(name: str) -> str:
    mapping = {
        "momentum": "量價突破",
        "rebound": "均值回歸",
    }
    return mapping.get(name, name)


def _format_distribution(distribution: Dict[str, int], *, humanize_engine: bool = False) -> str:
    if not distribution:
        return "無"
    return "、".join(
        f"{_humanize_engine_label(key) if humanize_engine else key}:{value}"
        for key, value in distribution.items()
    )


def _format_metric_value(value: Any, *, is_percent: bool = False) -> str:
    if value is None:
        return "無"
    if isinstance(value, float):
        if value == float("inf") or value == float("-inf"):
            return "不適用"
        if value != value:
            return "不適用"
        text = f"{value:.1f}" if is_percent else f"{value:.2f}"
        return f"{text}%" if is_percent else text
    return f"{value}%" if is_percent else str(value)


def _build_notification_content(metrics: Dict[str, Any], report_text: str) -> str:
    display = metrics.get("display") or {}
    lines = [
        f"週別區間: {display.get('週別區間', metrics.get('week_range', ''))}",
        f"完成交易筆數: {display.get('完成交易筆數', metrics.get('completed_trades', 0))}",
        f"勝率: {_format_metric_value(display.get('勝率', metrics.get('win_rate', 0.0)), is_percent=True)}",
        f"盈虧比: {_format_metric_value(display.get('盈虧比', metrics.get('profit_loss_ratio', 0.0)))}",
        f"短線策略分布: {_format_distribution(display.get('引擎分布') or metrics.get('engine_distribution') or {}, humanize_engine=True)}",
        f"法人訊號分布: {_format_distribution(display.get('法人訊號分布') or metrics.get('inst_signal_distribution') or {})}",
        f"跨池共振數: {display.get('跨池共振數', metrics.get('cross_signals_count', 0))}",
    ]
    return "\n".join(lines) + "\n\n" + report_text


def _sanitize_report_text(report_text: str) -> str:
    sanitized = report_text.replace("**盈虧比**: inf", "**盈虧比**: 不適用")
    sanitized = sanitized.replace("**盈虧比**: Infinity", "**盈虧比**: 不適用")
    sanitized = sanitized.replace("**盈虧比**: -inf", "**盈虧比**: 不適用")
    sanitized = sanitized.replace("**盈虧比**: NaN", "**盈虧比**: 不適用")
    return sanitized


def _notify(title: str, content: str, payload: Dict[str, Any]) -> None:
    control = get_current_control()
    if control is not None:
        write_payload(payload, control=control)
        logger.info("Master agent 接管通知：%s", title)
        return

    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
    notifier.send_message("oc_your_feishu_chat_id", title, content)

    wechat = WechatNotifier()
    wx_text = (((payload.get("notification") or {}).get("wechat") or {}).get("text") or content)[:1500]
    wx_typing_seconds = int((((payload.get("notification") or {}).get("wechat") or {}).get("typing_seconds") or 0))
    wx_ok = wechat.send_message(wx_text, typing_seconds=wx_typing_seconds, allow_unready_queue=True)
    if not wx_ok:
        logger.warning("微信發送失敗：%s", wechat.last_error)


def _format_week_label(week_start: datetime) -> str:
    return f"{week_start.year}年第{int(week_start.strftime('%W'))}週"


def run_review(trade_date: Optional[datetime] = None, dry_run: bool = False) -> Dict[str, Any]:
    week_start = _resolve_week_start(trade_date)
    tracker = get_screener_tracker()

    try:
        perf = tracker.generate_weekly_report(week_start=week_start)
        report_text = _sanitize_report_text(tracker.format_weekly_report(perf))
        report_dict = asdict(perf)
        metrics = _build_metrics(report_dict, week_start)

        result = {
            "ok": True,
            "reason": "",
            "week_range": metrics["week_range"],
            "metrics": metrics,
            "report_text": report_text,
        }

        week_label = _format_week_label(week_start)
        title = f"📈 一週回測摘要 · {week_label}"
        content = _build_notification_content(metrics, report_text)

        payload = {
            "agent": "screener_review",
            "artifacts": {
                "weekly_report_file": str(tracker.weekly_dir / f"{week_start.strftime('%Y-W%W')}.json"),
            },
            "meta": {
                "week_start": week_start.strftime("%Y-%m-%d"),
                "metrics": metrics,
            },
            "notification": {
                "feishu": {
                    "title": title,
                    "content": content,
                },
                "wechat": {
                    "text": content,
                    "typing_seconds": 4,
                },
            },
        }

        if not dry_run:
            if metrics["completed_trades"] == 0:
                fallback_title = f"⚠️ 週回測未生成完整摘要 · {week_label}"
                fallback_content = f"週別 {metrics['week_range']} 尚無完成交易筆數（0 筆），可用樣本不足。"
                _notify(
                    fallback_title,
                    fallback_content,
                    {
                        "agent": "screener_review",
                        "meta": {"reason": "DATA_INSUFFICIENT", "metrics": metrics},
                        "notification": {
                            "feishu": {"title": fallback_title, "content": fallback_content},
                            "wechat": {"text": fallback_content, "typing_seconds": 3},
                        },
                    },
                )
                result["ok"] = False
                result["reason"] = "DATA_INSUFFICIENT"
            else:
                _notify(title, content, payload)

        return result
    except Exception as e:
        logger.exception("週回測生成失敗")
        reason = f"COMPUTE_ERROR: {e}"
        msg = f"週回測未生成，原因：{reason}"
        if not dry_run:
            _notify(
                "⚠️ 週回測未生成",
                msg,
                {
                    "agent": "screener_review",
                    "meta": {"reason": reason},
                    "notification": {
                        "feishu": {"title": "⚠️ 週回測未生成", "content": msg},
                        "wechat": {"text": msg, "typing_seconds": 3},
                    },
                },
            )
        return {
            "ok": False,
            "reason": reason,
            "week_range": f"{week_start.strftime('%Y-%m-%d')} ~ {(week_start + timedelta(days=6)).strftime('%Y-%m-%d')}",
            "metrics": {},
            "report_text": "",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="週回測摘要")
    parser.add_argument("--dry-run", action="store_true", help="不推播")
    args = parser.parse_args()

    result = run_review(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
