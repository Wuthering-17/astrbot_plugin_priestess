"""Shared switches for proactive notifications; explicit command replies are unaffected."""
from __future__ import annotations


def notification_enabled(plugin, kind: str) -> bool:
    config = getattr(plugin, "plugin_config", {})
    return config.get("notify_all", True) is not False and config.get(f"notify_{kind}", True) is not False
