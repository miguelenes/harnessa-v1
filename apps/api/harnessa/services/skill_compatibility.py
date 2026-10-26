"""Stable capability snapshots for imported and active skill artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from harnessa.db.model_gateway import AliasDeployment, ModelAlias, ModelDeployment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def route_manifest(
    db: AsyncSession, organization_id: UUID, alias_name: str
) -> dict[str, Any]:
    alias = await db.scalar(select(ModelAlias).where(
        ModelAlias.organization_id == organization_id,
        ModelAlias.name == alias_name,
        ModelAlias.enabled.is_(True),
    ))
    if alias is None:
        raise ValueError("The selected model alias is not enabled")
    deployments = list(await db.execute(
        select(ModelDeployment, AliasDeployment.priority)
        .join(AliasDeployment, AliasDeployment.deployment_id == ModelDeployment.id)
        .where(AliasDeployment.alias_id == alias.id, ModelDeployment.enabled.is_(True))
        .order_by(AliasDeployment.priority, ModelDeployment.id)
    ))
    if not deployments:
        raise ValueError("The selected model alias has no enabled deployments")
    return {
        "alias": alias_name,
        "deployments": [
            {
                "id": str(deployment.id),
                "proxy_model": deployment.proxy_model,
                "actual_model": deployment.actual_model,
                "priority": priority,
                "price_revision": deployment.price_revision,
                "capabilities": deployment.capabilities,
                "context_window": deployment.context_window,
                "input_cost_per_million": str(deployment.input_cost_per_million),
                "output_cost_per_million": str(deployment.output_cost_per_million),
            }
            for deployment, priority in deployments
        ],
    }


def route_capability_revision(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def route_snapshot_matches(
    frozen: object, current: object, expected_alias: object
) -> bool:
    """Check the exact alias and deployment/capability/price snapshot for an evaluation."""
    if not isinstance(frozen, dict) or not isinstance(current, dict):
        return False
    if not isinstance(expected_alias, str) or not expected_alias:
        return False
    if frozen.get("alias") != expected_alias or current.get("alias") != expected_alias:
        return False
    return route_capability_revision(frozen) == route_capability_revision(current)
