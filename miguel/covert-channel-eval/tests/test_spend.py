import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from spend import BudgetExceeded, SpendLedger  # noqa: E402


def test_record_tracks_totals_roles_models_tokens():
    led = SpendLedger(cap_usd=1.0, label="run")
    led.record("m/a", "collab", 0.01, {
        "prompt_tokens": 10, "completion_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 3}, "cost": 0.01,
    })
    led.record("m/b", "monitor", None, None)
    d = led.to_dict()
    assert d["calls"] == 2 and d["unknown_cost_calls"] == 1
    assert d["total_usd"] == 0.01
    assert d["by_role"]["collab"]["usd"] == 0.01
    assert d["by_model"]["m/b"]["unknown_cost_calls"] == 1
    assert d["tokens"] == {"prompt": 10, "completion": 5, "reasoning": 3}
    assert d["label"] == "run" and d["cap_usd"] == 1.0 and not d["exhausted"]


def test_check_refuses_when_spent_reaches_cap():
    led = SpendLedger(cap_usd=0.05)
    led.record("m/a", "x", 0.05, None)
    with pytest.raises(BudgetExceeded):
        led.check("m/a")
    assert led.exhausted


def test_check_refuses_on_projected_cost():
    led = SpendLedger(cap_usd=0.10)
    led.record("m/a", "x", 0.06, None)
    led.check("m/b")  # no observation for m/b: projected 0
    with pytest.raises(BudgetExceeded):
        led.check("m/a")  # 0.06 + 0.06 > 0.10
    assert led.exhausted


def test_parent_cap_applies_to_child():
    batch = SpendLedger(cap_usd=0.10, label="batch")
    run = SpendLedger(cap_usd=None, parent=batch)
    run.record("m/a", "x", 0.10, None)
    with pytest.raises(BudgetExceeded, match="batch"):
        SpendLedger(cap_usd=5.0, parent=batch).check("m/b")
    assert batch.exhausted


def test_parent_accumulates_children_across_threads():
    batch = SpendLedger(cap_usd=None)
    runs = [SpendLedger(cap_usd=None, parent=batch) for _ in range(2)]

    def work(led):
        for _ in range(500):
            led.record("m/a", "collab", 0.001, {"prompt_tokens": 1})

    threads = [threading.Thread(target=work, args=(r,)) for r in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert batch.to_dict()["calls"] == 1000
    assert batch.total_usd == pytest.approx(1.0)
    assert batch.to_dict()["tokens"]["prompt"] == 1000
    assert all(r.total_usd == pytest.approx(0.5) for r in runs)
