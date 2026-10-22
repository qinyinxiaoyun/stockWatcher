"""Choose the persisted monitoring mode at each worker scan boundary."""
from __future__ import annotations

from .db import connect
from .monitor import MonitorService


class MonitorRouter:
    def __init__(self, db, fetcher, analyzer, *, corp_id, environ=None):
        self.db, self.fetcher, self.analyzer = db, fetcher, analyzer
        self.corp_id, self.environ = corp_id, environ
        self.legacy = MonitorService(db, fetcher, analyzer, environ=environ)
        self.member_service = None

    def run_once(self, **kwargs):
        from .subscriptions import MemberRegistry

        member_user = kwargs.pop("member_user", None)
        conn = connect(self.db)
        try:
            mode = conn.execute(
                "SELECT value FROM app_meta WHERE key='independent_subscriptions'"
            ).fetchone()
            enabled = mode is not None
            registry = MemberRegistry(conn, self.corp_id) if enabled else None
            if registry is not None:
                registry.is_enabled()  # Also verifies the configured organization.
            if member_user is not None:
                if not enabled:
                    raise ValueError("member backfill requires independent subscriptions")
                member = registry.get(member_user)
                if member is None or not member.active:
                    raise ValueError("active subscription member required")
                kwargs["member_id"] = member.id
        finally:
            conn.close()
        if enabled:
            if self.member_service is None:
                from .subscription_monitor import SubscriptionMonitorService

                self.member_service = SubscriptionMonitorService(
                    self.db, self.fetcher, self.analyzer,
                    corp_id=self.corp_id, environ=self.environ,
                )
            return self.member_service.run_once(**kwargs)
        if kwargs.get("member_id") is not None:
            raise ValueError("member backfill requires independent subscriptions")
        kwargs.pop("member_id", None)
        return self.legacy.run_once(**kwargs)
