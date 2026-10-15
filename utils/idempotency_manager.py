#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任務冪等性管理器 — 防止同一日同一 Agent 重複執行

核心功能：
1. 記錄每個 Agent 的執行狀態、輸出 checksum、時間戳
2. 執行前檢查是否已執行成功
3. 支援強制重跑模式
4. 自動清理過期記錄（保留 30 天）
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class AgentExecutionRecord:
    """單一 Agent 執行記錄"""

    def __init__(
        self,
        agent_name: str,
        success: bool,
        output_path: Optional[str] = None,
        output_checksum: Optional[str] = None,
        executed_at: Optional[str] = None,
        duration_sec: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        delivery_status: Optional[Dict[str, str]] = None,
    ):
        self.agent_name = agent_name
        self.success = success
        self.output_path = output_path
        self.output_checksum = output_checksum
        self.executed_at = executed_at or datetime.now().isoformat()
        self.duration_sec = duration_sec
        self.metadata = metadata or {}
        # delivery_status: {"feishu": "sent|failed|skipped", "wechat": "sent|failed|queued|gated|skipped"}
        # 用於 should_execute 判斷是否需要補發；只要任一 channel 非 sent/skipped，視為未完成。
        self.delivery_status = delivery_status or {}

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "success": self.success,
            "output_path": self.output_path,
            "output_checksum": self.output_checksum,
            "executed_at": self.executed_at,
            "duration_sec": self.duration_sec,
            "metadata": self.metadata,
            "delivery_status": self.delivery_status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentExecutionRecord":
        return cls(
            agent_name=data.get("agent_name", ""),
            success=data.get("success", False),
            output_path=data.get("output_path"),
            output_checksum=data.get("output_checksum"),
            executed_at=data.get("executed_at"),
            duration_sec=data.get("duration_sec", 0.0),
            metadata=data.get("metadata", {}),
            delivery_status=data.get("delivery_status", {}),
        )

    def has_undelivered_channel(self) -> bool:
        """是否存在未送達的通道（用於判斷是否需要補發）"""
        # "sent" 與 "skipped" 視為已完成；其他狀態（failed/queued/gated/error/partial_suspected）都算未完成
        completed_states = {"sent", "skipped"}
        for status in self.delivery_status.values():
            if status not in completed_states:
                return True
        return False


