FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.9.5 && uv sync --frozen --no-dev
COPY apps/api apps/api
ENV PYTHONPATH=/app/apps/api
CMD ["uv", "run", "--no-sync", "uvicorn", "harnessa.main:app", "--host", "0.0.0.0", "--port", "8000"]
