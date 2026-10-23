"""Separately runnable worker entry point; domain and adapters are shared with API."""

import asyncio

from harnessa.adapters.harness_evaluator import HTTPHarnessEvaluator
from harnessa.config import settings
from harnessa.services.worker import run


def _configured_harness_evaluator() -> HTTPHarnessEvaluator | None:
    endpoint = settings.harness_holdout_evaluator_url.strip()
    configured_token = settings.harness_holdout_evaluator_token
    token = configured_token.get_secret_value().strip() if configured_token is not None else ""
    if not endpoint and not token:
        return None
    if not endpoint or not token:
        raise ValueError(
            "HARNESS_HOLDOUT_EVALUATOR_URL and HARNESS_HOLDOUT_EVALUATOR_TOKEN must be set together"
        )
    return HTTPHarnessEvaluator(
        endpoint,
        token,
        timeout_seconds=settings.harness2_holdout_timeout_seconds,
    )

if __name__ == "__main__":
    asyncio.run(run(harness_evaluator=_configured_harness_evaluator()))