class IdempotencyManager:
    """
    任務冪等性管理器

    檔案結構：
    logs/idempotency/
      YYYY-MM-DD.json       — 每日執行記錄
      archive/              — 封存舊記錄
    """

    # 預設保留天數
    DEFAULT_RETENTION_DAYS = 30

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent / "logs" / "idempotency"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # 建立封存目錄
        self.archive_dir = self.base_dir / "archive"
        self.archive_dir.mkdir(exist_ok=True)

        self._lock = threading.RLock()
        self._today_str = datetime.now().strftime("%Y-%m-%d")
        self._state_file = self.base_dir / f"{self._today_str}.json"

        # 初始化今日記錄檔
        self._init_today_file()

    def _init_today_file(self):
        """初始化今日的記錄檔"""
        if not self._state_file.exists():
            self._write_state({
                "date": self._today_str,
                "created_at": datetime.now().isoformat(),
                "executions": {},
            })

    def _read_state(self) -> dict:
        """讀取今日狀態"""
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"讀取冪等性狀態失敗: {e}")
            return {"date": self._today_str, "executions": {}}

    def _write_state(self, state: dict):
        """寫入今日狀態（線程安全）"""
        with self._lock:
            self._state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    def _compute_checksum(self, file_path: Optional[str]) -> Optional[str]:
        """計算檔案 checksum"""
        if not file_path:
            return None
        path = Path(file_path)
        if not path.exists():
            return None
        try:
            content = path.read_bytes()
            return hashlib.md5(content).hexdigest()[:16]
        except Exception as e:
            logger.warning(f"計算 checksum 失敗: {e}")
            return None

    def should_execute(
        self,
        agent_name: str,
        force: bool = False,
        max_age_minutes: Optional[int] = None,
    ) -> tuple[bool, Optional[AgentExecutionRecord]]:
        """
        檢查是否應該執行此 Agent

        Args:
            agent_name: Agent 名稱
            force: 是否強制重跑
            max_age_minutes: 最大允許年齡（分鐘），超過則視為需要重跑

        Returns:
            (should_execute, existing_record)
            - should_execute: True 表示應該執行
            - existing_record: 既有執行記錄（如果有的話）
        """
        # 強制重跑模式
        if force:
            logger.info(f"🔄 [{agent_name}] 強制重跑模式，跳過冪等性檢查")
            return True, None

        state = self._read_state()
        executions = state.get("executions", {})

        # 沒有記錄，應該執行
        if agent_name not in executions:
            return True, None

        record = AgentExecutionRecord.from_dict(executions[agent_name])

        # 之前執行失敗，應該重試
        if not record.success:
            logger.info(f"🔄 [{agent_name}] 上次執行失敗，準備重試")
            return True, record

        # 上次執行成功，但有 IM 通道未送達，應該補發
        if record.has_undelivered_channel():
            undelivered = [
                f"{ch}={st}" for ch, st in record.delivery_status.items()
                if st not in {"sent", "skipped"}
            ]
            logger.info(f"🔄 [{agent_name}] 上次推送未完成（{', '.join(undelivered)}），準備補發")
            return True, record

        # 檢查是否超過最大年齡
        if max_age_minutes:
            try:
                executed_time = datetime.fromisoformat(record.executed_at)
                age = datetime.now() - executed_time
                if age > timedelta(minutes=max_age_minutes):
                    logger.info(f"🔄 [{agent_name}] 記錄已過期（{age.total_seconds()/60:.0f} 分鐘），準備重跑")
                    return True, record
            except Exception as e:
                logger.warning(f"解析執行時間失敗: {e}")

        # 已經成功執行且未過期，不需要重跑
        logger.info(f"⏭️ [{agent_name}] 今日已於 {record.executed_at[:19]} 成功執行，跳過")
        return False, record

    def record_execution(
        self,
        agent_name: str,
        success: bool,
        output_path: Optional[str] = None,
        duration_sec: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentExecutionRecord:
        """
        記錄 Agent 執行結果

        Args:
            agent_name: Agent 名稱
            success: 是否成功
            output_path: 輸出檔案路徑
            duration_sec: 執行耗時（秒）
            metadata: 額外元資料

        Returns:
            建立的執行記錄
        """
        checksum = self._compute_checksum(output_path)

        record = AgentExecutionRecord(
            agent_name=agent_name,
            success=success,
            output_path=output_path,
            output_checksum=checksum,
            duration_sec=duration_sec,
            metadata=metadata or {},
        )

        with self._lock:
            state = self._read_state()
            if "executions" not in state:
                state["executions"] = {}

            state["executions"][agent_name] = record.to_dict()
            state["last_updated"] = datetime.now().isoformat()
            self._write_state(state)

        logger.info(f"📝 [{agent_name}] 執行記錄已保存（success={success}）")
        return record

    def mark_delivery_status(
        self,
        agent_name: str,
        channel_status: Dict[str, str],
    ) -> Optional[AgentExecutionRecord]:
        """
        回寫 IM 推送送達狀態。

        Args:
            agent_name: Agent 名稱（須先有 record_execution 紀錄）
            channel_status: {"feishu": "sent|failed", "wechat": "sent|failed|queued|gated"}

        - 合併進現有 delivery_status（同 channel 以新狀態覆寫）
        - 若任一 channel 非 sent/skipped，視為「未完成」，將 record.success 設為 False
          這樣下一輪 should_execute 會回 True，自動觸發補發
        """
        with self._lock:
            state = self._read_state()
            executions = state.get("executions", {})
            if agent_name not in executions:
                logger.warning(f"[{agent_name}] 尚未有執行記錄，跳過 mark_delivery_status")
                return None

            record = AgentExecutionRecord.from_dict(executions[agent_name])
            merged = dict(record.delivery_status)
            merged.update({k: str(v) for k, v in channel_status.items() if v})
            record.delivery_status = merged

            # 任一通道未完成 → 整體標記為未成功，下輪允許重試
            if record.has_undelivered_channel():
                record.success = False

            executions[agent_name] = record.to_dict()
            state["executions"] = executions
            state["last_updated"] = datetime.now().isoformat()
            self._write_state(state)

        logger.info(
            f"📨 [{agent_name}] 送達狀態已更新：{merged}（success={record.success}）"
        )
        return record

    def get_execution_history(
        self,
        agent_name: Optional[str] = None,
        days: int = 7,
    ) -> Dict[str, list]:
        """
        取得執行歷史

        Args:
            agent_name: 指定 Agent（None 表示全部）
            days: 查詢最近幾天

        Returns:
            {date: [AgentExecutionRecord, ...]}
        """
        result = {}
        cutoff = datetime.now() - timedelta(days=days)

        for file_path in self.base_dir.glob("*.json"):
            if file_path.name == "archive":
                continue

            try:
                date_str = file_path.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    continue

                state = json.loads(file_path.read_text(encoding="utf-8"))
                executions = state.get("executions", {})

                records = []
                for name, data in executions.items():
                    if agent_name and name != agent_name:
                        continue
                    records.append(AgentExecutionRecord.from_dict(data))

                if records:
                    result[date_str] = records

            except Exception as e:
                logger.warning(f"讀取歷史記錄失敗 {file_path}: {e}")

        return result

    def cleanup_old_records(self, retention_days: Optional[int] = None):
        """
        清理過期記錄

        Args:
            retention_days: 保留天數（預設 30 天）
        """
        retention = retention_days or self.DEFAULT_RETENTION_DAYS
        cutoff = datetime.now() - timedelta(days=retention)

        moved_count = 0
        for file_path in list(self.base_dir.glob("*.json")):
            if file_path.name == "archive":
                continue

            try:
                date_str = file_path.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d")

                if file_date < cutoff:
                    # 移動到封存目錄
                    archive_path = self.archive_dir / file_path.name
                    file_path.rename(archive_path)
                    moved_count += 1
                    logger.debug(f"封存舊記錄: {file_path.name}")

            except Exception as e:
                logger.warning(f"清理記錄失敗 {file_path}: {e}")

        if moved_count > 0:
            logger.info(f"🗄️ 已封存 {moved_count} 份過期記錄到 {self.archive_dir}")

    def get_skipped_agents_today(self) -> Set[str]:
        """取得今日已被跳過的 Agent 集合"""
        state = self._read_state()
        executions = state.get("executions", {})

        skipped = set()
        for name, data in executions.items():
            if data.get("success", False):
                skipped.add(name)

        return skipped

    def reset_today(self, agent_name: Optional[str] = None):
        """
        重置今日記錄

        Args:
            agent_name: 指定 Agent（None 表示全部）
        """
        with self._lock:
            state = self._read_state()

            if agent_name:
                if agent_name in state.get("executions", {}):
                    del state["executions"][agent_name]
                    logger.info(f"🗑️ 已重置 [{agent_name}] 的今日記錄")
            else:
                state["executions"] = {}
                logger.info("🗑️ 已重置今日所有記錄")

            state["last_updated"] = datetime.now().isoformat()
            self._write_state(state)


# 全局實例
_idempotency_manager: Optional[IdempotencyManager] = None


def get_idempotency_manager(base_dir: Optional[Path] = None) -> IdempotencyManager:
    """取得 IdempotencyManager 單例"""
    global _idempotency_manager
    if _idempotency_manager is None:
        _idempotency_manager = IdempotencyManager(base_dir)
    return _idempotency_manager
