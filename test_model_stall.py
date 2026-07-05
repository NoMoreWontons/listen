"""Smoke check: the model-load stall detector resets on progress and fires when truly stuck."""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_progress_resets_the_stall_clock():
    app._model_progress["pct"] = 0
    app.MODEL_LOAD_STALL_S = 3  # 3 fake "seconds" at poll_s=1

    ticks = iter([1, 2, 3, 4, 5])  # 5 ticks > MODEL_LOAD_STALL_S if it never reset

    class FakeFuture:
        def result(self, timeout):
            n = next(ticks, None)
            if n is None:
                return "DONE"
            app._model_progress["pct"] = n  # progress every tick resets the clock
            raise TimeoutError  # simulate "not done yet" for this poll

    try:
        app._poll_until_ready(FakeFuture(), poll_s=1)
    except TimeoutError:
        raise AssertionError("progress ticks should have kept resetting the stall clock")
    print("ok: progress resets the stall clock")


def test_no_progress_raises_timeout():
    app._model_progress["pct"] = 0
    app.MODEL_LOAD_STALL_S = 3

    class FakeFuture:
        def result(self, timeout):
            raise TimeoutError  # never completes, never reports new progress

    try:
        app._poll_until_ready(FakeFuture(), poll_s=1)
        raise AssertionError("expected a TimeoutError from a genuinely stalled load")
    except TimeoutError:
        print("ok: a real stall raises TimeoutError")


if __name__ == "__main__":
    test_progress_resets_the_stall_clock()
    test_no_progress_raises_timeout()
