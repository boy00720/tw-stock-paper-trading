"""
M3 驗收測試：sim/commands.py 的 parse + dispatch。

執行：
    python3 sim/tests/test_commands.py
    python3 -m pytest sim/tests/test_commands.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.commands import (
    ParsedCommand,
    dispatch,
    is_sim_prefix,
    parse_command,
)
from sim.engine import MatchEngine
from sim.feed import QuoteFeed
from sim.models import Order, OrderSide, OrderType


# ─────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────

class _FakeFeed(QuoteFeed):
    """測試用 fake feed，注入固定報價。"""

    def __init__(self, prices: dict[str, float]):
        super().__init__()
        self._prices = dict(prices)
        # 讓 last_known 也有值
        for sym, p in prices.items():
            self._last_known[sym] = p

    def _fetch(self, symbols):
        return {sym: self._prices.get(sym) for sym in symbols}


def _make_engine(tmp_dir: Path) -> MatchEngine:
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


# ─────────────────────────────────────────────────────
# is_sim_prefix
# ─────────────────────────────────────────────────────

def test_is_sim_prefix_recognises_known_commands(tmp_dir):
    assert is_sim_prefix("/buy 2330.TW 100")
    assert is_sim_prefix("/sell 2330.TW 100")
    assert is_sim_prefix("/cancel ord-x")
    assert is_sim_prefix("/close_all")
    assert is_sim_prefix("/portfolio")
    assert is_sim_prefix("/p")
    assert is_sim_prefix("/orders")
    assert is_sim_prefix("/pnl")
    assert is_sim_prefix("/equity")
    assert is_sim_prefix("/quote 0050.TW")
    # 大小寫不敏感
    assert is_sim_prefix("/BUY 2330.TW 100")


def test_is_sim_prefix_rejects_non_sim(tmp_dir):
    assert not is_sim_prefix("")
    assert not is_sim_prefix("你好")
    assert not is_sim_prefix("/code create foo")
    assert not is_sim_prefix("/help")
    assert not is_sim_prefix("buy 2330.TW")  # 缺斜線


# ─────────────────────────────────────────────────────
# parse_command — happy path
# ─────────────────────────────────────────────────────

def test_parse_buy_market(tmp_dir):
    p = parse_command("/buy 2330.TW 100")
    assert p.cmd == "order"
    assert p.args["side"] == OrderSide.BUY
    assert p.args["type"] == OrderType.MKT
    assert p.args["symbol"] == "2330.TW"
    assert p.args["qty"] == 100
    assert p.args["price"] is None


def test_parse_buy_limit(tmp_dir):
    p = parse_command("/buy 2330.TW 100 limit 580")
    assert p.cmd == "order"
    assert p.args["type"] == OrderType.LMT
    assert p.args["price"] == 580.0


def test_parse_buy_stop(tmp_dir):
    p = parse_command("/buy 2330.TW 100 stop 620")
    assert p.cmd == "order"
    assert p.args["type"] == OrderType.STOP
    assert p.args["price"] == 620.0


def test_parse_sell(tmp_dir):
    p = parse_command("/sell 0050.TW 200")
    assert p.cmd == "order"
    assert p.args["side"] == OrderSide.SELL
    assert p.args["qty"] == 200


def test_parse_cancel(tmp_dir):
    p = parse_command("/cancel ord-abc123")
    assert p.cmd == "cancel"
    assert p.args["order_id"] == "ord-abc123"


def test_parse_close_all_portfolio_p_aliases(tmp_dir):
    assert parse_command("/close_all").cmd == "close_all"
    assert parse_command("/portfolio").cmd == "portfolio"
    assert parse_command("/p").cmd == "portfolio"
    assert parse_command("/orders").cmd == "orders"
    assert parse_command("/pnl").cmd == "pnl"
    assert parse_command("/equity").cmd == "equity"


def test_parse_quote(tmp_dir):
    p = parse_command("/quote 2330.TW")
    assert p.cmd == "quote"
    assert p.args["symbol"] == "2330.TW"


# ─────────────────────────────────────────────────────
# parse_command — invalid path
# ─────────────────────────────────────────────────────

def test_parse_invalid_buy_missing_args(tmp_dir):
    p = parse_command("/buy")
    assert p.cmd == "_invalid"
    assert "用法" in p.args["reason"]


def test_parse_invalid_buy_bad_qty(tmp_dir):
    p = parse_command("/buy 2330.TW abc")
    assert p.cmd == "_invalid"
    assert "qty" in p.args["reason"]


def test_parse_invalid_buy_negative_qty(tmp_dir):
    p = parse_command("/buy 2330.TW -10")
    assert p.cmd == "_invalid"
    assert "qty" in p.args["reason"]


def test_parse_invalid_limit_missing_price(tmp_dir):
    p = parse_command("/buy 2330.TW 100 limit")
    assert p.cmd == "_invalid"


def test_parse_invalid_unknown_order_kind(tmp_dir):
    p = parse_command("/buy 2330.TW 100 trail 5")
    assert p.cmd == "_invalid"
    assert "trail" in p.args["reason"]


def test_parse_invalid_symbol(tmp_dir):
    p = parse_command("/buy 2330#$%TW 100")
    assert p.cmd == "_invalid"


def test_parse_quote_missing_arg(tmp_dir):
    p = parse_command("/quote")
    assert p.cmd == "_invalid"


def test_parse_non_sim_returns_none(tmp_dir):
    assert parse_command("") is None
    assert parse_command("你好") is None
    assert parse_command("/code create foo") is None


# ─────────────────────────────────────────────────────
# placeholder 包容（< > 「」 等）
# ─────────────────────────────────────────────────────

def test_parse_cancel_strips_angle_brackets(tmp_dir):
    """常見情境：用戶從文件複製『/cancel <ord-xxx>』連同尖括號一起貼"""
    p = parse_command("/cancel <ord-50321461d2ed>")
    assert p.cmd == "cancel"
    assert p.args["order_id"] == "ord-50321461d2ed"


def test_parse_cancel_strips_chinese_brackets(tmp_dir):
    p = parse_command("/cancel 「ord-abc123」")
    assert p.cmd == "cancel"
    assert p.args["order_id"] == "ord-abc123"


def test_parse_cancel_strips_backticks(tmp_dir):
    p = parse_command("/cancel `ord-abc123`")
    assert p.cmd == "cancel"
    assert p.args["order_id"] == "ord-abc123"


def test_parse_buy_strips_symbol_brackets(tmp_dir):
    """同樣的包容也套到 symbol，避免 /buy <2330.TW> 100 這種失敗"""
    p = parse_command("/buy <2330.TW> 100")
    assert p.cmd == "order"
    assert p.args["symbol"] == "2330.TW"


def test_parse_quote_strips_symbol_brackets(tmp_dir):
    p = parse_command("/quote 「2330.TW」")
    assert p.cmd == "quote"
    assert p.args["symbol"] == "2330.TW"


def test_parse_cancel_empty_after_strip_invalid(tmp_dir):
    """全部都是 placeholder 字元 → strip 後為空，給 _invalid"""
    p = parse_command("/cancel <>")
    assert p.cmd == "_invalid"


# ─────────────────────────────────────────────────────
# dispatch — order 流程
# ─────────────────────────────────────────────────────

def test_dispatch_buy_market_fills_immediately(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    p = parse_command("/buy 2330.TW 100")
    reply = dispatch(p, e, feed)
    assert "已下單" in reply
    assert "成交" in reply
    assert "600" in reply  # 成交價附近
    # 持倉真的進來了
    assert "2330.TW" in e.account.positions


def test_dispatch_buy_limit_pending(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    p = parse_command("/buy 2330.TW 100 limit 500")
    reply = dispatch(p, e, feed)
    assert "已下單" in reply
    assert "等候撮合" in reply
    # pending 仍在
    assert any(o.symbol == "2330.TW" and o.price == 500.0 for o in e.pending_orders.values())


def test_dispatch_buy_no_quote_pending(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})  # 完全沒報價
    p = parse_command("/buy 2330.TW 100")
    reply = dispatch(p, e, feed)
    assert "已下單" in reply
    assert "無法取得" in reply or "等候" in reply


# ─────────────────────────────────────────────────────
# dispatch — cancel
# ─────────────────────────────────────────────────────

def test_dispatch_cancel_success(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    o = e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=500.0))
    p = parse_command(f"/cancel {o.order_id}")
    reply = dispatch(p, e, feed)
    assert "已取消" in reply
    assert o.order_id not in e.pending_orders


def test_dispatch_cancel_not_found(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    p = parse_command("/cancel ord-doesnotexist")
    reply = dispatch(p, e, feed)
    assert "失敗" in reply or "找不到" in reply


# ─────────────────────────────────────────────────────
# dispatch — portfolio / orders / pnl / equity
# ─────────────────────────────────────────────────────

def test_dispatch_portfolio_empty(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    p = parse_command("/portfolio")
    reply = dispatch(p, e, feed)
    assert "無持倉" in reply
    assert "現金" in reply


def test_dispatch_portfolio_with_positions(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 605.0})
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))

    p = parse_command("/portfolio")
    reply = dispatch(p, e, feed)
    assert "2330.TW" in reply
    assert "100" in reply
    # 含未實現損益標記
    assert "unrealized" in reply or "未實現" in reply or "+" in reply or "-" in reply


def test_dispatch_orders_empty(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    reply = dispatch(parse_command("/orders"), e, feed)
    assert "無待成交" in reply


def test_dispatch_orders_with_pending(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=500.0))
    reply = dispatch(parse_command("/orders"), e, feed)
    assert "2330.TW" in reply
    assert "LMT" in reply
    assert "PENDING" in reply


def test_dispatch_pnl_includes_realized_and_unrealized(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 605.0})
    # 建持倉
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))

    reply = dispatch(parse_command("/pnl"), e, feed)
    assert "已實現" in reply
    assert "未實現" in reply
    # M5：訊息結構含「今日 / 30 日 / 累計」三段切片
    assert "今日已實現" in reply
    assert "近 30 日已實現" in reply
    assert "累計已實現" in reply


def test_dispatch_equity_returns_summary(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    reply = dispatch(parse_command("/equity"), e, feed)
    assert "現金" in reply
    assert "權益" in reply
    assert "%" in reply
    # M5：含 30 日 equity_curve 摘要區塊（無記錄時降級訊息）
    assert "近 30 日" in reply


# ─────────────────────────────────────────────────────
# M5 A 方案：dispatch 立即撮合也要觸發 on_fill callback
# （否則 MKT 成交漏推 DM — 真實 bug，W 12:15 飛書驗證發現）
# ─────────────────────────────────────────────────────

def test_dispatch_mkt_buy_invokes_on_fill(tmp_dir):
    """/buy MKT 立即撮合應呼叫 on_fill callback 一次，含 fill_dict + snapshot。"""
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    captured: list[tuple[dict, dict]] = []

    def _cb(fill_dict, snapshot):
        captured.append((fill_dict, snapshot))

    reply = dispatch(parse_command("/buy 2330.TW 100"), e, feed, on_fill=_cb)

    assert "成交" in reply
    assert len(captured) == 1
    fill_dict, snapshot = captured[0]
    assert fill_dict["symbol"] == "2330.TW"
    assert fill_dict["side"] == "BUY"
    assert fill_dict["qty"] == 100
    assert "account" in snapshot


def test_dispatch_mkt_on_fill_exception_does_not_break_reply(tmp_dir):
    """callback 拋例外時 dispatch 仍回正常成交字串（callback 錯誤吞掉）。"""
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})

    def _bad_cb(fill_dict, snapshot):
        raise RuntimeError("boom")

    reply = dispatch(parse_command("/buy 2330.TW 100"), e, feed, on_fill=_bad_cb)
    assert "成交" in reply  # callback 失敗不阻擋回覆


def test_dispatch_close_all_invokes_on_fill(tmp_dir):
    """/close_all 立即撮合也應觸發 on_fill。"""
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    # 先建持倉
    dispatch(parse_command("/buy 2330.TW 100"), e, feed)

    captured: list[tuple[dict, dict]] = []

    def _cb(fill_dict, snapshot):
        captured.append((fill_dict, snapshot))

    reply = dispatch(parse_command("/close_all"), e, feed, on_fill=_cb)

    assert "全平倉" in reply
    assert len(captured) == 1
    assert captured[0][0]["side"] == "SELL"
    assert captured[0][0]["qty"] == 100


def test_dispatch_mkt_buy_no_callback_backwards_compat(tmp_dir):
    """沒傳 on_fill（舊 caller）時行為不變，不炸。"""
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    reply = dispatch(parse_command("/buy 2330.TW 100"), e, feed)  # 不傳 on_fill
    assert "成交" in reply
    assert "2330.TW" in e.account.positions


# ─────────────────────────────────────────────────────
# dispatch — close_all
# ─────────────────────────────────────────────────────

def test_dispatch_close_all_no_position(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    reply = dispatch(parse_command("/close_all"), e, feed)
    assert "無持倉" in reply


def test_dispatch_close_all_multiple_positions(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 605.0, "0050.TW": 152.0})
    # 建兩檔持倉
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    e.submit_order(Order(symbol="0050.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=200))
    e.apply_fills(e.match({"0050.TW": 150.0}))

    reply = dispatch(parse_command("/close_all"), e, feed)
    assert "2 張市價賣單" in reply
    assert "2330.TW" in reply
    assert "0050.TW" in reply
    # close_all 後（含內部撮合）持倉應清空
    assert not e.account.positions


# ─────────────────────────────────────────────────────
# dispatch — quote
# ─────────────────────────────────────────────────────

def test_dispatch_quote_success(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    reply = dispatch(parse_command("/quote 2330.TW"), e, feed)
    assert "2330.TW" in reply
    assert "600" in reply
    assert "yfinance" in reply or "延遲" in reply


def test_dispatch_quote_no_data(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})  # 沒任何報價
    reply = dispatch(parse_command("/quote 9999.TW"), e, feed)
    assert "無法取得" in reply


# ─────────────────────────────────────────────────────
# dispatch — invalid / sell with insufficient holding
# ─────────────────────────────────────────────────────

def test_dispatch_invalid_returns_friendly_error(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({})
    p = parse_command("/buy")
    reply = dispatch(p, e, feed)
    assert reply.startswith("❌")
    assert "用法" in reply


def test_dispatch_sell_rejected_when_no_holding(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = _FakeFeed({"2330.TW": 600.0})
    p = parse_command("/sell 2330.TW 100")
    reply = dispatch(p, e, feed)
    assert "失敗" in reply
    assert "insufficient holding" in reply or "持倉" in reply or "held" in reply


# ─────────────────────────────────────────────────────
# 自包執行
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    g = globals()
    tests = [name for name, obj in g.items() if name.startswith("test_") and callable(obj)]
    failed = []
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root)
        for t in tests:
            try:
                sub = root / t
                sub.mkdir()
                g[t](sub)
                print(f"  ✅ {t}")
            except Exception as exc:
                import traceback
                print(f"  ❌ {t}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                failed.append(t)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通過")
    sys.exit(0 if not failed else 1)


try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass
