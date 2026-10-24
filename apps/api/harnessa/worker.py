"""Separately runnable worker entry point; domain and adapters are shared with API."""

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from harnessa.adapters.harness_evaluator import HTTPHarnessEvaluator
from harnessa.adapters.protocols import HarnessEvaluator
from harnessa.config import settings
from harnessa.services.spend_budget import record_external_spend_estimate
from harnessa.services.worker import run


class BudgetedHarnessEvaluator:
    """Book a configured worst-case evaluator charge before making its request."""

    def __init__(self, evaluator: HarnessEvaluator, max_cost_usd: Decimal) -> None:
        if max_cost_usd <= 0:
            raise ValueError("HARNESS_HOLDOUT_EVALUATOR_MAX_COST_USD must be positive")
        self._evaluator = evaluator
        self._max_cost_usd = max_cost_usd

    async def evaluate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        await record_external_spend_estimate(self._max_cost_usd)
        return await self._evaluator.evaluate(candidate)


def _configured_harness_evaluator() -> HarnessEvaluator | None:
    endpoint = settings.harness_holdout_evaluator_url.strip()
    configured_token = settings.harness_holdout_evaluator_token
    token = configured_token.get_secret_value().strip() if configured_token is not None else ""
    if not endpoint and not token:
        return None
    if not endpoint or not token:
        raise ValueError(
            "HARNESS_HOLDOUT_EVALUATOR_URL and HARNESS_HOLDOUT_EVALUATOR_TOKEN must be set together"
        )
    return BudgetedHarnessEvaluator(
        HTTPHarnessEvaluator(
            endpoint,
            token,
            timeout_seconds=settings.harness2_holdout_timeout_seconds,
        ),
        settings.harness_holdout_evaluator_max_cost_usd,
    )

if __name__ == "__main__":
    asyncio.run(run(harness_evaluator=_configured_harness_evaluator()))
