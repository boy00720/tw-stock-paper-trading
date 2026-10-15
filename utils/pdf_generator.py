#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 報告生成器

新版路徑：structured payload + Jinja2 template + WeasyPrint
舊版路徑：Markdown 直轉 HTML，僅作相容 fallback
"""

import os
import re
import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# WeasyPrint 需要 gobject/pango/cairo 動態連結庫
if os.uname().sysname == "Darwin":
    os.environ.setdefault("DYLD_LIBRARY_PATH", "/Users/w/.homebrew/lib")

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from weasyprint import HTML, CSS  # noqa: E402

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "stock_report_pdf_v2.html.j2"

THEME = {
    "ink": "#1f2937",
    "muted": "#6b7280",
    "line": "#d1d5db",
    "line_dark": "#9ca3af",
    "paper": "#ffffff",
    "paper_soft": "#f8fafc",
    "navy": "#0f172a",
    "navy_soft": "#1e3a5f",
    "blue": "#1d4ed8",
    "red": "#b91c1c",
    "green": "#047857",
    "gold": "#a16207",
}


def _format_number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _safe_text(value: Any, default: str = "—") -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _bucket_label(symbol: str) -> str:
    if symbol in {"^TWII", "TWII"}:
        return "台股指數"
    if symbol in {"^DJI", "^IXIC", "NVDA", "TSM"}:
        return "美股關鍵"
    return "重點觀察"


def _change_class(change_text: str) -> str:
    if "▲" in change_text or "+" in change_text:
        return "is-up"
    if "▼" in change_text or "-" in change_text:
        return "is-down"
    return ""


def _render_market_rows(market: Dict[str, Any]) -> list[Dict[str, Any]]:
    rows = []
    for key in ["tw_index", "tw_key", "us_key"]:
        for item in market.get(key) or []:
            rows.append(
                {
                    "name": _safe_text(item.get("name")),
                    "price_text": _safe_text(item.get("price_text")),
                    "change_text": _safe_text(item.get("change_text")),
                    "change_class": _change_class(_safe_text(item.get("change_text"), "")),
                    "bucket_label": _bucket_label(_safe_text(item.get("symbol"), "")),
                }
            )
    return rows


def _leader_text(items: list[Dict[str, Any]]) -> str:
    if not items:
        return "未提供"
    parts = []
    for item in items[:5]:
        name = _safe_text(item.get("name") or item.get("symbol"))
        lots = item.get("today_lots")
        try:
            lot_text = f"{float(lots):+,.0f}張"
        except (TypeError, ValueError):
            lot_text = "未提供"
        parts.append(f"{name}{lot_text}")
    return "、".join(parts) if parts else "未提供"


def _normalize_formal_payload(report_payload: Dict[str, Any]) -> Dict[str, Any]:
    meta = report_payload.get("meta") or {}
    market_overview = report_payload.get("market_overview") or {}
    appendix = report_payload.get("appendix") or {}
    cover = report_payload.get("cover") or {}

    normalized = {
        "meta": {
            "title": _safe_text(meta.get("title"), "台股每日報告"),
            "subtitle": _safe_text(meta.get("subtitle"), "券商式盤後摘要"),
            "report_date": _safe_text(meta.get("report_date"), datetime.now().strftime("%Y-%m-%d")),
            "generated_at": _safe_text(meta.get("generated_at"), datetime.now().strftime("%Y-%m-%d %H:%M")),
            "version": _safe_text(meta.get("version"), "v2"),
            "source_note": _safe_text(meta.get("source_note"), "資料來源：盤後行情、法人籌碼、觀察池與 AI 分析。"),
        },
        "headline": {
            "judgment": _safe_text((report_payload.get("headline") or {}).get("judgment"), "今日無判讀資料"),
            "stance": _safe_text((report_payload.get("headline") or {}).get("stance"), ""),
        },
        "executive_summary": {
            "market_summary": _safe_text((report_payload.get("executive_summary") or {}).get("market_summary"), "市場摘要暫缺"),
            "shortline_summary": _safe_text((report_payload.get("executive_summary") or {}).get("shortline_summary"), "短線摘要暫缺"),
            "risk_summary": _safe_text((report_payload.get("executive_summary") or {}).get("risk_summary"), "風險摘要暫缺"),
            "next_action_summary": _safe_text((report_payload.get("executive_summary") or {}).get("next_action_summary"), "明日行動摘要暫缺"),
        },
        "cover": {
            "kpis": cover.get("kpis") or [],
            "market_snapshot": cover.get("market_snapshot") or market_overview.get("snapshot_rows") or [],
            "action_items": cover.get("action_items") or report_payload.get("action_items") or [],
        },
        "market_overview": {
            "snapshot_rows": market_overview.get("snapshot_rows") or [],
            "institutional_summary": _safe_text(market_overview.get("institutional_summary"), ""),
            "institutional_leaders": market_overview.get("institutional_leaders") or [],
        },
        "watchlist_overview": report_payload.get("watchlist_overview") or {
            "short_term_count": 0,
            "medium_term_count": 0,
            "long_term_count": 0,
            "intraday_count": 0,
            "cross_signals_count": 0,
        },
        "shortline_focus": report_payload.get("shortline_focus") or [],
        "cross_pool_focus": report_payload.get("cross_pool_focus") or [],
        "intraday_focus": report_payload.get("intraday_focus") or [],
        "risk_alerts": report_payload.get("risk_alerts") or [],
        "action_items": report_payload.get("action_items") or [],
        "tracking_sections": report_payload.get("tracking_sections") or [],
        "appendix": {
            "medium_term_focus": appendix.get("medium_term_focus") or [],
            "long_term_focus": appendix.get("long_term_focus") or [],
            "data_notes": appendix.get("data_notes") or [],
            "method_note": _safe_text(appendix.get("method_note"), ""),
        },
        "narrative": {
            "market_commentary": _safe_text((report_payload.get("narrative") or {}).get("market_commentary"), ""),
            "shortline_commentary": _safe_text((report_payload.get("narrative") or {}).get("shortline_commentary"), ""),
            "cross_intraday_commentary": _safe_text((report_payload.get("narrative") or {}).get("cross_intraday_commentary"), ""),
            "risk_commentary": _safe_text((report_payload.get("narrative") or {}).get("risk_commentary"), ""),
        },
        "disclaimer": _safe_text(report_payload.get("disclaimer"), "本報告僅供資訊整理與研究參考，不構成投資建議。"),
    }
    return normalized


def _render_report_html(report_payload: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(**_normalize_formal_payload(report_payload))


def _build_formal_report_css() -> str:
    t = THEME
    return f"""
    @font-face {{
        font-family: "ReportCJK";
        src: url("file:///System/Library/Fonts/Hiragino%20Sans%20GB.ttc");
    }}
    @font-face {{
        font-family: "ReportCJK";
        src: url("file:///System/Library/Fonts/STHeiti%20Light.ttc");
        font-weight: 300;
    }}
    @font-face {{
        font-family: "ReportCJK";
        src: url("file:///System/Library/Fonts/STHeiti%20Medium.ttc");
        font-weight: 600;
    }}

    @page {{
        size: A4;
        margin: 16mm 15mm 17mm 15mm;
        @top-right {{
            content: "台股每日報告｜" counter(page);
            font-size: 8pt;
            color: {t['muted']};
            font-family: "ReportCJK", "Hiragino Sans GB", "STHeiti", sans-serif;
        }}
    }}

    * {{ box-sizing: border-box; }}
    html, body {{
        font-family: "ReportCJK", "Hiragino Sans GB", "STHeiti", sans-serif;
        font-kerning: none;
        font-variant-ligatures: none;
        letter-spacing: 0;
        word-spacing: 0;
        -webkit-font-smoothing: antialiased;
    }}
    body {{
        margin: 0;
        color: {t['ink']};
        font-size: 10.7pt;
        line-height: 1.68;
        background: {t['paper']};
    }}

    .page-break-after {{ page-break-after: always; }}
    .avoid-break {{ page-break-inside: avoid; break-inside: avoid; }}

    .cover {{
        min-height: 254mm;
        padding: 10mm 0 7mm;
        display: flex;
        flex-direction: column;
        gap: 4.2mm;
    }}
    .cover-topbar {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 8mm;
        padding: 0 0 5mm;
        border-bottom: 1px solid {t['line']};
    }}
    .cover-eyebrow {{
        font-size: 8.3pt;
        letter-spacing: 1px;
        color: {t['muted']};
        margin-bottom: 3.2mm;
    }}
    .cover h1 {{
        margin: 0 0 1.8mm;
        font-size: 22.5pt;
        line-height: 1.12;
        color: {t['navy']};
        font-weight: 700;
    }}
    .cover-subtitle {{
        font-size: 10.6pt;
        color: {t['navy_soft']};
    }}
    .cover-version-panel {{
        min-width: 28mm;
        text-align: right;
    }}
    .version-chip {{
        display: inline-block;
        padding: 1.6mm 3mm;
        background: {t['navy']};
        color: #fff;
        font-size: 8pt;
        font-weight: 700;
        border-radius: 999px;
        margin-bottom: 2.1mm;
    }}
    .version-meta {{
        font-size: 8.4pt;
        color: {t['muted']};
    }}
    .cover-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 3mm 5mm;
        font-size: 8.6pt;
        color: {t['muted']};
    }}
    .source-note {{
        margin: auto 0 0;
        padding-top: 3mm;
        border-top: 1px solid {t['line']};
        color: {t['muted']};
        font-size: 8.4pt;
    }}

    .headline-block {{
        padding: 6.2mm 7mm;
        background: linear-gradient(135deg, {t['navy']} 0%, {t['navy_soft']} 100%);
        color: #fff;
        border-radius: 4mm;
    }}
    .section-label {{
        font-size: 7.7pt;
        letter-spacing: 0.6px;
        text-transform: uppercase;
        opacity: 0.8;
        margin-bottom: 1.6mm;
    }}
    .headline-text {{
        font-size: 13.2pt;
        line-height: 1.45;
        font-weight: 600;
    }}
    .headline-stance {{
        margin-top: 2.5mm;
        font-size: 8.8pt;
        line-height: 1.5;
        color: #dbeafe;
    }}

    .cover-kpi-strip {{
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 2.6mm;
    }}
    .kpi-card {{
        border: 1px solid {t['line']};
        border-radius: 3mm;
        background: {t['paper_soft']};
        padding: 3mm 3.5mm;
        min-height: 17.5mm;
    }}
    .kpi-card.tone-primary {{ border-top: 2.4px solid {t['navy']}; }}
    .kpi-card.tone-accent {{ border-top: 2.4px solid {t['blue']}; }}
    .kpi-card.tone-neutral {{ border-top: 2.4px solid {t['line_dark']}; }}
    .kpi-label {{
        font-size: 7.9pt;
        color: {t['muted']};
        margin-bottom: 1.1mm;
    }}
    .kpi-value {{
        font-size: 15pt;
        line-height: 1.1;
        color: {t['navy']};
        font-weight: 700;
    }}

    .summary-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 3mm;
    }}
    .summary-card {{
        border: 1px solid {t['line']};
        border-radius: 3.1mm;
        padding: 3.8mm 4.2mm;
        background: {t['paper_soft']};
    }}
    .card-kicker {{
        font-size: 7.2pt;
        letter-spacing: 0.5px;
        color: {t['muted']};
        text-transform: uppercase;
        margin-bottom: 1mm;
    }}
    .summary-card h2 {{
        margin: 0 0 1.5mm;
        font-size: 9.5pt;
        color: {t['navy']};
    }}
    .summary-card p {{
        margin: 0;
        font-size: 9pt;
        line-height: 1.55;
    }}

    .cover-bottom-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 3.2mm;
    }}
    .cover-panel {{
        border: 1px solid {t['line']};
        border-radius: 3.2mm;
        padding: 4mm 4.4mm;
        background: #fcfdff;
    }}
    .panel-header {{ margin-bottom: 2.2mm; }}
    .panel-header h2 {{
        margin: 0;
        font-size: 10.4pt;
        color: {t['navy']};
    }}
    .snapshot-list {{
        display: flex;
        flex-direction: column;
        gap: 2mm;
    }}
    .snapshot-row {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 3.5mm;
        padding-bottom: 1.8mm;
        border-bottom: 1px solid #e5e7eb;
    }}
    .snapshot-row:last-child {{
        padding-bottom: 0;
        border-bottom: none;
    }}
    .snapshot-name {{
        font-size: 9pt;
        font-weight: 600;
        color: {t['navy']};
    }}
    .snapshot-bucket {{
        font-size: 7.7pt;
        color: {t['muted']};
        margin-top: 0.6mm;
    }}
    .snapshot-metrics {{ text-align: right; }}
    .snapshot-price {{
        font-size: 8.9pt;
        font-weight: 600;
        color: {t['ink']};
    }}
    .snapshot-change {{
        font-size: 8.1pt;
        margin-top: 0.6mm;
    }}
    .cover-action-list {{
        margin: 0;
        padding-left: 4.8mm;
    }}
    .cover-action-list li {{
        margin-bottom: 2.2mm;
        padding-left: 0.8mm;
        line-height: 1.56;
        font-size: 8.9pt;
    }}
    .compact-empty {{ padding: 3.4mm; font-size: 8.6pt; }}

    .section {{ margin: 0 0 9mm; }}
    .section-header {{
        display: flex;
        align-items: center;
        gap: 4mm;
        margin: 0 0 4.5mm;
        padding-bottom: 2.6mm;
        border-bottom: 1.4px solid {t['line_dark']};
    }}
    .section-no {{
        width: 10.5mm;
        height: 10.5mm;
        background: {t['navy']};
        color: #fff;
        font-size: 7.8pt;
        font-weight: 700;
        display: flex;
        align-items: center;
        justify-content: center;
        border-radius: 50%;
    }}
    .section-header h2 {{
        margin: 0;
        font-size: 13.6pt;
        color: {t['navy']};
    }}
    .subsection {{ margin: 0 0 6.5mm; }}
    .subsection h3, .narrative-block h3 {{
        margin: 0 0 2mm;
        font-size: 10.2pt;
        color: {t['navy_soft']};
    }}
    .plain-block {{
        padding: 4.2mm 4.8mm;
        border-left: 2.5px solid {t['navy_soft']};
        background: {t['paper_soft']};
        white-space: pre-wrap;
        font-size: 9.4pt;
        line-height: 1.64;
    }}

    .data-table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 4mm;
        table-layout: fixed;
    }}
    .data-table thead {{ display: table-header-group; }}
    .data-table tr {{ page-break-inside: avoid; break-inside: avoid; }}
    .data-table th, .data-table td {{
        border: 1px solid {t['line']};
        padding: 3.1mm 3mm;
        vertical-align: top;
        font-size: 9.15pt;
        line-height: 1.6;
        word-wrap: break-word;
        overflow-wrap: break-word;
    }}
    .data-table th {{
        background: #eef2f7;
        color: {t['navy']};
        font-weight: 700;
    }}
    .compact-table th, .compact-table td {{ font-size: 8.8pt; }}
    .market-table th:nth-child(1) {{ width: 32%; }}
    .market-table th:nth-child(2) {{ width: 21%; }}
    .market-table th:nth-child(3) {{ width: 18%; }}
    .market-table th:nth-child(4) {{ width: 19%; }}
    .focus-table {{
        table-layout: auto;
        margin-top: 2.6mm;
    }}
    .focus-table th, .focus-table td {{
        padding: 3.5mm 3.4mm;
        line-height: 1.65;
    }}
    .focus-table tbody tr:nth-child(even) {{ background: #fbfcff; }}
    .focus-table th:nth-child(1) {{ width: 22%; }}
    .focus-table th:nth-child(2) {{ width: 17%; }}
    .focus-table th:nth-child(3) {{ width: 25%; }}
    .focus-table th:nth-child(4) {{ width: 36%; }}

    .trade-line {{
        display: flex;
        justify-content: space-between;
        gap: 3.2mm;
        margin-bottom: 1.6mm;
        font-size: 9.15pt;
        line-height: 1.58;
    }}
    .trade-line span {{ color: {t['muted']}; }}
    .trade-line strong {{ color: {t['navy']}; font-weight: 600; }}
    .focus-note-block + .focus-note-block {{
        margin-top: 2mm;
        padding-top: 1.8mm;
        border-top: 1px dashed {t['line']};
    }}
    .focus-note-label {{
        margin-bottom: 0.9mm;
        font-size: 7.7pt;
        font-weight: 700;
        letter-spacing: 0.3px;
        color: {t['navy_soft']};
    }}
    .focus-note {{
        line-height: 1.66;
        font-size: 9.25pt;
        color: {t['ink']};
    }}
    .note-gap {{ margin-top: 1.4mm; }}
    .inline-meta {{
        margin-top: 1.8mm;
        font-size: 8.7pt;
        line-height: 1.52;
        color: {t['navy_soft']};
    }}
    .score-chip {{
        display: inline-block;
        margin-bottom: 2mm;
        padding: 0.9mm 2.4mm;
        border-radius: 999px;
        background: #e8eef8;
        color: {t['navy']};
        font-size: 8.5pt;
        font-weight: 700;
    }}
    .price-line {{
        font-size: 9pt;
        line-height: 1.56;
        color: {t['ink']};
    }}
    .institution-note {{
        color: {t['muted']};
        font-size: 8.85pt;
        line-height: 1.62;
    }}

    .is-up {{ color: {t['green']}; font-weight: 700; }}
    .is-down {{ color: {t['red']}; font-weight: 700; }}
    .muted {{ color: {t['muted']}; font-size: 8.1pt; margin-top: 0.7mm; }}
    .name-cell {{ font-weight: 600; color: {t['navy']}; }}

    .leader-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 3mm;
    }}
    .leader-card {{
        border: 1px solid {t['line']};
        border-radius: 3mm;
        padding: 3.7mm 4.2mm;
        background: {t['paper_soft']};
    }}
    .leader-title {{
        margin-bottom: 2mm;
        font-weight: 700;
        color: {t['navy']};
    }}
    .leader-line {{
        display: flex;
        gap: 3mm;
        margin-bottom: 1mm;
        font-size: 8.7pt;
        line-height: 1.5;
    }}
    .leader-line span:first-child {{
        min-width: 9mm;
        color: {t['muted']};
    }}

    .overview-strip {{
        display: flex;
        flex-wrap: wrap;
        gap: 3mm;
        margin-bottom: 5mm;
    }}
    .metric {{
        min-width: 22mm;
        padding: 3mm 4mm;
        border: 1px solid {t['line']};
        border-radius: 3mm;
        background: {t['paper_soft']};
    }}
    .metric span {{
        display: block;
        font-size: 7.8pt;
        color: {t['muted']};
        margin-bottom: 1mm;
    }}
    .metric strong {{
        font-size: 11.5pt;
        color: {t['navy']};
    }}

    .narrative-block {{
        margin-top: 3.5mm;
        padding: 4.2mm 4.8mm;
        border: 1px solid {t['line']};
        border-radius: 3mm;
        background: #fcfcfd;
    }}
    .narrative-block p {{
        margin: 0 0 2.1mm;
        font-size: 9.3pt;
        line-height: 1.7;
    }}
    .narrative-block p:last-child {{ margin-bottom: 0; }}
    .narrative-risk {{ border-left: 3px solid {t['gold']}; }}
    .narrative-shortline {{ background: #fbfcff; }}
    .shortline-summary-block {{
        margin-bottom: 3.4mm;
        border-left-color: {t['navy']};
        background: #f8fbff;
        font-size: 9.25pt;
    }}

    .risk-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 3.2mm;
    }}
    .risk-card {{
        border: 1px solid #f3d7a6;
        border-left: 4px solid {t['gold']};
        border-radius: 3mm;
        background: #fffaf0;
        padding: 4mm 4.4mm;
    }}
    .risk-name {{
        font-size: 9.4pt;
        font-weight: 700;
        color: {t['navy']};
        margin-bottom: 1.2mm;
    }}
    .risk-price {{
        font-size: 8.4pt;
        color: {t['muted']};
        margin-bottom: 1.6mm;
    }}
    .risk-reason {{
        font-size: 9pt;
        line-height: 1.56;
    }}

    .empty-state {{
        padding: 5mm;
        border: 1px dashed {t['line_dark']};
        color: {t['muted']};
        background: {t['paper_soft']};
        border-radius: 3mm;
        font-size: 8.8pt;
    }}

    .action-section {{ page-break-inside: auto; break-inside: auto; }}
    .action-list {{
        margin: 0;
        padding-left: 0;
        list-style: none;
    }}
    .action-card-list li {{
        margin-bottom: 2.8mm;
        padding: 3.8mm 4.4mm;
        border: 1px solid {t['line']};
        border-radius: 3mm;
        background: {t['paper_soft']};
        counter-increment: action-step;
        position: relative;
        padding-left: 12mm;
        line-height: 1.6;
        font-size: 9.1pt;
    }}
    .action-card-list {{ counter-reset: action-step; }}
    .action-card-list li::before {{
        content: counter(action-step, decimal-leading-zero);
        position: absolute;
        left: 4mm;
        top: 3.6mm;
        font-size: 8pt;
        font-weight: 700;
        color: {t['navy_soft']};
    }}

    .appendix-section {{ opacity: 0.95; }}
    .appendix-section .section-header h2 {{ font-size: 12.6pt; }}
    .appendix-list {{
        margin: 0;
        padding-left: 4.5mm;
    }}
    .appendix-list li {{
        margin-bottom: 1.8mm;
        font-size: 8.9pt;
        line-height: 1.55;
    }}
    .appendix-section p {{ font-size: 9pt; line-height: 1.58; }}

    .report-footer {{
        margin-top: 9mm;
        padding-top: 4mm;
        border-top: 1px solid {t['line']};
        color: {t['muted']};
        font-size: 8.3pt;
    }}
    """


def generate_formal_stock_report_pdf(report_payload: Dict[str, Any], output_path: Path) -> Optional[Path]:
    try:
        html_text = _render_report_html(report_payload)
        css = CSS(string=_build_formal_report_css())
        HTML(string=html_text, base_url=str(TEMPLATE_DIR)).write_pdf(str(output_path), stylesheets=[css])
        size_kb = output_path.stat().st_size / 1024
        logger.info("正式 PDF 生成成功：%s（%.1f KB）", output_path, size_kb)
        return output_path
    except Exception as e:
        logger.error("正式 PDF 生成失敗：%s", e, exc_info=True)
        return None


# ===== 相容舊版 Markdown 路徑 =====
def _md_to_html(md_text: str, title: str = "台股每日報告", date_str: str = "") -> str:
    html_body = html.escape(md_text)

    judgment_match = re.search(r'^## 一、今日一句話判斷\s*\n(.+?)(?=\n## |\Z)', md_text, re.MULTILINE | re.DOTALL)
    if judgment_match:
        judgment_text = html.escape(judgment_match.group(1).strip())
        old_block = html.escape(judgment_match.group(0))
        new_block = f'## 一、今日一句話判斷\n<div class="judgment-box">{judgment_text}</div>\n'
        html_body = html_body.replace(old_block, new_block, 1)

    def _h2_repl(m):
        text = html.escape(m.group(1))
        num_match = re.match(r'^([一二三四五六七八九十]+、)', m.group(1))
        if num_match:
            num = html.escape(num_match.group(1))
            rest = html.escape(m.group(1)[len(num_match.group(1)):])
            return f'<h2><span class="section-num">{num}</span>{rest}</h2>'
        return f'<h2>{text}</h2>'

    html_body = re.sub(r'^### (.*)$', lambda m: f"<h3>{html.escape(m.group(1))}</h3>", html_body, flags=re.MULTILINE)
    html_body = re.sub(r'^## (.*)$', _h2_repl, html_body, flags=re.MULTILINE)
    html_body = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html_body)
    html_body = re.sub(r'^\s*---+\s*$', '<hr class="section-divider">', html_body, flags=re.MULTILINE)
    html_body = re.sub(r'^\s*[-•] (.*)$', r'<li>\1</li>', html_body, flags=re.MULTILINE)
    html_body = re.sub(r'^(\d+)\. (.*)$', r'<li value="\1">\2</li>', html_body, flags=re.MULTILINE)

    lines = html_body.split("\n")
    result_lines = []
    in_list = False
    list_type = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("<li>"):
            if not in_list:
                if stripped.startswith("<li value="):
                    list_type = "ol"
                    result_lines.append("<ol>")
                else:
                    list_type = "ul"
                    result_lines.append("<ul>")
                in_list = True
            result_lines.append(line)
        else:
            if in_list:
                result_lines.append("</ol>" if list_type == "ol" else "</ul>")
                in_list = False
                list_type = None
            result_lines.append(line)
    if in_list:
        result_lines.append("</ol>" if list_type == "ol" else "</ul>")

    html_body = "\n".join(result_lines)
    wrapped = []
    for line in html_body.split("\n"):
        stripped = line.strip()
        if not stripped:
            wrapped.append("")
        elif stripped.startswith("<") and stripped.endswith(">"):
            wrapped.append(line)
        else:
            wrapped.append(f"<p>{stripped}</p>")
    html_body = "\n".join(wrapped)
    html_body = re.sub(r"<p></p>", "", html_body)
    html_body = re.sub(r"\n{3,}", "\n\n", html_body)
    html_body = re.sub(
        r'([+-]\d+\.?\d*%)',
        lambda m: f'<span class="{"up" if m.group(1).startswith("+") else "down"}">{m.group(1)}</span>',
        html_body,
    )
    return html_body


def _build_legacy_css() -> str:
    return f"""
    @page {{ size: A4; margin: 2.2cm 2cm 2.5cm 2cm; }}
    body {{ font-family: "PingFang TC", "Hiragino Sans", sans-serif; font-size: 10.5pt; color: {THEME['ink']}; line-height: 1.75; }}
    .cover {{ text-align: center; padding: 3.5cm 1cm 2.5cm; margin-bottom: 1.8cm; }}
    .cover h1 {{ font-size: 28pt; color: {THEME['navy']}; margin-bottom: 0.6cm; }}
    .cover .date {{ font-size: 12pt; color: {THEME['muted']}; margin-bottom: 0.8cm; }}
    .cover .badge {{ display: inline-block; background: {THEME['navy_soft']}; color: white; padding: 5px 20px; border-radius: 20px; font-size: 9pt; }}
    h2 {{ font-size: 15pt; color: {THEME['navy']}; margin-top: 1.4cm; margin-bottom: 0.5cm; border-bottom: 2px solid {THEME['navy_soft']}; padding-bottom: 8px; }}
    h3 {{ font-size: 12pt; color: {THEME['navy_soft']}; margin-top: 0.9cm; margin-bottom: 0.35cm; }}
    p {{ margin-bottom: 0.45cm; text-align: justify; }}
    ul, ol {{ margin-bottom: 0.6cm; padding-left: 1.5em; }}
    .up {{ color: {THEME['green']}; font-weight: 600; }}
    .down {{ color: {THEME['red']}; font-weight: 600; }}
    .judgment-box {{ background: {THEME['navy']}; color: white; padding: 16px 20px; font-size: 12pt; margin: 0.5cm 0 0.8cm; }}
    .section-num {{ display: inline-block; min-width: 28px; background: {THEME['navy_soft']}; color: white; padding: 2px 6px; margin-right: 8px; }}
    hr.section-divider {{ border: none; border-top: 1px solid {THEME['line']}; margin: 1cm 0; }}
    """


def generate_pdf(md_content: str, output_path: Path, title: str = "台股每日報告", date_str: str = "") -> Optional[Path]:
    try:
        html_body = _md_to_html(md_content, title, date_str)
        date_display = date_str or ""
        full_html = f"""<!DOCTYPE html>
