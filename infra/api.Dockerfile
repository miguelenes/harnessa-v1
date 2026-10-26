FROM node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS openspec-runtime
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

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS python-dependencies
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y git libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.5 \
    && uv sync --frozen --no-dev

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d
WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y git libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=openspec-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=openspec-runtime /app/node_modules /app/node_modules
COPY --from=python-dependencies /usr/local/bin/uv /usr/local/bin/uv
COPY --from=python-dependencies /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:/app/node_modules/.bin:${PATH}"
COPY apps/api apps/api
ENV PYTHONPATH=/app/apps/api
CMD ["uv", "run", "--no-sync", "uvicorn", "harnessa.main:app", "--host", "0.0.0.0", "--port", "8000"]
