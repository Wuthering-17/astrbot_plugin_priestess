"""Canonical AstrBot data directory and lossless migration of the old port path."""

from pathlib import Path

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_priestess"
LEGACY_NAME = "astrbot-plugin-priestess"


def get_data_dir(parent: Path | None = None) -> Path:
    parent = (parent if parent is not None else Path("data/plugin_data")).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / PLUGIN_NAME
    legacy = parent / LEGACY_NAME
    for folder in (target, legacy):
        if folder.is_symlink() or folder.resolve().parent != parent:
            raise RuntimeError("Priestess 数据目录不可指向其他路径，请检查目录链接。")
    if legacy.is_dir() and any(legacy.iterdir()):
        if target.exists() and any(target.iterdir()):
            raise RuntimeError("Priestess 新旧数据目录同时包含文件，已停止迁移以避免覆盖；请停止机器人并备份、核对两个目录后重试。")
        if target.exists():
            target.rmdir()  # Only an empty, validated directory may be removed.
        try:
            legacy.rename(target)
        except OSError as error:
            raise RuntimeError("Priestess 旧数据目录迁移失败，请停止机器人后重启再试；原数据未被覆盖。") from error
        logger.info("[Priestess] 已将旧版数据目录迁移至 data/plugin_data/astrbot_plugin_priestess")
    target.mkdir(parents=True, exist_ok=True)
    return target
