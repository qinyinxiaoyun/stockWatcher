"""Offline, deterministic management commands.

Only the grammar in :data:`HELP_TEXT` can modify configuration. Unknown text
returns ``None`` from :func:`parse_command` so the callback layer may enqueue a
constrained natural-language task without holding a database transaction open.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Mapping

from .config import ConfigError, ConfigSnapshot, ConfigStore, Confirmation


HELP_TEXT = """可用指令：
帮助
查看监控
添加股票 <带市场代码> <名称> [别名1,别名2]
删除股票 <代码或别名>
设置关注 <代码或别名> <关注点>
设置别名 <代码或别名> <别名1,别名2>
添加方向 <名称> [搜索词1,搜索词2]
删除方向 <名称>
暂停监控 <代码或别名>
恢复监控 <代码或别名>
设置推送等级 <重大 重要 普通中的一个或多个>
清空监控
确认 <确认码>

名称或关注点含空格时可使用引号。单项删除直接执行；清空和批量删除必须二次确认。"""


class CommandError(ConfigError):
    """A known command has invalid arguments."""


class UnknownCommand(CommandError):
    """Raised by ``execute`` when text should be routed to NL parsing."""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    action: str
    operation: Mapping[str, Any] | None = None
    token: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    status: str
    text: str
    snapshot: ConfigSnapshot | None = None
    confirmation: Confirmation | None = None

    @property
    def message(self) -> str:
        return self.text

    @property
    def confirmation_token(self) -> str | None:
        return self.confirmation.token if self.confirmation else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "confirmation": self.confirmation.to_dict() if self.confirmation else None,
        }


def _tokenize(text: str) -> list[str]:
    try:
        return shlex.split(text.strip(), posix=True)
    except ValueError as exc:
        raise CommandError(f"指令中的引号不完整：{exc}") from exc


def _one_target(args: list[str], usage: str) -> str:
    if len(args) != 1:
        raise CommandError(f"用法：{usage}")
    return args[0]


def _split_list(parts: list[str]) -> list[str]:
    values: list[str] = []
    for part in parts:
        for item in re.split(r"[,，、]", part):
            item = item.strip()
            if item and item not in values:
                values.append(item)
    return values


def parse_command(text: str) -> ParsedCommand | None:
    """Return a parsed explicit command, or ``None`` for natural language.

    Once a known verb is recognized, malformed arguments raise
    :class:`CommandError`; they are never sent to a model as a fallback.
    """

    if not isinstance(text, str) or not text.strip():
        raise CommandError("指令不能为空；发送“帮助”查看用法。")
    parts = _tokenize(text)
    if not parts:
        raise CommandError("指令不能为空；发送“帮助”查看用法。")
    verb, args = parts[0].casefold(), parts[1:]

    if verb in {"帮助", "help", "?", "？"}:
        if args:
            raise CommandError("“帮助”后不需要参数。")
        return ParsedCommand("help")
    if verb in {"查看监控", "查看", "列表", "view", "list"}:
        if args not in ([], ["监控"]):
            raise CommandError("用法：查看监控")
        return ParsedCommand("view")
    if verb in {"添加股票", "add_stock"}:
        if len(args) < 2:
            raise CommandError("用法：添加股票 <带市场代码> <名称> [别名1,别名2]")
        operation: dict[str, Any] = {
            "op": "add_stock",
            "symbol": args[0],
            "name": args[1],
        }
        if len(args) > 2:
            operation["aliases"] = _split_list(args[2:])
        return ParsedCommand("mutate", operation)
    if verb in {"删除股票", "remove_stock", "delete_stock"}:
        target = _one_target(args, "删除股票 <代码或别名>")
        return ParsedCommand("mutate", {"op": "remove_stock", "target": target})
    if verb in {"设置关注", "set_focus"}:
        if not args:
            raise CommandError("用法：设置关注 <代码或别名> <关注点>")
        return ParsedCommand(
            "mutate",
            {"op": "set_focus", "target": args[0], "focus": " ".join(args[1:])},
        )
    if verb in {"设置别名", "set_aliases"}:
        if not args:
            raise CommandError("用法：设置别名 <代码或别名> [别名1,别名2]")
        return ParsedCommand(
            "mutate",
            {"op": "set_aliases", "target": args[0], "aliases": _split_list(args[1:])},
        )
    if verb in {"添加方向", "add_theme"}:
        if not args:
            raise CommandError("用法：添加方向 <名称> [搜索词1,搜索词2]")
        name = args[0]
        keywords = _split_list(args[1:]) or [name]
        return ParsedCommand(
            "mutate", {"op": "add_theme", "name": name, "keywords": keywords}
        )
    if verb in {"删除方向", "remove_theme", "delete_theme"}:
        target = _one_target(args, "删除方向 <名称>")
        return ParsedCommand("mutate", {"op": "remove_theme", "target": target})
    if verb in {"暂停监控", "pause", "pause_stock"}:
        target = _one_target(args, "暂停监控 <代码或别名>")
        return ParsedCommand("mutate", {"op": "pause_stock", "target": target})
    if verb in {"恢复监控", "resume", "resume_stock"}:
        target = _one_target(args, "恢复监控 <代码或别名>")
        return ParsedCommand("mutate", {"op": "resume_stock", "target": target})
    if verb in {"设置推送等级", "set_push_levels"}:
        levels = _split_list(args)
        if not levels:
            raise CommandError("用法：设置推送等级 <重大 重要 普通中的一个或多个>")
        return ParsedCommand("mutate", {"op": "set_push_levels", "levels": levels})
    if verb in {"清空", "清空监控", "clear"}:
        if args not in ([], ["监控"]):
            raise CommandError("用法：清空监控")
        return ParsedCommand("request_clear")
    if verb in {"确认", "确认清空", "confirm"}:
        if len(args) != 1:
            raise CommandError("用法：确认 <确认码>")
        return ParsedCommand("confirm", token=args[0])

    # Split two-word English commands, while keeping arbitrary prose out.
    if verb in {"add", "remove", "delete", "set"}:
        raise CommandError("该指令不完整；发送“帮助”查看明确指令格式。")
    return None


def _render_snapshot(snapshot: ConfigSnapshot) -> str:
    lines = [f"当前监控（配置 v{snapshot.config_version}）："]
    for stock in snapshot.stocks:
        state = "启用" if stock.enabled else "暂停"
        lines.append(
            f"{stock.symbol} {stock.name} {state}；关注：{stock.focus or '无'}"
        )
    for theme in snapshot.themes:
        state = "启用" if theme.enabled else "暂停"
        lines.append(f"方向：{theme.name} {state}；搜索词：{'、'.join(theme.keywords)}")
    if not snapshot.stocks and not snapshot.themes:
        lines.append("空名单，全部目标暂停。")
    lines.append("推送等级：" + "、".join(snapshot.push_levels))
    return "\n".join(lines)


def _operation_summary(operation: Mapping[str, Any]) -> str:
    action = str(operation.get("op", ""))
    if action == "add_stock":
        return f"新增股票 {operation.get('symbol')} {operation.get('name')}"
    if action == "remove_stock":
        return f"删除股票 {operation.get('target')}"
    if action == "pause_stock":
        return f"暂停股票 {operation.get('target')}"
    if action == "resume_stock":
        return f"恢复股票 {operation.get('target')}"
    if action == "set_focus":
        return f"更新 {operation.get('target')} 的关注点"
    if action == "set_aliases":
        return f"更新 {operation.get('target')} 的搜索别名"
    if action == "add_theme":
        return f"新增方向 {operation.get('name')}"
    if action == "remove_theme":
        return f"删除方向 {operation.get('target')}"
    if action == "set_push_levels":
        return "设置推送等级为" + "、".join(operation.get("levels", ()))
    return "更新监控配置"


class CommandService:
    """Execute explicit commands with no network or model calls."""

    def __init__(self, store: ConfigStore, *, confirmation_ttl_seconds: int = 300) -> None:
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds must be positive")
        self.store = store
        self.confirmation_ttl_seconds = confirmation_ttl_seconds

    def execute(
        self,
        text: str,
        *,
        user_id: str,
        expected_version: int | None = None,
        source_message_id: str | None = None,
    ) -> CommandResult:
        command = parse_command(text)
        if command is None:
            raise UnknownCommand("不是已知明确指令，应转入受约束的自然语言处理。")
        if command.action == "help":
            return CommandResult("help", HELP_TEXT, self.store.snapshot())
        if command.action == "view":
            snapshot = self.store.snapshot()
            return CommandResult("view", _render_snapshot(snapshot), snapshot)
        if command.action == "mutate":
            assert command.operation is not None
            snapshot = self.store.apply_operation(
                command.operation,
                actor=user_id,
                expected_version=expected_version,
                source="wecom-command" if source_message_id else "command",
                source_message_id=source_message_id,
            )
            return CommandResult(
                "updated",
                f"已保存：{_operation_summary(command.operation)}；配置 v{snapshot.config_version}；"
                f"生效时间 {snapshot.updated_at}。在下一次定时任务开始时读取。\n"
                + _render_snapshot(snapshot),
                snapshot,
            )
        if command.action == "request_clear":
            snapshot = self.store.snapshot()
            scope = [f"股票 {item.symbol} {item.name}" for item in snapshot.stocks]
            scope.extend(f"方向 {item.name}" for item in snapshot.themes)
            confirmation = self.store.request_confirmation(
                user_id=user_id,
                action="clear_stocks",
                ttl_seconds=self.confirmation_ttl_seconds,
                expected_version=expected_version,
            )
            scope_text = "、".join(scope) if scope else "空名单（仍允许确认）"
            return CommandResult(
                "confirmation_required",
                f"将清空：{scope_text}。配置尚未修改。请在5分钟内发送“确认 {confirmation.token}”；"
                "配置版本改变后需重新确认。",
                snapshot,
                confirmation,
            )
        if command.action == "confirm":
            assert command.token is not None
            snapshot = self.store.apply_confirmation(
                command.token,
                user_id=user_id,
                actor=user_id,
                source="wecom-command" if source_message_id else "command",
                source_message_id=source_message_id,
            )
            return CommandResult(
                "updated",
                f"已确认并保存，配置 v{snapshot.config_version}；生效时间 {snapshot.updated_at}。"
                "在下一次定时任务开始时读取。",
                snapshot,
            )
        raise AssertionError(f"unhandled command action: {command.action}")


__all__ = [
    "CommandError",
    "CommandResult",
    "CommandService",
    "HELP_TEXT",
    "ParsedCommand",
    "UnknownCommand",
    "parse_command",
]
