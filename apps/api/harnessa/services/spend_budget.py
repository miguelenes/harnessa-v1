"""Budget accounting for independently billed evaluator requests."""

from decimal import Decimal

from harnessa.config import settings
from harnessa.db.model_gateway import ModelGlobalSpendBudget
from harnessa.db.session import SessionLocal
from sqlalchemy import select


class GlobalSpendBudgetError(RuntimeError):
    """An external evaluator request cannot fit under the shared spend ceiling."""


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

        global_limit = min(budget.max_usd, settings.model_global_budget_usd)
        if budget.billed_usd + budget.estimated_usd + budget.reserved_usd + amount_usd > global_limit:
            raise GlobalSpendBudgetError("External evaluator cost exceeds the shared spend ceiling")

        # Keep the full configured maximum charged even if the evaluator fails or
        # does not return billing data; a timeout may have happened after dispatch.
        budget.estimated_usd += amount_usd
        await db.commit()
