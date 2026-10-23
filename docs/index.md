# Harnessa documentation

Harnessa is an open-source, self-hosted control plane for repository change workflows. The project is in active development; the [cloud portal](https://app.harnessa.cloud) is a static preview, and hosted provisioning is not available.

Use this guide to [run Harnessa locally](getting-started.md), understand its [architecture](architecture.md), and [contribute](contributing.md).

For GitHub App installation, secret-reference setup, repository opt-in, and delivery repair, see the [GitHub App integration guide](integrations/github-app.md).

## Project links

- [Project website](https://harnessa.cloud)
- [Cloud preview](https://app.harnessa.cloud)
- [Source code](https://github.com/miguelenes/harnessa-v1)
- [GitHub Discussions](https://github.com/miguelenes/harnessa-v1/discussions)
- [Report a vulnerability](https://github.com/miguelenes/harnessa-v1/security/advisories/new)

## What is implemented

The current foundation includes a modular monorepo, a FastAPI service, a Next.js app shell, durable orchestration, a private LiteLLM gateway, a shared UI system, and public marketing and portal previews. The [milestone reports](milestones/04.md) describe what each completed milestone covers; later milestones remain in progress until their acceptance evidence is recorded.

The cloud portal is informational only. It has no connected account, workspace provisioning, billing, or live service status.
