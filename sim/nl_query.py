"""
automation/sim/nl_query.py — sim 自然語言查詢路由與 snapshot 構建（M4）

提供三個對外函數：
- `is_sim_nl_query(text)`：快速判斷是否為 sim 自然語言查詢（feishu_bot 路由用）
- `build_snapshot(engine, feed=None, n_fills=100, n_orders=50)`：構建餵 LLM 的狀態 snapshot
- `answer_nl_query(text, snapshot, helper=None)`：把問句 + snapshot 餵 LLM 取得回覆

設計見 sim/M4_PLAN.md。

設計原則：
- 觸發條件雙軌：明示前綴 `@sim` + 隱式關鍵字白名單
- snapshot 薄版：engine.snapshot + 最近 N 筆 fills + lifetime 簡化勝率
- 回覆風格：system prompt 約束 + LLM 自由生成（風格對齊 M3 _fmt_*）
- 純函數，不依賴飛書 API；helper 預設 lazy import 便於單測注入 mock
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 觸發條件
# ─────────────────────────────────────────────────────

# 明示前綴（大小寫不敏感）
_AT_SIM_PREFIX_LOWER = "@sim"

# 隱式關鍵字白名單（M4_PLAN v1 草案 + 2026-05-05 補簡體，PR 階段可校準）
# 原則：
# - 含「我的 / 我這 / 我目前 / 我累計」等第一人稱所有格 → sim 場景
# - 含 sim-domain 名詞（勝率 / 未實現 / 成交記錄等）→ sim 場景
# - 不放單獨「虧損 / 盈利 / 成交」（會誤吞情緒抒發或新聞討論）
# - 同時收錄繁體與簡體（飛書輸入法可能輸出簡體；繁簡相同字詞不重複）
_NL_KEYWORDS: tuple[str, ...] = (
    # 第一人稱所有格（繁體）
    "我的持倉",
    "我這個月",
    "我這週",
    "我這禮拜",
    "我目前",
    "我累計",
    # 第一人稱所有格（簡體；「我目前」繁簡同形不重複）
    "我的持仓",
    "我这个月",
    "我这周",
    "我这礼拜",
    "我累计",
    # sim-domain 名詞（繁體）
    "勝率",
    "未實現損益",
    "已實現損益",
    "平倉記錄",
    "成交記錄",
    "委託記錄",
    "最大虧損",
    "最大盈利",
    "持倉成本",
    "平均成本",
    "損益排行",
    # sim-domain 名詞（簡體；「最大盈利 / 平均成本」繁簡同形不重複）
    "胜率",
    "未实现损益",
    "已实现损益",
    "平仓记录",
    "成交记录",
    "委托记录",
    "最大亏损",
    "持仓成本",
    "损益排行",
)


def is_sim_nl_query(text: str) -> bool:
    """判斷一段訊息是否為 sim 自然語言查詢。

    雙軌規則：
    1. 訊息（去頭尾空白後）以 `@sim` 起頭（大小寫不敏感）→ True
    2. 含 `_NL_KEYWORDS` 任一關鍵字 → True
    3. 都不命中 → False
    """
    if not text:
        return False
    stripped = text.strip()
    if stripped.lower().startswith(_AT_SIM_PREFIX_LOWER):
        return True
    for keyword in _NL_KEYWORDS:
        if keyword in stripped:
            return True
    return False


# ─────────────────────────────────────────────────────
# Snapshot 構建
# ─────────────────────────────────────────────────────

# 預設 fills.jsonl 位置（與 sim/__init__.py 建立的目錄一致；
# 用相對於本檔的路徑，便於跨機器 / 測試環境）
_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILLS_PATH = _BASE_DIR / "logs" / "sim" / "fills.jsonl"


def _tail_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    """讀取 jsonl 檔的最後 n 筆，回傳 dict list。

    用 collections.deque(maxlen=n) 避免一次載入整個檔案；
    JSON parse 失敗的行會被略過（不影響其他行）。
    檔案不存在或 IO 錯誤時回傳空 list（不丟例外）。
    """
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            tail_lines = deque(f, maxlen=n)
        records: list[dict[str, Any]] = []
        for line in tail_lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception as exc:
        logger.warning("[sim.nl] tail %s 失敗: %s", path, exc)
        return []


def _compute_lifetime_stats(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """計算 lifetime 簡化勝率。

    定義：
    - realized 筆數 = realized_pnl 不為 0/null 的 fill 數
    - 勝率 = realized_pnl > 0 的筆數 / realized 筆數

    注意：M4 薄版只看 fills jsonl 裡有 realized_pnl 欄位的記錄。
    M5+ 若改 sql 或更精細統計，這裡可換實作。
    """
    realized: list[dict[str, Any]] = []
    for f in fills:
        rp = f.get("realized_pnl")
        if rp is None:
            continue
        try:
            if float(rp) != 0.0:
                realized.append(f)
        except (TypeError, ValueError):
            continue

    win_count = 0
    for f in realized:
        try:
            if float(f.get("realized_pnl", 0)) > 0:
                win_count += 1
        except (TypeError, ValueError):
            pass

    total = len(realized)
    return {
        "total_realized_fills": total,
        "win_count": win_count,
        "win_rate": (win_count / total) if total > 0 else None,
    }


def build_snapshot(
    engine: Any,
    feed: Any = None,
    n_fills: int = 100,
    n_orders: int = 50,
    fills_path: Optional[Path] = None,
) -> dict[str, Any]:
    """構建 sim 狀態 snapshot 餵給 LLM。

    內容：
    - account: cash / initial_cash / total_realized_pnl / equity / total_return_pct
    - positions（在 account 內）: 每檔加上 current_quote / unrealized_pnl
    - pending_orders: 最近 n_orders 筆未成交委託
    - recent_fills: 最近 n_fills 筆成交（從 fills.jsonl tail）
    - lifetime_stats: 簡化勝率

    feed 為 None 時不取即時報價，positions 的 current_quote 與 unrealized_pnl 為 None。
    """
    # 取持倉的即時報價（若 feed 提供）
    positions_dict = getattr(engine.account, "positions", {}) or {}
    quotes: dict[str, float] = {}
    if feed is not None and positions_dict:
        try:
            quotes = feed.get_quotes(list(positions_dict.keys())) or {}
        except Exception as exc:
            logger.warning("[sim.nl] 取持倉報價失敗: %s", exc)

    # engine.snapshot() 已含 account.{cash, equity, positions, ...} 與 pending_orders
    snap = engine.snapshot(quotes if quotes else None)

    # 為每個 position 補 current_quote 與 unrealized_pnl（snapshot 沒帶這兩個）
    positions_out = snap.get("account", {}).get("positions") or {}
    for sym, pos_dict in positions_out.items():
        q = quotes.get(sym)
        pos_dict["current_quote"] = q
        if q is not None:
            try:
                pos_dict["unrealized_pnl"] = (q - pos_dict["avg_cost"]) * pos_dict["qty"]
            except (TypeError, KeyError):
                pos_dict["unrealized_pnl"] = None
        else:
            pos_dict["unrealized_pnl"] = None

    # 截斷 pending_orders 到 n_orders
    pending = snap.get("pending_orders") or []
    pending = pending[-n_orders:]

    # tail fills.jsonl
    path = fills_path or _DEFAULT_FILLS_PATH
    recent_fills = _tail_jsonl(path, n_fills)

    # lifetime 簡化勝率
    lifetime = _compute_lifetime_stats(recent_fills)

    return {
        "account": snap.get("account", {}),
        "pending_orders": pending,
        "recent_fills": recent_fills,
        "lifetime_stats": lifetime,
    }


# ─────────────────────────────────────────────────────
# LLM 呼叫
# ─────────────────────────────────────────────────────

_M4_SYSTEM_PROMPT = """你是 W 的台股模擬盤助理。

