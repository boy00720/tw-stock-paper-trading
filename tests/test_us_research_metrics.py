from utils.us_research_metrics import info_sections, technical_metrics


def _rows(volume_missing=False):
    rows = [
        {"date": f"d{i}", "close": 100 + i, "volume": 1000 + i}
        for i in range(70)
    ]
    if volume_missing:
        rows[-1]["volume"] = None
    return rows


def test_technical_metrics_keep_price_volume_alignment():
    metrics = technical_metrics(_rows(volume_missing=True), _rows())
    assert metrics["available"] is True
    assert metrics["volume_ratio_20d"] is None
    assert "not aligned" in metrics["warnings"][0]


def test_info_availability_matches_scored_fields():
    trailing_only = info_sections({"trailingPE": 20})
    average_volume_only = info_sections({"averageVolume": 1_000_000})
    assert trailing_only["valuation"]["available"] is False
    assert average_volume_only["capital"]["available"] is False


def test_info_sections_accept_scored_fields_and_reject_non_finite():
    sections = info_sections({
        "forwardPE": 20,
        "earningsGrowth": 0.2,
        "heldPercentInstitutions": 0.7,
        "revenueGrowth": "NaN",
    })
    assert sections["valuation"]["available"] is True
    assert sections["fundamental"]["available"] is True
    assert sections["capital"]["available"] is True
    assert sections["fundamental"]["metrics"]["revenue_growth"] is None
