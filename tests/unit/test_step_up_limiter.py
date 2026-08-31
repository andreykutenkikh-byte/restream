from app.step_up_limiter import StepUpRateLimiter


def test_step_up_limiter_lockout_retry_and_window_expiry() -> None:
    now = [100.0]
    limiter = StepUpRateLimiter(attempts=2, window_seconds=30, clock=lambda: now[0])
    assert limiter.retry_after("session-a:client-a") is None
    limiter.fail("session-a:client-a")
    assert limiter.retry_after("session-a:client-a") is None
    limiter.fail("session-a:client-a")
    assert limiter.retry_after("session-a:client-a") == 30
    now[0] = 129.1
    assert limiter.retry_after("session-a:client-a") == 1
    now[0] = 130.0
    assert limiter.retry_after("session-a:client-a") is None


def test_step_up_success_resets_only_its_identity() -> None:
    limiter = StepUpRateLimiter(attempts=1, window_seconds=30)
    limiter.fail("session-a:client")
    limiter.fail("session-b:client")
    assert limiter.retry_after("session-a:client") is not None
    assert limiter.retry_after("session-b:client") is not None
    limiter.success("session-a:client")
    assert limiter.retry_after("session-a:client") is None
    assert limiter.retry_after("session-b:client") is not None
