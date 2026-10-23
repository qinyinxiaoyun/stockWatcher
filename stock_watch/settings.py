"""Environment-backed settings with secrets excluded from representations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


def _budget_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value < 0:
            raise ValueError()
        return value
    except ValueError:
        raise ValueError(f"{name} must be a non-negative integer") from None


def _budget_fraction() -> float:
    try:
        value = float(os.getenv("NEWS_MAJOR_RESERVE_FRACTION", "0.2"))
        if not 0 <= value < 1:
            raise ValueError()
        return value
    except ValueError:
        raise ValueError("NEWS_MAJOR_RESERVE_FRACTION must be in [0,1)") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    build_sha: str = "development"
    monitor_enabled: bool = False
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    push_token: str = field(default="", repr=False)
    wecom_corp_id: str = ""
    wecom_agent_id: int | None = None
    wecom_secret: str = field(default="", repr=False)
    wecom_to_users: tuple[str, ...] = ()
    wecom_callback_token: str = field(default="", repr=False)
    wecom_encoding_aes_key: str = field(default="", repr=False)
    wecom_admin_userids: tuple[str, ...] = ()

    deepseek_api_key: str = field(default="", repr=False)
    deepseek_url: str = "https://api.deepseek.com/chat/completions"
    deepseek_model: str = "deepseek-flash"

    news_timeout_seconds: float = 12.0
    model_timeout_seconds: float = 90.0
    push_timeout_seconds: float = 12.0
    worker_poll_seconds: float = 2.0
    news_hourly_token_budget: int = 60_000
    news_daily_token_budget: int = 300_000
    news_major_reserve_fraction: float = .2

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "stock-watch.db"))).expanduser()
        agent_text = os.getenv("WECOM_AGENT_ID", "").strip()
        try:
            agent_id = int(agent_text) if agent_text else None
        except ValueError as exc:
            raise ValueError("WECOM_AGENT_ID must be a positive integer") from exc
        if agent_id is not None and agent_id <= 0:
            raise ValueError("WECOM_AGENT_ID must be a positive integer")
        schedule_timezone = os.getenv("SCHEDULE_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
        if schedule_timezone != "Asia/Shanghai":
            raise ValueError("SCHEDULE_TIMEZONE must be Asia/Shanghai")
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            build_sha=os.getenv("BUILD_SHA", "development").strip() or "development",
            monitor_enabled=_bool("MONITOR_ENABLED", False),
            timezone=schedule_timezone,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            push_token=os.getenv("PUSH_TOKEN", "").strip(),
            wecom_corp_id=os.getenv("WECOM_CORP_ID", "").strip(),
            wecom_agent_id=agent_id,
            wecom_secret=os.getenv("WECOM_SECRET", "").strip(),
            wecom_to_users=_csv("WECOM_TOUSER"),
            wecom_callback_token=os.getenv("WECOM_CALLBACK_TOKEN", "").strip(),
            wecom_encoding_aes_key=os.getenv("WECOM_ENCODING_AES_KEY", "").strip(),
            wecom_admin_userids=_csv("WECOM_ADMIN_USERIDS"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            deepseek_url=os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions").strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            news_timeout_seconds=float(os.getenv("NEWS_TIMEOUT_SECONDS", "12")),
            model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "90")),
            push_timeout_seconds=float(os.getenv("PUSH_TIMEOUT_SECONDS", "12")),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "2")),
            news_hourly_token_budget=_budget_int("NEWS_HOURLY_TOKEN_BUDGET", 60_000),
            news_daily_token_budget=_budget_int("NEWS_DAILY_TOKEN_BUDGET", 300_000),
            news_major_reserve_fraction=_budget_fraction(),
        )

    def ensure_data_dir(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_callback(self) -> None:
        missing = [
            name
            for name, value in (
                ("WECOM_CORP_ID", self.wecom_corp_id),
                ("WECOM_AGENT_ID", self.wecom_agent_id),
                ("WECOM_CALLBACK_TOKEN", self.wecom_callback_token),
                ("WECOM_ENCODING_AES_KEY", self.wecom_encoding_aes_key),
                ("WECOM_ADMIN_USERIDS", self.wecom_admin_userids),
            )
            if not value
        ]
        if missing:
            raise ValueError("callback settings missing: " + ", ".join(missing))

    def validate_push(self) -> None:
        missing = [
            name
            for name, value in (
                ("WECOM_CORP_ID", self.wecom_corp_id),
                ("WECOM_AGENT_ID", self.wecom_agent_id),
                ("WECOM_SECRET", self.wecom_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError("push settings missing: " + ", ".join(missing))
