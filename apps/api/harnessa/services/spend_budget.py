"""Budget accounting for independently billed evaluator requests."""

from decimal import Decimal

from harnessa.config import settings
from harnessa.db.model_gateway import ModelGlobalSpendBudget
from harnessa.db.session import SessionLocal
from sqlalchemy import select


class GlobalSpendBudgetError(RuntimeError):
    """An external evaluator request cannot fit under the shared spend ceiling."""


def would_exceed_global_spend_budget(
    budget: ModelGlobalSpendBudget, amount_usd: Decimal, configured_limit_usd: Decimal
) -> bool:
    """Use the same committed and in-flight total for every external admission path."""
    limit_usd = min(budget.max_usd, configured_limit_usd)
    committed_usd = budget.billed_usd + budget.estimated_usd + budget.reserved_usd
    return committed_usd + amount_usd > limit_usd


async def record_external_spend_estimate(amount_usd: Decimal) -> None:
    """Book an evaluator's declared maximum before dispatch; retain it conservatively."""
    if amount_usd <= 0:
        raise ValueError("External evaluator cost ceiling must be positive")

    async with SessionLocal() as db:
        budget = await db.scalar(
            select(ModelGlobalSpendBudget)
            .where(ModelGlobalSpendBudget.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if budget is None:
            raise GlobalSpendBudgetError("The shared model spend budget is not initialized")

        if would_exceed_global_spend_budget(
            budget, amount_usd, settings.model_global_budget_usd
        ):
            raise GlobalSpendBudgetError("External evaluator cost exceeds the shared spend ceiling")

        # Keep the full configured maximum charged even if the evaluator fails or
        # does not return billing data; a timeout may have happened after dispatch.
        budget.estimated_usd += amount_usd
        await db.commit()
