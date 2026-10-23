"""All operational commands share one explicit, testable entry point."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from wsgiref.simple_server import make_server

from .db import SCHEMA_VERSION, connect, init_db
from .config import ConfigError
from .settings import Settings


def output(value):
    print(json.dumps(asdict(value) if is_dataclass(value) else value, ensure_ascii=False,
                     default=str, indent=2))


def parser():
    result = argparse.ArgumentParser(prog="stock-watch", description="自建股票新闻监控运维；默认不调用真实外部接口。")
    result.add_argument("--env-file", help="显式读取本地环境文件，已有环境变量优先")
    result.add_argument("--database", help="覆盖数据库路径")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="幂等初始化数据库与原始三家公司")
    config = sub.add_parser("config", help="查看监控快照；独立订阅模式需指定成员")
    config.add_argument("--member", help="指定已准入成员UserID")
    command = sub.add_parser("command", help="本地执行明确管理指令")
    command.add_argument("text")
    command.add_argument("--user", default="local-operator")
    command.add_argument("--member", help="本地可信操作指定成员自己的订阅")
    members = sub.add_parser("members", help="本地可信管理：成员准入、撤权及独立订阅迁移")
    member_actions = members.add_subparsers(dest="member_action", required=True)
    member_actions.add_parser("list", help="查看已准入成员")
    member_add = member_actions.add_parser("add", help="准入成员；默认空订阅")
    member_add.add_argument("user")
    member_add.add_argument("--role", choices=("subscriber", "admin"), default="subscriber")
    member_revoke = member_actions.add_parser("revoke", help="撤销成员资格及待投递任务")
    member_revoke.add_argument("user")
    member_owner = member_actions.add_parser("assign-legacy-owner", help="显式指定唯一原拥有者并切换独立订阅")
    member_owner.add_argument("user")
    member_analysis = member_actions.add_parser("analysis", help="选择该成员分析结果的隔离范围")
    member_analysis.add_argument("user")
    privacy = member_analysis.add_mutually_exclusive_group(required=True)
    privacy.add_argument("--private", action="store_true", help="分析结果按成员隔离")
    privacy.add_argument("--shared", action="store_true", help="相同公开关注需求在本组织内复用")
    monitor = sub.add_parser("monitor", help="手工执行一轮；实际远程访问需显式 --real")
    monitor.add_argument("--dry-run", action="store_true", help="复制数据库到临时库，不改正式seen/任务，不投递")
    source = monitor.add_mutually_exclusive_group()
    source.add_argument("--fake", action="store_true", help="离线模拟新闻与模型（默认）")
    source.add_argument("--real", action="store_true", help="真实新闻+模型，消耗额度；不直接投递，写入outbox")
    backfill = sub.add_parser("backfill", help="单目标历史补查，最多10条；默认隔离模拟预览")
    backfill.add_argument("--target", required=True, help="唯一的启用目标代码/方向名")
    backfill.add_argument("--member", help="独立订阅补查必须指定的成员UserID")
    backfill.add_argument("--hours", type=int, choices=range(1, 25), default=24)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--real", action="store_true", help="真实抓取分析；不带dry-run时写正式outbox")
    backfill.add_argument("--ack-max-notifications", type=int, choices=[10], help="真实补查写正式库前明确确认最多10条通知")
    worker = sub.add_parser("worker", help="处理持久任务和回执；自动新闻调度由MONITOR_ENABLED控制")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--fake", action="store_true", help="离线运行且模拟投递")
    server = sub.add_parser("serve", help="启动仅监听127.0.0.1的本地WSGI开发服务")
    server.add_argument("--port", type=int, default=8000)
    sub.add_parser("status", help="查看任务、待投递数量、schema和运行SHA")
    sub.add_parser("worker-health", help="检查worker持久心跳")
    test = sub.add_parser("test-push", help="明确的单条测试消息（默认模拟）")
    test.add_argument("--to", required=True, help="指定唯一测试成员UserID")
    test.add_argument("--text", default="自建股票监控测试消息📈")
    test.add_argument("--real", action="store_true", help="真的发送消息")
    imported = sub.add_parser("import-seen", help="验证并幂等导入从COS下载的seen.json")
    imported.add_argument("path")
    backup = sub.add_parser("backup", help="SQLite online backup，目标必须不存在")
    backup.add_argument("path")
    retry = sub.add_parser("retry-outbox", help="显式重试dead待投递项")
    retry.add_argument("id", type=int)
    sub.add_parser("demo", help="独立临时库内演示明确指令→动态检索→分析→模拟投递")
    return result


def load_env(path):
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid env file line")
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class FakeFetcher:
    def __init__(self):
        self.sequence = 0
        self.queries = []

    def fetch_target(self, target, *, now=None):
        from .news import NewsArticle
        self.queries.append(target.query)
        return [NewsArticle(key=f"fake-{target.key}-{self.sequence}", title=f"{target.name} 模拟新订单📈",
                            url=f"https://example.invalid/{target.key}/{self.sequence}",
                            published_at=now or datetime.now(timezone.utc),
                            summary="这是离线fixture，不代表真实新闻。", target_keys=(target.key,))]


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, text, to_user=None):
        self.sent.append({"to_user": to_user, "bytes": len(text.encode("utf-8"))})
        return {"code": 0, "accepted_chunks": 1, "msgids": ["fake-message"]}

    def send_chunk(self, content, to_user=None, **kwargs):
        return self.send(content, to_user=to_user)


def monitor_service(settings, *, fake=False, db=None):
    from .analyzer import DeepSeekAnalyzer, DeepSeekHTTPTransport, FakeAnalyzer
    from .monitor_router import MonitorRouter
    from .news import EastmoneyNewsClient
    fetcher = FakeFetcher() if fake else EastmoneyNewsClient(timeout=settings.news_timeout_seconds)
    analyzer = FakeAnalyzer() if fake else DeepSeekAnalyzer(
        DeepSeekHTTPTransport(settings.deepseek_api_key, settings.deepseek_url),
        model=settings.deepseek_model, timeout=settings.model_timeout_seconds,
    )
    return MonitorRouter(db or settings.database_path, fetcher, analyzer,
                         corp_id=settings.wecom_corp_id,
                         budget_limits={"hour_limit": settings.news_hourly_token_budget,
                                        "day_limit": settings.news_daily_token_budget,
                                        "major_reserve_fraction": settings.news_major_reserve_fraction},
                         environ={"MONITOR_ENABLED": str(settings.monitor_enabled).lower()})


def run_worker(settings, *, once=False, fake=False):
    from .callback_service import CallbackService
    from .delivery import DeliveryDispatcher
    from .push import WeComClient
    from .worker import run_worker_once
    if not fake:
        from .model_usage import recover_reservations
        recover_reservations(settings.database_path,
            before=datetime.now(timezone.utc)-timedelta(seconds=max(1200, settings.model_timeout_seconds+60)))
    service = monitor_service(settings, fake=fake) if (fake or settings.monitor_enabled) else None
    callback = CallbackService(settings)
    if fake:
        from .intent import IntentError
        class OfflineIntent:
            def parse(self, *_args):
                raise IntentError("离线模拟未调用模型，临时库配置未修改。")
        callback.intent_client = OfflineIntent()
    client = FakeSender() if fake else WeComClient(settings)
    dispatcher = DeliveryDispatcher(settings, client)
    stopped = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stopped.set())
    def heartbeat():
        conn = connect(settings.database_path)
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute("INSERT INTO monitor_state(key,value,updated_at) VALUES('worker:heartbeat',?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (settings.build_sha, stamp))
        finally:
            conn.close()
    def heartbeat_loop():
        while not stopped.is_set():
            try:
                heartbeat()
            except sqlite3.Error as exc:
                logging.getLogger(__name__).error("worker_heartbeat_failed error_type=%s", type(exc).__name__)
            stopped.wait(20)
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True, name="worker-heartbeat")
    heartbeat_thread.start()
    while not stopped.is_set():
        try:
            result = run_worker_once(service, dispatcher, callback_service=callback)
            if result.monitor and result.monitor.status != "skipped":
                output(result.monitor)
        except Exception as exc:
            logging.getLogger(__name__).error("worker_cycle_failed error_type=%s", type(exc).__name__)
            if once:
                stopped.set()
                heartbeat_thread.join(timeout=5)
                return 1
        if once:
            stopped.set()
            heartbeat_thread.join(timeout=5)
            return 0
        stopped.wait(settings.worker_poll_seconds)
    heartbeat_thread.join(timeout=5)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.env_file:
            load_env(args.env_file)
        settings = Settings.from_env()
        if args.database:
            from dataclasses import replace
            settings = replace(settings, database_path=Path(args.database))
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if args.command == "demo":
            return demo(settings)
        # dry-run opens source read-only via backup, never creates formal data.
        scan_command = args.command in {"monitor", "backfill"}
        backfill_options = {"backfill_hours": args.hours, "backfill_target": args.target,
                            "member_user": args.member} if args.command == "backfill" else {}
        if args.command == "backfill" and args.real and not args.dry_run and args.ack_max_notifications != 10:
            print("真实补查可能新增最多10条通知；请先预览，再显式添加 --ack-max-notifications 10。", file=sys.stderr)
            return 1
        if scan_command and (args.dry_run or not args.real):
            with tempfile.TemporaryDirectory(prefix="stock-watch-dry-") as directory:
                temp_db = Path(directory) / "dry-run.db"
                target = sqlite3.connect(temp_db)
                if settings.database_path.exists():
                    source = sqlite3.connect(f"file:{settings.database_path.as_posix()}?mode=ro", uri=True)
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                target.close()
                conn = connect(temp_db)
                init_db(conn)
                independent = conn.execute("SELECT 1 FROM app_meta WHERE key='independent_subscriptions'").fetchone() is not None
                conn.close()
                service = monitor_service(settings, fake=not args.real, db=temp_db)
                output({"dry_run": True, "external_model": bool(args.real) and not independent, "delivery": False,
                        "analysis_mode": "cache_preview" if args.real and independent else "real" if args.real else "fake",
                        "notification_limit": 10, **backfill_options,
                        "result": asdict(service.run_once(force=True, dry_run=True, **backfill_options))})
                return 0
        if args.command == "worker" and args.fake:
            from dataclasses import replace
            with tempfile.TemporaryDirectory(prefix="stock-watch-fake-worker-") as directory:
                temp_db = Path(directory) / "worker.db"
                target = sqlite3.connect(temp_db)
                if settings.database_path.exists():
                    source = sqlite3.connect(f"file:{settings.database_path.as_posix()}?mode=ro", uri=True)
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                target.close()
                conn = connect(temp_db)
                init_db(conn)
                conn.close()
                isolated = replace(settings, database_path=temp_db, monitor_enabled=False)
                return run_worker(isolated, once=args.once, fake=True)
        conn = connect(settings.database_path)
        try:
            init_db(conn)
            if args.command == "init-db":
                from .subscription_scan import prepare_history
                prepare_history(conn, datetime.now(timezone.utc))
                output({"database": str(settings.database_path.resolve()), "schema_version": SCHEMA_VERSION,
                        "sqlite": sqlite3.sqlite_version, "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                        "monitor_enabled": settings.monitor_enabled})
            elif args.command == "config":
                from .config import ConfigStore
                from .subscriptions import MemberConfigStore, MemberRegistry
                enabled = conn.execute("SELECT 1 FROM app_meta WHERE key='independent_subscriptions'").fetchone()
                registry = MemberRegistry(conn, settings.wecom_corp_id) if (args.member or enabled) else None
                if args.member:
                    member = registry.get(args.member)
                    if member is None or not member.active:
                        raise ConfigError("该成员未准入或已停用。")
                    store = MemberConfigStore(conn, member.id)
                elif registry is not None and registry.is_enabled():
                    raise ConfigError("独立订阅模式请使用 config --member 指定成员。")
                else:
                    store = ConfigStore(conn)
                output(store.snapshot().to_dict())
            elif args.command == "command":
                from .commands import CommandService
                from .config import ConfigStore
                from .subscriptions import MemberConfigStore, MemberRegistry
                enabled = conn.execute("SELECT 1 FROM app_meta WHERE key='independent_subscriptions'").fetchone()
                registry = MemberRegistry(conn, settings.wecom_corp_id) if (args.member or enabled) else None
                user = args.member or args.user
                if args.member or registry is not None and registry.is_enabled():
                    member = registry.get(user)
                    if member is None or not member.active:
                        raise ConfigError("该成员未准入或已停用。")
                    store = MemberConfigStore(conn, member.id)
                else:
                    store = ConfigStore(conn)
                result = CommandService(store).execute(args.text, user_id=user)
                print(result.text)
            elif args.command == "members":
                from .subscriptions import MemberRegistry
                registry = MemberRegistry(conn, settings.wecom_corp_id)
                if args.member_action == "list":
                    output({"independent_subscriptions": registry.is_enabled(),
                            "members": [member.to_dict() for member in registry.all()]})
                elif args.member_action == "add":
                    output(registry.add(args.user, role=args.role).to_dict())
                elif args.member_action == "revoke":
                    output(registry.revoke(args.user).to_dict())
                elif args.member_action == "assign-legacy-owner":
                    output(registry.assign_legacy_owner(args.user).to_dict())
                elif args.member_action == "analysis":
                    output(registry.set_analysis_private(args.user, args.private).to_dict())
            elif args.command == "status":
                from .model_usage import budget_status
                member_mode = conn.execute("SELECT value FROM app_meta WHERE key='independent_subscriptions'").fetchone()
                output({"build_sha": settings.build_sha, "schema_version": SCHEMA_VERSION,
                        "monitor_enabled": settings.monitor_enabled,
                        "independent_subscriptions": member_mode is not None,
                        "subscription_organization_matches": member_mode is None or member_mode[0] == settings.wecom_corp_id,
                        "subscription_members": [dict(row) for row in conn.execute(
                            "SELECT active,count(*) AS count FROM members WHERE corp_id=? GROUP BY active",
                            (settings.wecom_corp_id,))],
                        "outbox": [dict(row) for row in conn.execute("SELECT status,count(*) AS count FROM outbox GROUP BY status")],
                        "management_tasks": [dict(row) for row in conn.execute("SELECT status,count(*) AS count FROM async_tasks GROUP BY status")],
                        "news_candidates": [dict(row) for row in conn.execute("SELECT status,count(*) AS count FROM notification_candidates GROUP BY status")],
                        "news_budget": budget_status(conn, hour_limit=settings.news_hourly_token_budget,
                            day_limit=settings.news_daily_token_budget, major_reserve_fraction=settings.news_major_reserve_fraction),
                        "model_usage": [dict(row) for row in conn.execute("SELECT category,COUNT(*) AS requests,SUM(total_tokens) AS measured_tokens,SUM(total_tokens IS NULL) AS unknown_attempts,SUM(budget_charge) AS budget_charge FROM model_usage GROUP BY category")],
                        "runs": [dict(row) for row in conn.execute("SELECT * FROM monitor_runs ORDER BY started_at DESC LIMIT 10")]})
            elif args.command == "worker-health":
                row = conn.execute("SELECT updated_at FROM monitor_state WHERE key='worker:heartbeat'").fetchone()
                if not row or datetime.fromisoformat(row[0].replace("Z", "+00:00")) < datetime.now(timezone.utc) - timedelta(minutes=3):
                    return 1
                output({"worker": "ready"})
            elif args.command == "retry-outbox":
                cursor = conn.execute("UPDATE outbox SET status='pending',attempts=0,available_at=?,locked_until=NULL "
                                      "WHERE id=? AND status='dead'", (datetime.now(timezone.utc).isoformat(), args.id))
                output({"retried": cursor.rowcount})
        finally:
            conn.close()
        if scan_command:
            output(monitor_service(settings, fake=not args.real).run_once(force=True, **backfill_options))
        elif args.command == "worker":
            return run_worker(settings, once=args.once, fake=args.fake)
        elif args.command == "serve":
            from .api import create_app
            print(f"http://127.0.0.1:{args.port} (MONITOR_ENABLED={settings.monitor_enabled})", flush=True)
            with make_server("127.0.0.1", args.port, create_app(settings)) as server:
                server.serve_forever()
        elif args.command == "test-push":
            if "|" in args.to or "," in args.to or args.to == "@all":
                raise ValueError("test-push requires one explicit member UserID")
            from .push import WeComClient
            output((WeComClient(settings) if args.real else FakeSender()).send(args.text, to_user=args.to))
        elif args.command == "import-seen":
            from .migration import import_seen
            output(import_seen(settings.database_path, args.path))
        elif args.command == "backup":
            from .migration import backup_database
            output(backup_database(settings.database_path, args.path))
        return 0
    except ConfigError as exc:
        print(f"配置未修改：{exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        # Never print upstream exception URLs or credentials.
        print(f"操作失败：{type(exc).__name__}。检查配置和本地状态；运行 --help 查看命令。", file=sys.stderr)
        return 1


def demo(settings):
    from dataclasses import replace
    from .commands import CommandService
    from .config import ConfigStore
    from .delivery import DeliveryDispatcher
    with tempfile.TemporaryDirectory(prefix="stock-watch-demo-") as directory:
        settings = replace(settings, database_path=Path(directory) / "demo.db", wecom_to_users=("offline-user",))
        conn = connect(settings.database_path)
        init_db(conn)
        CommandService(ConfigStore(conn)).execute("添加方向 半导体设备", user_id="offline-admin")
        conn.close()
        service = monitor_service(settings, fake=True)
        first = service.run_once(force=True)
        service.fetcher.sequence += 1
        second = service.run_once(force=True)
        sender = FakeSender()
        dispatcher = DeliveryDispatcher(settings, sender)
        for _ in range(20):
            if not dispatcher.deliver_one():
                break
        output({"offline": True, "baseline": asdict(first), "next_scan": asdict(second),
                "queries": service.fetcher.queries, "deliveries": sender.sent})
    return 0