回覆規則：
- 對象是 W 本人，不需要客套或免責聲明
- 台股口吻；數字與單位寫清楚（股 / 元 / NTD / %）
- 簡潔，最多 4 行；必有數字
- 不重述使用者問題、不堆砌欄位
- 風格對齊既有 /portfolio /pnl 的輸出（簡潔列點 + 必要數字）
- 找不到答案就直說「snapshot 沒有這個資訊」，不要編造
"""


def answer_nl_query(
    text: str,
    snapshot: dict[str, Any],
    helper: Any = None,
) -> str:
    """把使用者問句 + snapshot 餵 LLM，回傳純文字回覆。

    - `helper` 預設 None：lazy 載入 `ClaudeHelper(max_retries=0)`；
      測試時可注入 mock helper。
    - LLM 失敗時回降級訊息提示改用結構化指令。
    - `@sim` 前綴會被剝掉再餵 LLM（語意上是路由標記，不是查詢內容）。
    """
    if helper is None:
        try:
            from utils.claude_helper import ClaudeHelper
            helper = ClaudeHelper(max_retries=0)
        except Exception as exc:
            logger.error("[sim.nl] ClaudeHelper 載入失敗: %s", exc)
            return (
                "❌ NL 查詢暫不可用（LLM helper 載入失敗）。\n"
                "  請改用 /portfolio /pnl /equity /orders 等指令。"
            )

    # 把 @sim 前綴剝掉再餵 LLM
    cleaned_text = text.strip()
    if cleaned_text.lower().startswith(_AT_SIM_PREFIX_LOWER):
        cleaned_text = cleaned_text[len(_AT_SIM_PREFIX_LOWER):].strip()
    if not cleaned_text:
        return "❌ 查詢內容是空的，請在 @sim 後面加上問題。例如：@sim 我這個月勝率多少"

    snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=str, indent=2)
    user_msg = f"{cleaned_text}\n\n## 模擬盤狀態（JSON）\n```json\n{snapshot_json}\n```"

    try:
        reply = helper.chat(
            prompt=user_msg,
            system=_M4_SYSTEM_PROMPT,
            max_tokens=600,
        )
        return (reply or "").strip() or "（LLM 回覆為空，請稍後再試）"
    except Exception as exc:
        logger.error("[sim.nl] LLM 呼叫失敗: %s", exc)
        return (
            f"❌ NL 查詢暫不可用（LLM 呼叫失敗：{exc}）。\n"
            "  請改用 /portfolio /pnl /equity /orders 等指令。"
        )
