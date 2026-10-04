"""Unit tests for the in-memory rate limiter."""
from app.core.rate_limit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_per_window():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_s=60, clock=clock)
    assert [limiter.allow("u1") for _ in range(3)] == [True, True, False]
    assert limiter.allow("u2")  # per key


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_s=60, clock=clock)
    assert limiter.allow("u")
    clock.now = 59.9
    assert not limiter.allow("u")
    clock.now = 60.0
    assert limiter.allow("u")


def test_refused_calls_do_not_extend_the_wait():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_s=60, clock=clock)
    limiter.allow("u")
    for t in (10, 20, 30):
        clock.now = t
        assert not limiter.allow("u")
    clock.now = 60
    assert limiter.allow("u")
