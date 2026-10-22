"""Application-owned ACP agent compatibility records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentTransport(StrEnum):
    NATIVE_ACP = "native_acp"
    ADAPTER = "adapter"
    BRIDGE = "bridge"


class CompatibilityStatus(StrEnum):
    VERIFIED = "verified"
    ADAPTER_REQUIRED = "adapter_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AgentCompatibility:
    agent_id: str
    display_name: str
    transport: AgentTransport
    status: CompatibilityStatus
    command: tuple[str, ...] | None
    package_version: str | None
    protocol_version: int | None
    resume: bool | None
    permissions: bool
    filesystem: bool
    terminal: bool
    model_route_compatibility: str
    source_url: str | None


# The OpenCode client is intentionally the only enabled native path. The other
# entries document protocol discovery without claiming a tested Harnessa runner.
AGENT_COMPATIBILITY: tuple[AgentCompatibility, ...] = (
    AgentCompatibility(
        agent_id="opencode",
        display_name="OpenCode",
        transport=AgentTransport.NATIVE_ACP,
        status=CompatibilityStatus.VERIFIED,
        command=("opencode", "acp"),
        package_version="2.0.32",
        protocol_version=1,
        resume=True,
        permissions=True,
        filesystem=True,
        terminal=True,
        model_route_compatibility="run-scoped Harnessa ModelRouter broker; disabled by default",
        source_url="https://opencode.ai/docs/acp/",
    ),
    AgentCompatibility(
        agent_id="codex",
        display_name="Codex CLI",
        transport=AgentTransport.BRIDGE,
        status=CompatibilityStatus.ADAPTER_REQUIRED,
        command=None,
        package_version=None,
        protocol_version=1,
        resume=None,
        permissions=True,
        filesystem=True,
        terminal=True,
        model_route_compatibility="unverified",
        source_url="https://github.com/agentclientprotocol/codex-acp",
    ),
    AgentCompatibility(
        agent_id="claude-code",
        display_name="Claude Code",
        transport=AgentTransport.BRIDGE,
        status=CompatibilityStatus.ADAPTER_REQUIRED,
        command=None,
        package_version=None,
        protocol_version=1,
        resume=None,
        permissions=True,
        filesystem=True,
        terminal=True,
        model_route_compatibility="unverified",
        source_url="https://github.com/agentclientprotocol/claude-agent-acp",
    ),
    AgentCompatibility(
        agent_id="goose",
        display_name="Goose",
        transport=AgentTransport.NATIVE_ACP,
        status=CompatibilityStatus.ADAPTER_REQUIRED,
        command=("goose", "acp"),
        package_version=None,
        protocol_version=1,
        resume=None,
        permissions=True,
        filesystem=True,
        terminal=True,
        model_route_compatibility="unverified",
        source_url="https://github.com/block/goose/tree/main/documentation/docs/gdk/acp",
    ),
    AgentCompatibility(
        agent_id="gemini-cli",
        display_name="Gemini CLI",
        transport=AgentTransport.NATIVE_ACP,
        status=CompatibilityStatus.ADAPTER_REQUIRED,
        command=("gemini", "--acp"),
        package_version=None,
        protocol_version=1,
        resume=None,
        permissions=True,
        filesystem=True,
        terminal=True,
        model_route_compatibility="unverified",
        source_url="https://github.com/google-gemini/gemini-cli",
    ),
)


def get_agent(agent_id: str) -> AgentCompatibility:
    for agent in AGENT_COMPATIBILITY:
        if agent.agent_id == agent_id:
            return agent
    raise KeyError(f"Unknown ACP agent id: {agent_id}")
