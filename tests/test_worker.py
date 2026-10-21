from datetime import datetime
from zoneinfo import ZoneInfo

from stock_watch.worker import run_worker_once


def test_disabled_monitor_does_not_disable_management_or_receipts():
    class Callback:
        called = False
        def run_one(self):
            self.called = True
            return True
    class Delivery:
        called = False
        def deliver_one(self, now=None):
            self.called = True
            return True
    callback, delivery = Callback(), Delivery()
    result = run_worker_once(None, delivery, callback_service=callback)
    assert result.management_processed and result.delivery_processed


def test_scan_clock_is_observed_after_management_at_cutoff():
    from stock_watch.scheduler import slot_for_time
    clock = [datetime(2026, 10, 21, 15, 4, 50, tzinfo=ZoneInfo("Asia/Shanghai"))]
    class Callback:
        def run_one(self):
            clock[0] = clock[0].replace(minute=5, second=10)
            return True
    class Service:
        slot = "not-run"
        def run_once(self, now=None, **kwargs):
            self.slot = slot_for_time(now or clock[0])
    service = Service()
    run_worker_once(service, callback_service=Callback())
    assert service.slot is None
