#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_agent.py — 記憶管家 W

責任：
1. 吸收當日所有產出檔（日報/新聞/股市 brief/警報/研究報告/週反思）
2. 用 Markdown chunking 切段後嵌入 ChromaDB
3. 提供語意檢索介面（支援日期過濾、來源過濾、時間衰減重排）
4. 初始化失敗會 raise（不再靜默變空殼）
5. CLI 支援單日或範圍回填
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 清除代理變數，避免 HF 模型下載 / ChromaDB 請求出錯
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import chromadb
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    chromadb = None
    SentenceTransformer = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

from utils.text_chunker import chunk_markdown

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent


class MemoryAgent:
    """記憶管家：把每日文件嵌入向量庫並提供檢索。"""

    def __init__(self, persist_dir: str = "logs/chroma_db") -> None:
        if _IMPORT_ERROR is not None or chromadb is None or SentenceTransformer is None:
            raise RuntimeError(
                "Memory agent optional dependencies are unavailable. "
                "Run `pip3 install -r requirements.txt` in /Users/w/Cursor/automation. "
                f"Original error: {_IMPORT_ERROR}"
            )

        self.persist_dir = str(_BASE_DIR / persist_dir)
        os.makedirs(self.persist_dir, exist_ok=True)

        # 初始化 ChromaDB 本地檔案模式
        self.client = chromadb.PersistentClient(path=self.persist_dir)

        # 初始化 embedding 模型（多語系輕量模型，支援繁體中文）
        try:
            self.embed_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

            class _EmbeddingFn(chromadb.EmbeddingFunction):
                def __init__(self, model: Any) -> None:
                    self.model = model

                def __call__(self, input: chromadb.Documents) -> chromadb.Embeddings:
                    return self.model.encode(list(input)).tolist()

            self.embedding_function = _EmbeddingFn(self.embed_model)
        except Exception as e:
            logger.error("Failed to load sentence-transformers model: %s", e)
            raise RuntimeError(f"Memory agent embedding model failed to load: {e}") from e

        self.collection_name = "w_memory_pool"
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=self.embedding_function,
        )

    # ──────────────────────────────────────────────────────────────────────
    # 儲存
    # ──────────────────────────────────────────────────────────────────────

    def store_document(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """[向後相容] 把整份文件存成單一 document。建議改用 store_chunked_document。"""
        if not content.strip():
            return False
        try:
            self.collection.upsert(
                documents=[content],
                metadatas=[metadata or {"date": datetime.now().strftime("%Y-%m-%d")}],
                ids=[doc_id],
            )
            return True
        except Exception as e:
            logger.error("Failed to store document %s: %s", doc_id, e)
            return False

    def store_chunked_document(
        self,
        doc_id_prefix: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        max_chars: int = 800,
        overlap: int = 100,
    ) -> int:
        """
        把文件切成 chunks 後存入向量庫。回傳成功寫入的 chunk 數。
        重跑會 upsert 覆蓋同樣的 doc_id（避免重複）。
        """
        if not content or not content.strip():
            return 0

        chunks = chunk_markdown(content, max_chars=max_chars, overlap=overlap)
        if not chunks:
            return 0

        base_meta = dict(metadata or {})
        base_meta.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
        base_meta["source_id"] = doc_id_prefix
        base_meta["chunk_total"] = len(chunks)

        documents: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        ids: List[str] = []

        for chunk in chunks:
            chunk_id = f"{doc_id_prefix}_chunk_{chunk['order']:03d}"
            chunk_meta = dict(base_meta)
            chunk_meta["chunk_index"] = chunk["order"]
            chunk_meta["heading"] = chunk["heading"]
            documents.append(chunk["content"])
            metadatas.append(chunk_meta)
            ids.append(chunk_id)

        try:
            # 先刪除舊 chunks（避免 chunk_total 變小時殘留）
            try:
                self.collection.delete(where={"source_id": doc_id_prefix})
            except Exception:
                pass
            self.collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
            return len(documents)
        except Exception as e:
            logger.error("Failed to store chunked document %s: %s", doc_id_prefix, e)
            return 0

    # ──────────────────────────────────────────────────────────────────────
    # 檢索
    # ──────────────────────────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        *,
        n_results: int = 3,
        days_back: Optional[int] = None,
        source_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        依語意從記憶庫檢索。
        - days_back：只查最近 N 天內的資料
        - source_filter：只查指定來源（如 ["stock_brief", "stock_alerts"]）
        - 結果按 distance * 時間衰減重排
        """
        if not self.collection:
            return []

        # 多抓一些，好讓時間衰減 + source filter 之後還有足夠結果
        raw_n = max(n_results * 4, 10)

        # ChromaDB 1.5.5+ 的 $gte 只接受 int/float，不支援字串日期比較。
        # 因此 days_back 不再放進 where clause，改由 Python 在取回結果後過濾。
        # source_filter 仍可安全地放進 where clause。
        where: Dict[str, Any] = {}
        if source_filter:
            if len(source_filter) == 1:
                where = {"source": source_filter[0]}
            else:
                where = {"source": {"$in": source_filter}}

        try:
            kwargs: Dict[str, Any] = {
                "query_texts": [query],
                "n_results": raw_n,
            }
            if where:
                kwargs["where"] = where
            results = self.collection.query(**kwargs)
        except Exception as e:
            logger.error("Memory retrieve failed for query '%s': %s", query, e)
            return []

        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        ids = (results.get("ids") or [[]])[0]
        distances = (results.get("distances") or [[0.0] * len(documents)])[0]

        now = datetime.now()
        cutoff_date: Optional[datetime] = None
        if days_back is not None and days_back > 0:
            cutoff_date = now - timedelta(days=days_back)

        scored: List[Dict[str, Any]] = []
        for i in range(len(documents)):
            meta = metadatas[i] if metadatas else {}
            distance = distances[i] if distances else 0.0
            # 時間衰減：越久前的 score 越大（distance 越大代表越不相關）
            age_days = 0
            date_str = str(meta.get("date") or "")
            if date_str:
                try:
                    doc_date = datetime.strptime(date_str, "%Y-%m-%d")
                    age_days = max(0, (now - doc_date).days)
                    # 硬性日期截止：超出 days_back 範圍的直接跳過
                    if cutoff_date and doc_date < cutoff_date:
                        continue
                except ValueError:
                    pass
            decay = 1 + (age_days / 60.0)
            scored.append({
                "id": ids[i],
                "content": documents[i],
                "metadata": meta,
                "distance": distance,
                "score": distance * decay,
                "age_days": age_days,
            })

        scored.sort(key=lambda x: x["score"])
        return scored[:n_results]

    # ──────────────────────────────────────────────────────────────────────
    # 每日吸收
    # ──────────────────────────────────────────────────────────────────────

    def _ingest_file(
        self,
        path: Path,
        doc_id_prefix: str,
        metadata: Dict[str, Any],
        *,
        log_label: str,
    ) -> int:
        if not path.exists():
            return 0
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("讀取 %s 失敗: %s", path, e)
            return 0
        count = self.store_chunked_document(doc_id_prefix, content, metadata)
        if count > 0:
            logger.info("Ingested %s (%d chunks)", log_label, count)
        else:
            logger.warning("Skipped %s (0 chunks)", log_label)
        return count

    def _ingest_stock_alerts(self, date_str: str) -> int:
        """吸收當日警報 JSON，把 price/change_pct 存到 metadata。"""
        alerts_file = _BASE_DIR / "logs" / "stock_alerts_today.json"
        if not alerts_file.exists():
            return 0

        try:
            data = json.loads(alerts_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("讀取警報檔失敗: %s", e)
            return 0

        alerts = data.get("alerts", [])
        if not alerts:
            return 0

        file_date = str(data.get("date") or date_str)
        mode = str(data.get("mode") or "daily")

        # 一筆警報 → 一個 document（小量且每筆都想要精準檢索）
        total = 0
        for i, alert in enumerate(alerts):
            name = alert.get("name", "未知標的")
            symbol = alert.get("symbol", "N/A")
            level = alert.get("alert", "info")
            change_pct = alert.get("change_pct", 0.0)
            price = alert.get("price", 0.0)
            content = (
                f"股市異常警報 · {file_date}（{mode}）\n"
                f"標的：{name if name.endswith(f'（{symbol}）') else f'{name}（{symbol}）'}\n"
                f"價格：{price}\n"
                f"漲跌幅：{change_pct}%\n"
                f"警示級別：{level}"
            )
            doc_id = f"alerts_{file_date}_{mode}_{symbol.replace('.', '_').replace('^', 'idx_')}_{i}"
            metadata = {
                "source": "stock_alerts",
                "date": file_date,
                "mode": mode,
                "symbol": symbol,
                "name": name,
                "change_pct": float(change_pct) if isinstance(change_pct, (int, float, str)) and str(change_pct).replace("-", "").replace(".", "").isdigit() else 0.0,
                "price": float(price) if isinstance(price, (int, float)) else 0.0,
                "alert_level": level,
            }
            try:
                self.collection.upsert(
                    documents=[content],
                    metadatas=[metadata],
                    ids=[doc_id],
                )
                total += 1
            except Exception as e:
                logger.warning("警報 %s upsert 失敗: %s", doc_id, e)

        if total > 0:
            logger.info("Ingested %d stock alert records for %s", total, file_date)
        return total

    def ingest_daily_files(self, date_str: Optional[str] = None) -> Dict[str, int]:
        """
        吸收指定日期的所有產出檔。若 date_str 為 None 則為今日。
        回傳各來源吸收的 chunk 數字典。
        """
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            logger.error("Invalid date_str '%s', expected YYYY-MM-DD", date_str)
            return {}

        counts: Dict[str, int] = {}

        # 1. 日報
        counts["daily_report"] = self._ingest_file(
            _BASE_DIR / "logs" / "daily_reports" / f"{date_str}.md",
            doc_id_prefix=f"diary_{date_str}",
            metadata={"source": "daily_report", "date": date_str},
            log_label=f"daily_report {date_str}",
        )

        # 2. 每日新聞
        counts["daily_news"] = self._ingest_file(
            _BASE_DIR / "logs" / "daily_news" / f"{date_str}.md",
            doc_id_prefix=f"news_{date_str}",
            metadata={"source": "daily_news", "date": date_str},
            log_label=f"daily_news {date_str}",
        )

        # 3. 股市 brief（morning & afternoon）
        for mode in ("morning", "afternoon"):
            brief_path = _BASE_DIR / "logs" / "stock_briefs" / f"{date_str}_{mode}.md"
            counts[f"stock_brief_{mode}"] = self._ingest_file(
                brief_path,
                doc_id_prefix=f"stock_brief_{date_str}_{mode}",
                metadata={"source": "stock_brief", "date": date_str, "mode": mode},
                log_label=f"stock_brief {date_str} {mode}",
            )

        # 4. 股市警報（metadata 化）
        counts["stock_alerts"] = self._ingest_stock_alerts(date_str)

        # 5. 深度研究報告（當日所有）
        reports_dir = _BASE_DIR / "logs" / "research_reports"
        research_total = 0
        if reports_dir.exists():
            for report_path in sorted(reports_dir.glob(f"{day.strftime('%Y%m%d')}_*.md")):
                research_total += self._ingest_file(
                    report_path,
                    doc_id_prefix=f"research_{report_path.stem}",
                    metadata={
                        "source": "research_report",
                        "date": date_str,
                        "file": report_path.name,
                    },
                    log_label=f"research_report {report_path.name}",
                )
        counts["research_reports"] = research_total

        # 6. 週日的週反思
        if day.weekday() == 6:
            week_num = day.isocalendar()[1]
            counts["weekly_reflection"] = self._ingest_file(
                _BASE_DIR / "logs" / "reflections" / f"{day.year}-W{week_num:02d}.md",
                doc_id_prefix=f"reflection_{day.year}_W{week_num:02d}",
                metadata={
                    "source": "weekly_reflection",
                    "date": date_str,
                    "week": week_num,
                },
                log_label=f"weekly_reflection {day.year}-W{week_num:02d}",
            )

        # 7. 短線選股敘事
        counts["screener"] = self._ingest_file(
            _BASE_DIR / "logs" / "screener" / f"{date_str}_narrative.md",
            doc_id_prefix=f"screener_{date_str}",
            metadata={"source": "screener", "date": date_str},
            log_label=f"screener {date_str}",
        )

        # 8. 週五的選股復盤
        if day.weekday() == 4:
            week_num = day.isocalendar()[1]
            counts["screener_review"] = self._ingest_file(
                _BASE_DIR / "logs" / "screener_review" / f"{day.year}-W{week_num:02d}.md",
                doc_id_prefix=f"screener_review_{day.year}_W{week_num:02d}",
                metadata={
                    "source": "screener_review",
                    "date": date_str,
                    "week": week_num,
                },
                log_label=f"screener_review {day.year}-W{week_num:02d}",
            )

        total_chunks = sum(counts.values())
        logger.info("🧠 記憶管家吸收完成 %s：%s（共 %d chunks）", date_str, counts, total_chunks)
        return counts

    def ingest_date_range(self, start: str, end: str) -> Dict[str, Dict[str, int]]:
        """吸收一段日期範圍（含起訖）。"""
        try:
            start_day = datetime.strptime(start, "%Y-%m-%d")
            end_day = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            logger.error("Invalid date range %s:%s", start, end)
            return {}

        results: Dict[str, Dict[str, int]] = {}
        cursor = start_day
        while cursor <= end_day:
            ds = cursor.strftime("%Y-%m-%d")
            results[ds] = self.ingest_daily_files(ds)
            cursor += timedelta(days=1)
        return results

    # ──────────────────────────────────────────────────────────────────────
    # 遺忘 / 封存機制
    # ──────────────────────────────────────────────────────────────────────

    def archive_old_memories(self, days: int = 90) -> Dict[str, int]:
        """
        封存舊記憶（不移除，只降低檢索權重）

        Args:
            days: 多少天前的記憶視為「舊」

        Returns:
            統計結果
        """
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            # 查詢舊記憶
            old_docs = self.collection.get(
                where={"date": {"$lt": cutoff_date}}
            )

            if not old_docs or not old_docs.get("ids"):
                logger.info("沒有需要封存的舊記憶")
                return {"archived": 0}

            ids = old_docs["ids"]
            metadatas = old_docs.get("metadatas", [])

            # 更新 metadata，標記為已封存
            updated_metadatas = []
            for meta in metadatas:
                if meta:
                    meta["archived"] = True
                    meta["archive_date"] = datetime.now().isoformat()
                updated_metadatas.append(meta)

            # 批量更新
            self.collection.update(
                ids=ids,
                metadatas=updated_metadatas
            )

            logger.info(f"📦 已封存 {len(ids)} 筆 {days} 天前的記憶")
            return {"archived": len(ids)}

        except Exception as e:
            logger.error(f"封存舊記憶失敗: {e}")
            return {"archived": 0, "error": str(e)}

    def cleanup_orphaned_chunks(self) -> Dict[str, int]:
        """
        清理孤立 chunks（source_id 不存在對應主文件的）

        Returns:
            清理統計
        """
        try:
            # 取得所有 chunks
            all_docs = self.collection.get()

            if not all_docs or not all_docs.get("ids"):
                return {"cleaned": 0}

            # 檢查每個 source_id 對應的原始檔案是否存在
            to_delete = []
            ids = all_docs.get("ids", [])
            metadatas = all_docs.get("metadatas", [])

            for i, meta in enumerate(metadatas):
                if not meta:
                    continue

                source_id = meta.get("source_id", "")
                date_str = meta.get("date", "")

                # 檢查原始檔案是否存在
                if source_id.startswith("diary_"):
                    file_path = _BASE_DIR / "logs" / "daily_reports" / f"{date_str}.md"
                elif source_id.startswith("news_"):
                    file_path = _BASE_DIR / "logs" / "daily_news" / f"{date_str}.md"
                elif source_id.startswith("stock_brief_"):
                    parts = source_id.split("_")
                    if len(parts) >= 4:
                        file_path = _BASE_DIR / "logs" / "stock_briefs" / f"{date_str}_{parts[-1]}.md"
                    else:
                        file_path = None
                elif source_id.startswith("screener_"):
                    file_path = _BASE_DIR / "logs" / "screener" / f"{date_str}_narrative.md"
                else:
                    file_path = None

                if file_path and not file_path.exists():
                    to_delete.append(ids[i])

            if to_delete:
                self.collection.delete(ids=to_delete)
                logger.info(f"🗑️ 已清理 {len(to_delete)} 筆孤立 chunks")

            return {"cleaned": len(to_delete)}

        except Exception as e:
            logger.error(f"清理孤立 chunks 失敗: {e}")
            return {"cleaned": 0, "error": str(e)}

    def get_memory_stats(self) -> Dict[str, Any]:
        """
        取得記憶庫統計

        Returns:
            統計資訊
        """
        try:
            # 總數
            total = self.collection.count()

            # 封存數
            try:
                archived = self.collection.get(where={"archived": True})
                archived_count = len(archived.get("ids", [])) if archived else 0
            except Exception:
                archived_count = 0

            # 今日新增
            today_str = datetime.now().strftime("%Y-%m-%d")
            try:
                today_docs = self.collection.get(where={"date": today_str})
                today_count = len(today_docs.get("ids", [])) if today_docs else 0
            except Exception:
                today_count = 0

            return {
                "total_documents": total,
                "archived_documents": archived_count,
                "active_documents": total - archived_count,
                "today_added": today_count,
            }

        except Exception as e:
            logger.error(f"取得記憶統計失敗: {e}")
            return {"error": str(e)}


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="記憶管家 W：每日吸收 / 回填 / 維護")
    parser.add_argument("--date", type=str, help="單日回填 YYYY-MM-DD")
    parser.add_argument("--range", type=str, help="範圍回填 YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--query", type=str, help="測試檢索某個 query")
    parser.add_argument("--cleanup", action="store_true", help="清理孤立 chunks")
    parser.add_argument("--archive", action="store_true", help="封存 90 天前的記憶")
    parser.add_argument("--stats", action="store_true", help="顯示記憶庫統計")
    args = parser.parse_args()

    try:
        agent = MemoryAgent()
    except RuntimeError as e:
        logger.warning("記憶管家不可用：%s", e)
        print(f"Memory agent unavailable: {e}")
        return

    if args.query:
        hits = agent.retrieve(args.query, n_results=5)
        for h in hits:
            print(f"[{h['score']:.3f}] {h['metadata'].get('source','?')} / {h['metadata'].get('date','?')} / {h['metadata'].get('heading','')}")
            print(f"  {h['content'][:200]}...")
            print()
        return

    if args.stats:
        stats = agent.get_memory_stats()
        print("\n📊 記憶庫統計:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        return

    if args.archive:
        result = agent.archive_old_memories()
        print(f"\n📦 封存完成: {result}")
        return

    if args.cleanup:
        result = agent.cleanup_orphaned_chunks()
        print(f"\n🗑️ 清理完成: {result}")
        return

    if args.range:
        try:
            start, end = args.range.split(":")
        except ValueError:
            print("--range 格式錯誤，應為 YYYY-MM-DD:YYYY-MM-DD")
            sys.exit(1)
        agent.ingest_date_range(start.strip(), end.strip())
    else:
        agent.ingest_daily_files(args.date)

    print("Memory ingestion complete.")


if __name__ == "__main__":
    main()
