from utils.market_data_contract import attach_change_metric, percent_change


def test_percent_change_is_labelled_with_basis_and_source():
    metric = percent_change(
        105,
        100,
        basis="previous_close",
        source="IBKR",
        is_realtime=False,
        asof="2026-07-11T10:00:00+08:00",
    )

    assert metric.value_pct == 5.0
    assert metric.basis == "previous_close"
    assert metric.source == "IBKR"
    assert metric.is_realtime is False


def test_missing_reference_does_not_create_fake_zero_change():
    metric = percent_change(105, None, basis="previous_close", source="yfinance")
    assert metric.value_pct is None


def test_attach_change_metric_keeps_original_payload():
    payload = {"last": 110, "close": 100, "asof": "2026-07-11T10:00:00+08:00"}
    out = attach_change_metric(
        payload,
        current_key="last",
        reference_key="close",
        basis="previous_close",
        source="IBKR",
    )

    assert out["last"] == 110
    assert out["change_metric"]["value_pct"] == 10.0
    assert "change_metric" not in payload
