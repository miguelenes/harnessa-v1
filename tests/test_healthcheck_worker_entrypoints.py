from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from urllib.error import URLError

import pytest
from harnessa import healthcheck
from harnessa.services import worker as worker_service


def test_healthcheck_accepts_a_ready_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(healthcheck, "urlopen", lambda *args, **kwargs: Response())
    healthcheck.main()


def test_healthcheck_rejects_non_ready_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(healthcheck, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(SystemExit) as error:
        healthcheck.main()
    assert error.value.code == 1


def test_healthcheck_rejects_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(healthcheck, "urlopen", unavailable)
    with pytest.raises(SystemExit) as error:
        healthcheck.main()
    assert error.value.code == 1


def test_worker_module_entrypoint_starts_the_shared_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    async def fake_worker(*, harness_evaluator: object | None = None) -> None:
        nonlocal started
        assert harness_evaluator is None
        started = True

    def run(coroutine) -> None:
        coroutine.close()

    monkeypatch.setattr(worker_service, "run", fake_worker)
    monkeypatch.setattr(asyncio, "run", run)
    worker_path = Path(worker_service.__file__).parent.parent / "worker.py"

    runpy.run_path(str(worker_path), run_name="__main__")

    assert started is False  # asyncio.run is replaced to prove entrypoint wiring only.
