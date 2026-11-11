"""Named laps for timing logs: one line that says where a slow request spent its time."""

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager


class Laps:
    """Time named steps, then log them as one line: `fetch 0.81s, store 0.20s`."""

    def __init__(self, on_start: Callable[[str], None] | None = None) -> None:
        self.started = time.perf_counter()
        self.laps: list[tuple[str, float]] = []
        self.on_start = on_start  # told each step's name as it begins (to show progress)

    @contextmanager
    def lap(self, name: str) -> Generator[None]:
        if self.on_start:
            self.on_start(name)
        start = time.perf_counter()
        try:
            yield
        finally:
            self.laps.append((name, time.perf_counter() - start))

    @property
    def total(self) -> float:
        return time.perf_counter() - self.started

    def __str__(self) -> str:
        return ", ".join(f"{name} {seconds:.2f}s" for name, seconds in self.laps)
