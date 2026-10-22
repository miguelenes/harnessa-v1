FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.9.5 && uv sync --frozen --no-dev
COPY apps/api apps/api
ENV PYTHONPATH=/app/apps/api
CMD ["uv", "run", "--no-sync", "uvicorn", "harnessa.main:app", "--host", "0.0.0.0", "--port", "8000"]
