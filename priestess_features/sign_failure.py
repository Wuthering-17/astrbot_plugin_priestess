"""Private automatic sign-in failure summaries, with persistent recipient-level deduplication."""
from __future__ import annotations

import re

from astrbot.api import logger
from ..notifications import notification_enabled


class SignFailureNotifier:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def recipients(self) -> list[str]:
        configured = self.plugin.plugin_config.get("sign_failure_notify_qq", "")
        if not isinstance(configured, str):
            return []
        return sorted({value for value in re.split(r"[\s,，;；]+", configured.strip()) if re.fullmatch(r"[1-9]\d{4,11}", value)})

    async def notify(self, state: dict, day: str, issues: dict[str, str], *, category: str = "sign_failure_private") -> bool:
        if not notification_enabled(self.plugin, category):
            return True
        recipients = self.recipients()
        if not recipients or not issues:
            return True
        alerts = state.setdefault("sign_failure_alerts", {})
        if alerts.get("date") != day:
            alerts.clear()
            alerts.update(date=day, sent={})
        try:
            clients = [p.get_client() for p in self.plugin.context.platform_manager.get_insts() if p.meta().name == "aiocqhttp" and hasattr(p, "get_client")]
        except Exception:
            logger.warning("[Priestess] 签到失败提醒暂时无法获取 QQ 连接，稍后重试")
            return False
        complete = True
        for qq in recipients:
            sent = alerts["sent"].setdefault(qq, [])
            pending = {key: value for key, value in issues.items() if key not in sent}
            if not pending:
                continue
            message = "\n".join(["Priestess 自动签到失败提醒", f"日期：{day}", *pending.values(), "后台会继续重试；同一账号同一游戏当天不重复提醒。", "请检查账号绑定、网络与机器人日志。"])
            for client in clients:
                try:
                    response = await client.call_action("send_private_msg", user_id=int(qq), message=message)
                    if isinstance(response, dict) and (response.get("status") == "failed" or response.get("retcode", 0) != 0):
                        raise RuntimeError("OneBot rejected the private notification")
                except Exception:
                    continue
                sent.extend(pending)
                break
            else:
                complete = False
                logger.warning("[Priestess] 签到失败私聊提醒未能发送，稍后重试；请确认接收 QQ 可接收机器人私聊")
        return complete
