"""Rate limiter tests: per-host serialization with fake clock, no real sleeps."""

import threading

from prime_sportdata.rate_limit import RateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecorderSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)  # never actually sleeps


def make_limiter(interval: float = 2.0, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    sleeper = RecorderSleep()
    limiter = RateLimiter(interval=interval, clock=clock, sleep=sleeper)
    return limiter, clock, sleeper


def test_serializes_two_calls_for_same_host():
    limiter, clock, sleeper = make_limiter(interval=2.0)
    limiter.acquire("www.flashscore.com")
    assert sleeper.calls == []

    limiter.acquire("www.flashscore.com")  # same host, 0 s since last -> full wait
    assert sleeper.calls == [2.0]

    clock.advance(1.5)  # only 1.5 s elapsed -> still 0.5 s of wait
    limiter.acquire("www.flashscore.com")
    assert sleeper.calls == [2.0, 0.5]


def test_no_wait_when_interval_elapsed():
    limiter, clock, sleeper = make_limiter(interval=2.0)
    limiter.acquire("host")
    clock.advance(2.5)
    limiter.acquire("host")
    assert sleeper.calls == []


def test_does_not_block_different_hosts():
    limiter, clock, sleeper = make_limiter(interval=10.0)
    limiter.acquire("host-a")
    clock.advance(0.1)
    limiter.acquire("host-b")  # fresh host: no wait even though interval is huge
    assert sleeper.calls == []

    limiter.acquire("host-a")  # host-a last hit at 0.0, now 0.1 -> waits 9.9
    clock.advance(0.1)  # t = 0.2
    limiter.acquire("host-b")  # host-b last hit at 0.1, now 0.2 -> waits 9.9
    assert sleeper.calls == [9.9, 9.9]


def test_last_hit_stamp_updated_after_wait():
    limiter, clock, sleeper = make_limiter(interval=1.0)
    limiter.acquire("host")  # stamped at 0.0
    clock.advance(0.5)
    limiter.acquire("host")  # waits 0.5; stamp refreshed to the post-wait moment
    clock.advance(0.4)
    limiter.acquire("host")  # 0.4 s since the refreshed stamp -> waits 0.6
    assert sleeper.calls == [0.5, 0.6]


def test_concurrent_different_hosts_do_not_block_each_other():
    limiter, _, sleeper = make_limiter(interval=10.0)
    barrier = threading.Barrier(2, timeout=10)
    done: list[str] = []

    def worker(host: str) -> None:
        barrier.wait()
        limiter.acquire(host)  # no own history: must not wait at all
        done.append(host)

    threads = [threading.Thread(target=worker, args=(h,)) for h in ("host-1", "host-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(done) == ["host-1", "host-2"]
    assert sleeper.calls == []  # no cross-host blocking even concurrently
