"""Environment-backed settings with secrets excluded from representations."""

from __future__ import annotations

import os
import re
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
    dashboard_username: str = ""
    dashboard_password: str = field(default="", repr=False)
    dashboard_session_secret: str = field(default="", repr=False)
    dashboard_origin: str = "https://stock.janettepa.uk"
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

    research_api_enabled: bool = False
    research_apply_enabled: bool = False
    research_allowed_companies: tuple[str, ...] = ("gigadevice", "alibaba", "smic")
    research_api_token: str = field(default="", repr=False)
    research_api_principal: str = "cloud-research"
    research_max_request_bytes: int = 4194304
    research_report_dir: Path | None = None

    def __post_init__(self) -> None:
        allowed = self.research_allowed_companies
        if not allowed or len(set(allowed)) != len(allowed) or not set(allowed) <= {"gigadevice", "alibaba", "smic"}:
            raise ValueError("invalid research company allowlist")
        if self.research_api_token and (len(self.research_api_token) < 32 or not self.research_api_token.isascii() or any(c.isspace() for c in self.research_api_token)):
            raise ValueError("invalid research API token")
        if not isinstance(self.research_api_principal, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", self.research_api_principal):
            raise ValueError("invalid research API principal")
        if type(self.research_max_request_bytes) is not int or not 1 <= self.research_max_request_bytes <= 16777216:
            raise ValueError("invalid research request size")
        report = self.research_report_dir or self.data_dir / "research" / "reports"
        if not report.resolve().is_relative_to(self.data_dir.resolve()) or report.resolve() == self.data_dir.resolve():
            raise ValueError("research report directory must be within DATA_DIR")
        object.__setattr__(self, "research_report_dir", report)

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
            research_api_enabled=_bool("RESEARCH_API_ENABLED"),
            research_apply_enabled=_bool("RESEARCH_APPLY_ENABLED"),
            research_allowed_companies=tuple(os.getenv("RESEARCH_ALLOWED_COMPANIES", "gigadevice,alibaba,smic").split(",")),
            research_api_token=os.getenv("RESEARCH_API_TOKEN", ""),
            research_api_principal=os.getenv("RESEARCH_API_PRINCIPAL", "cloud-research"),
            research_max_request_bytes=_budget_int("RESEARCH_MAX_REQUEST_BYTES", 4194304),
            research_report_dir=Path(os.environ["RESEARCH_REPORT_DIR"]) if "RESEARCH_REPORT_DIR" in os.environ else None,
            database_path=database_path,
            build_sha=os.getenv("BUILD_SHA", "development").strip() or "development",
            monitor_enabled=_bool("MONITOR_ENABLED", False),
            timezone=schedule_timezone,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            push_token=os.getenv("PUSH_TOKEN", "").strip(),
            dashboard_username=os.getenv("DASHBOARD_USERNAME", "").strip(),
            dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
            dashboard_session_secret=os.getenv("DASHBOARD_SESSION_SECRET", ""),
            dashboard_origin=os.getenv("DASHBOARD_ORIGIN", "https://stock.janettepa.uk").strip(),
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
