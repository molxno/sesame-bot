from sesame.guards import RateLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_allows_up_to_limit_then_blocks():
    clock = Clock()
    rl = RateLimiter(3, 10, clock=clock)
    assert [rl.hit("u") for _ in range(3)] == [0, 0, 0]
    assert rl.hit("u") == 10
    clock.t = 10.01
    assert rl.hit("u") == 0


def test_keys_are_independent():
    rl = RateLimiter(1, 10, clock=Clock())
    assert rl.hit("a") == 0
    assert rl.hit("b") == 0
    assert rl.hit("a") > 0


def test_memory_is_bounded():
    clock = Clock()
    rl = RateLimiter(1, 1, max_keys=100, clock=clock)
    for i in range(1000):
        clock.t += 0.001
        rl.hit(i)
    assert len(rl) <= 100
