FROM node:26.12.0-bookworm-slim@sha256:742a0fe7320e09e0321222b68165e6ac731d30470e7ea7f98f9bfc3a6e1034fe AS openspec-runtime
WORKDIR /app
RUN npm install --global pnpm@10.34.7
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/cloud-portal/package.json apps/cloud-portal/package.json
COPY apps/marketing/package.json apps/marketing/package.json
COPY apps/web/package.json apps/web/package.json
COPY packages/contracts/package.json packages/contracts/package.json
COPY packages/design-tokens/package.json packages/design-tokens/package.json
COPY packages/ui-react/package.json packages/ui-react/package.json
COPY packages/ui-svelte/package.json packages/ui-svelte/package.json
RUN pnpm install --prod --frozen-lockfile --ignore-scripts --filter harnessa

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS python-dependencies
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.5 \
    && uv sync --frozen --no-dev

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
WORKDIR /app
COPY --from=openspec-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=openspec-runtime /app/node_modules /app/node_modules
COPY --from=python-dependencies /usr/local/bin/uv /usr/local/bin/uv
COPY --from=python-dependencies /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:/app/node_modules/.bin:${PATH}"
COPY apps/api apps/api
ENV PYTHONPATH=/app/apps/api
CMD ["uv", "run", "--no-sync", "uvicorn", "harnessa.main:app", "--host", "0.0.0.0", "--port", "8000"]
