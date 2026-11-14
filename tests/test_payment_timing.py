"""Payment timing: invoices and bills are paid at the policy's run times (here 11:59 UTC every day, so a person can
change course each day); scheduled lines (payroll, benefits) on their pay date from 00:30 UTC."""
import datetime as dt

import pytest

from venthora import priority_sop as sop

UTC = dt.UTC


def test_a_daily_run_at_1159_utc_whatever_the_company_time_zone():
    pol = sop.check_policy({"run_times": ["11:59"], "run_days": "every", "run_tz": "UTC"})
    tz = sop.run_timezone(pol, "Asia/Jakarta")
    assert tz == "UTC"
    sat = dt.datetime(2026, 11, 14, 11, 0, tzinfo=UTC)
    assert sop.next_run(pol, sat, tz) == dt.datetime(2026, 11, 14, 11, 59, tzinfo=dt.timezone.utc).astimezone(sop.next_run(pol, sat, tz).tzinfo)
    assert sop.due_slot(pol, dt.datetime(2026, 11, 14, 12, 5, tzinfo=UTC), tz, set()) == "2026-11-14 11:59"
    assert sop.due_slot(pol, dt.datetime(2026, 11, 14, 12, 5, tzinfo=UTC), tz, {"2026-11-14 11:59"}) is None  # once a day
    assert sop.due_slot(pol, dt.datetime(2026, 11, 14, 11, 58, tzinfo=UTC), tz, set()) is None
    assert sop.run_timezone(sop.check_policy({"run_times": ["11:59"]}), "Asia/Jakarta") == "Asia/Jakarta"  # company time by default


def test_the_time_zone_is_company_or_utc_only():
    with pytest.raises(ValueError):
        sop.check_policy({"run_tz": "Mars/Olympus"})


def test_scheduled_lines_wait_for_0030_utc():
    assert not sop.scheduled_pay_open(dt.datetime(2026, 11, 28, 0, 10, tzinfo=UTC))
    assert sop.scheduled_pay_open(dt.datetime(2026, 11, 28, 0, 30, tzinfo=UTC))
    assert sop.scheduled_pay_open(dt.datetime(2026, 11, 28, 13, 0, tzinfo=UTC))
