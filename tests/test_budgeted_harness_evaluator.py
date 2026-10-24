from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from harnessa.worker import BudgetedHarnessEvaluator


@pytest.mark.anyio
async def test_budgeted_evaluator_accounts_before_external_dispatch(monkeypatch) -> None:
    events: list[str] = []

    async def reserve(amount: Decimal) -> None:
        assert amount == Decimal("0.25")
        events.append("reserved")

    class FakeEvaluator:
        async def evaluate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
            events.append("dispatched")
            return {"accepted": True}

    monkeypatch.setattr("harnessa.worker.record_external_spend_estimate", reserve)
    evaluator = BudgetedHarnessEvaluator(FakeEvaluator(), Decimal("0.25"))

    assert await evaluator.evaluate({"manifest": "fixture"}) == {"accepted": True}
    assert events == ["reserved", "dispatched"]


def test_budgeted_evaluator_requires_a_positive_maximum() -> None:
    class FakeEvaluator:
        async def evaluate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"accepted": True}

    with pytest.raises(ValueError, match="must be positive"):
        BudgetedHarnessEvaluator(FakeEvaluator(), Decimal(0))
