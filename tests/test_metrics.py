import time
from unittest import mock

from wled_proxy.metrics import RateCounter


def test_rate_is_steady_for_steady_traffic():
    clock = [1000.0]
    with mock.patch.object(time, "monotonic", lambda: clock[0]):
        counter = RateCounter(window=2.0)
        for _ in range(300):  # 30 events per second for 10 seconds
            clock[0] += 1 / 30
            counter.add()
        assert counter.total == 300
        assert 29.0 < counter.rate < 31.0


def test_rate_decays_to_zero_when_traffic_stops():
    clock = [1000.0]
    with mock.patch.object(time, "monotonic", lambda: clock[0]):
        counter = RateCounter(window=2.0)
        for _ in range(60):
            clock[0] += 1 / 30
            counter.add()
        clock[0] += 3.0
        assert counter.rate < 15.0
        clock[0] += 10.0
        assert counter.rate == 0.0


def test_a_late_read_is_not_averaged_over_all_of_history():
    clock = [1000.0]
    with mock.patch.object(time, "monotonic", lambda: clock[0]):
        counter = RateCounter(window=2.0)
        counter.add(100)
        clock[0] += 60.0
        assert counter.rate == 0.0
        assert counter.total == 100