<html lang=\"zh-TW\">
<head>
    <meta charset=\"UTF-8\">
    <title>{html.escape(title)}</title>
</head>
<body>
    <div class=\"cover\">
        <h1>{html.escape(title)}</h1>
        {f'<div class="date">{html.escape(date_display)}</div>' if date_display else ''}
        <div class=\"badge\">AI 助理團隊自動生成</div>
    </div>
    {html_body}
</body>
</html>"""
        css = CSS(string=_build_legacy_css())
        HTML(string=full_html).write_pdf(str(output_path), stylesheets=[css])
        size_kb = output_path.stat().st_size / 1024
        logger.info("舊版 PDF 生成成功：%s（%.1f KB）", output_path, size_kb)
        return output_path
    except Exception as e:
        logger.error("舊版 PDF 生成失敗：%s", e, exc_info=True)
        return None


def generate_stock_report_pdf(
    base_path: Path,
    report_payload: Optional[Dict[str, Any]] = None,
    md_content: str = "",
    date_str: str = "",
) -> Optional[Path]:
    pdf_path = base_path.with_suffix(".pdf")
    if not report_payload:
        logger.error("缺少正式報告 payload，拒絕生成舊版 fallback PDF")
        return None
    return generate_formal_stock_report_pdf(report_payload=report_payload, output_path=pdf_path)
