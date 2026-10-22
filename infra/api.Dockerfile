FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.9.5 && uv sync --frozen --no-dev
COPY apps/api apps/api
ENV PYTHONPATH=/app/apps/api
CMD ["uv", "run", "--no-sync", "uvicorn", "harnessa.main:app", "--host", "0.0.0.0", "--port", "8000"]
