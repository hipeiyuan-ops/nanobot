"""
定时任务工具模块，提供提醒和定时任务调度能力。

该模块实现了 cron 工具，允许 agent 创建和管理定时任务：
- 一次性提醒：在指定时间执行
- 周期性任务：按固定间隔重复执行
- Cron 表达式：使用标准 cron 语法调度

任务触发时会向 agent 发送消息，agent 可以执行相应的操作。
"""

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJobState, CronSchedule


class CronTool(Tool):
    """
    定时任务工具，用于调度提醒和周期性任务。

    该工具提供三种调度方式：
    1. every_seconds: 按固定秒数间隔重复执行
    2. cron_expr: 使用 cron 表达式调度（如 "0 9 * * *" 表示每天 9 点）
    3. at: 在指定的 ISO 时间执行一次

    任务触发时会向 agent 发送消息，agent 根据消息内容执行相应操作。

    Attributes:
        _cron: Cron 服务实例
        _default_timezone: 默认时区
        _channel: 当前频道
        _chat_id: 当前聊天 ID
        _in_cron_context: 标记是否在 cron 回调中执行
    """

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        """
        初始化定时任务工具。

        Args:
            cron_service: Cron 服务实例
            default_timezone: 默认时区（IANA 格式，如 'Asia/Shanghai'）
        """
        self._cron = cron_service
        self._default_timezone = default_timezone
        self._channel = ""
        self._chat_id = ""
        # 使用 ContextVar 跟踪是否在 cron 回调中执行
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        设置当前会话上下文，用于任务触发时的消息投递。

        Args:
            channel: 频道名称
            chat_id: 聊天 ID
        """
        self._channel = channel
        self._chat_id = chat_id

    def set_cron_context(self, active: bool):
        """
        标记是否在 cron 任务回调中执行。

        用于防止在 cron 回调中创建新的 cron 任务。

        Args:
            active: 是否在 cron 上下文中

        Returns:
            ContextVar 的 token，用于恢复
        """
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """
        恢复之前的 cron 上下文状态。

        Args:
            token: set_cron_context 返回的 token
        """
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        """
        验证时区是否有效。

        Args:
            tz: 时区字符串（IANA 格式）

        Returns:
            错误消息，如果时区有效则返回 None
        """
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """
        选择最适合显示的时区。

        优先使用任务指定的时区，否则使用默认时区。

        Args:
            schedule: 调度配置

        Returns:
            时区字符串
        """
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        """
        将毫秒时间戳格式化为带时区的 ISO 格式字符串。

        Args:
            ms: 毫秒时间戳
            tz_name: 时区名称

        Returns:
            格式化的时间字符串
        """
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        """工具名称。"""
        return "cron"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Instruction for the agent to execute when the job triggers (e.g., 'Send a reminder to WeChat: xxx' or 'Check system status and report')"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone for cron expressions "
                        f"(e.g. 'America/Vancouver'). Defaults to {self._default_timezone}."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ISO datetime for one-time execution "
                        f"(e.g. '2026-02-12T10:30:00'). Naive values default to {self._default_timezone}."
                    ),
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """
        执行定时任务操作。

        Args:
            action: 操作类型（add, list, remove）
            message: 任务消息（add 时必需）
            every_seconds: 重复间隔（秒）
            cron_expr: Cron 表达式
            tz: 时区
            at: 一次性执行时间（ISO 格式）
            job_id: 任务 ID（remove 时必需）

        Returns:
            操作结果消息
        """
        if action == "add":
            # 防止在 cron 回调中创建新任务
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(message, every_seconds, cron_expr, tz, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        """
        添加定时任务。

        Args:
            message: 任务消息
            every_seconds: 重复间隔
            cron_expr: Cron 表达式
            tz: 时区
            at: 一次性执行时间

        Returns:
            操作结果消息
        """
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # 构建调度配置
        delete_after = False
        if every_seconds:
            # 固定间隔
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            # Cron 表达式
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            # 一次性执行
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            # 如果没有时区，使用默认时区
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        # 添加任务
        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """
        将调度配置格式化为人类可读的字符串。

        Args:
            schedule: 调度配置

        Returns:
            格式化的调度描述
        """
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            # 转换为更友好的单位
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """
        格式化任务运行状态。

        Args:
            state: 任务状态
            schedule: 调度配置

        Returns:
            状态描述行列表
        """
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    def _list_jobs(self) -> str:
        """
        列出所有定时任务。

        Returns:
            任务列表字符串
        """
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        """
        移除定时任务。

        Args:
            job_id: 任务 ID

        Returns:
            操作结果消息
        """
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
