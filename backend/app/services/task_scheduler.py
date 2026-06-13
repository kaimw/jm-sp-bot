from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class RetryPolicy:
    base_delay_seconds: int = 60
    multiplier: int = 2
    max_delay_seconds: int | None = 3600
    jitter_seconds: int = 0


class TaskScheduler:
    def __init__(self, policy: RetryPolicy, *, rng: random.Random | None = None):
        self.policy = policy
        self.rng = rng or random

    def delay_seconds(self, attempt_count: int) -> int:
        attempt = max(1, int(attempt_count or 1))
        base = max(1, int(self.policy.base_delay_seconds or 1))
        multiplier = max(1, int(self.policy.multiplier or 1))
        delay = base * (multiplier ** max(0, attempt - 1))
        if self.policy.max_delay_seconds is not None:
            delay = min(delay, max(1, int(self.policy.max_delay_seconds)))
        jitter = max(0, int(self.policy.jitter_seconds or 0))
        if jitter:
            delay += self.rng.randint(-jitter, jitter)
        return max(1, delay)

    def next_retry_at(self, attempt_count: int, *, now: datetime | None = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        return current + timedelta(seconds=self.delay_seconds(attempt_count))


def next_retry_at(attempt_count: int, policy: RetryPolicy, *, now: datetime | None = None, rng: random.Random | None = None) -> datetime:
    return TaskScheduler(policy, rng=rng).next_retry_at(attempt_count, now=now)
