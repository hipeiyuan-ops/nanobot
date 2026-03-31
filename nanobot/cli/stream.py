"""
CLI 流式渲染器模块。

使用 Rich Live 组件配合 auto_refresh=False 实现稳定、无闪烁的
Markdown 流式渲染。使用省略号模式处理内容溢出。

主要组件：
    - ThinkingSpinner: 思考中状态指示器
    - StreamRenderer: 流式消息渲染器

渲染流程：
    1. 显示思考中动画（spinner）
    2. 收到第一个可见增量时启动 Live 渲染
    3. 流式更新 Markdown 内容
    4. 流结束时停止 Live（内容保留在屏幕上）
"""

from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from nanobot import __logo__


def _make_console() -> Console:
    """创建标准输出控制台实例。"""
    return Console(file=sys.stdout)


class ThinkingSpinner:
    """
    思考中状态指示器，显示 "nanobot is thinking..." 动画。

    支持暂停功能，用于在思考过程中临时输出其他内容。

    属性：
        _spinner: Rich 状态指示器
        _active: 是否处于活动状态
    """

    def __init__(self, console: Console | None = None):
        """
        初始化思考指示器。

        参数：
            console: 可选的控制台实例，默认使用标准输出
        """
        c = console or _make_console()
        self._spinner = c.status("[dim]nanobot is thinking...[/dim]", spinner="dots")
        self._active = False

    def __enter__(self):
        """启动思考动画。"""
        self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        """停止思考动画。"""
        self._active = False
        self._spinner.stop()
        return False

    def pause(self):
        """
        上下文管理器：临时暂停动画以便输出其他内容。

        使用示例：
            with spinner.pause():
                console.print("中间输出")
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if self._spinner and self._active:
                self._spinner.stop()
            try:
                yield
            finally:
                if self._spinner and self._active:
                    self._spinner.start()

        return _ctx()


class StreamRenderer:
    """
    Rich Live 流式渲染器，支持 Markdown 格式。

    使用 auto_refresh=False 避免渲染竞争条件。

    增量内容已从 Agent 循环中预过滤（无 <think/> 标签）。

    每轮流程：
        spinner -> 第一个可见增量 -> 标题 + Live 渲染 ->
        on_end -> Live 停止（内容保留在屏幕上）

    属性：
        streamed: 是否已流式输出内容
    """

    def __init__(self, render_markdown: bool = True, show_spinner: bool = True):
        """
        初始化流式渲染器。

        参数：
            render_markdown: 是否渲染 Markdown 格式
            show_spinner: 是否显示思考中动画
        """
        self._md = render_markdown
        self._show_spinner = show_spinner
        self._buf = ""
        self._live: Live | None = None
        self._t = 0.0
        self.streamed = False
        self._spinner: ThinkingSpinner | None = None
        self._start_spinner()

    def _render(self):
        """渲染当前缓冲区内容。"""
        return Markdown(self._buf) if self._md and self._buf else Text(self._buf or "")

    def _start_spinner(self) -> None:
        """启动思考动画。"""
        if self._show_spinner:
            self._spinner = ThinkingSpinner()
            self._spinner.__enter__()

    def _stop_spinner(self) -> None:
        """停止思考动画。"""
        if self._spinner:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    async def on_delta(self, delta: str) -> None:
        """
        处理增量文本块。

        参数：
            delta: 增量文本内容

        处理逻辑：
            1. 标记已流式输出
            2. 累积到缓冲区
            3. 首次有内容时启动 Live 渲染
            4. 按节流策略更新显示
        """
        self.streamed = True
        self._buf += delta
        if self._live is None:
            if not self._buf.strip():
                return
            self._stop_spinner()
            c = _make_console()
            c.print()
            c.print(f"[cyan]{__logo__} nanobot[/cyan]")
            self._live = Live(self._render(), console=c, auto_refresh=False)
            self._live.start()
        now = time.monotonic()
        if "\n" in delta or (now - self._t) > 0.05:
            self._live.update(self._render())
            self._live.refresh()
            self._t = now

    async def on_end(self, *, resuming: bool = False) -> None:
        """
        流式输出结束回调。

        参数：
            resuming: 是否将继续下一轮对话

        处理逻辑：
            1. 最终更新并停止 Live
            2. 停止思考动画
            3. 如果继续，重置状态并重新启动动画
        """
        if self._live:
            self._live.update(self._render())
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._stop_spinner()
        if resuming:
            self._buf = ""
            self._start_spinner()
        else:
            _make_console().print()

    async def close(self) -> None:
        """停止动画/Live，不渲染最终的流式轮次。"""
        if self._live:
            self._live.stop()
            self._live = None
        self._stop_spinner()
