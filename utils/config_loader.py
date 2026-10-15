#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加載工具
"""

from pathlib import Path
import yaml


class ConfigLoader:
    """配置加載器"""

    @staticmethod
    def load_config(config_path: str) -> dict:
        """加載配置文件

        Args:
            config_path: 配置文件路徑

        Returns:
            dict: 配置內容
        """
        if not config_path:
            raise ValueError("config_path cannot be empty")

        path = Path(config_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            with path.open('r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {path}: {e}") from e

        return config or {}


# 為腳本提供簡單的工裝函數
def load_config(config_path: str) -> dict:
    """加載配置的工裝函數

    Args:
        config_path: 配置文件路徑

    Returns:
        dict: 配置內容
    """
    return ConfigLoader.load_config(config_path)
