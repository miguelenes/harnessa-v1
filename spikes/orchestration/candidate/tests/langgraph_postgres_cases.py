from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4
from typing_extensions import TypedDict

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


DATABASE_URL = os.environ.get("M4_SPIKE_DATABASE_URL", "")


class HumanAnswer(TypedDict):
    answer: str
    revision: int


class RunState(TypedDict, total=False):
    run_id: str
    question_id: str
    question_revision: int
    effect_key: str
    answer: HumanAnswer | None
    outcome: str


class StaleAnswer(ValueError):
    pass


def require_database() -> str:
    if not DATABASE_URL:
        pytest.skip("M4_SPIKE_DATABASE_URL must point to disposable PostgreSQL")
    if not DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
        raise RuntimeError("M4_SPIKE_DATABASE_URL must end with a database name ending in _test")
    return DATABASE_URL


def create_fixture_tables() -> None:
    with psycopg.connect(require_database()) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS spike_question_authority "
            "(question_id text PRIMARY KEY, revision integer NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS spike_answer_receipts "
            "(idempotency_key text PRIMARY KEY, question_id text NOT NULL, "
            "revision integer NOT NULL, answer jsonb NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS spike_effect_intents "
            "(stable_key text PRIMARY KEY, attempts integer NOT NULL, accepted_count integer NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS spike_dispatch_receipts "
            "(idempotency_key text PRIMARY KEY, run_id text NOT NULL)"
        )


def reset_fixture_tables() -> None:
    with psycopg.connect(require_database()) as connection:
        connection.execute(
            "TRUNCATE spike_question_authority, spike_answer_receipts, "
            "spike_effect_intents, spike_dispatch_receipts"
        )


def migration_state() -> tuple[int, int]:
    with psycopg.connect(require_database()) as connection:
        row = connection.execute(
            "SELECT count(*), COALESCE(max(v), -1) FROM checkpoint_migrations"
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def table_names() -> set[str]:
    with psycopg.connect(require_database()) as connection:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def insert_question(question_id: str, revision: int) -> None:
    with psycopg.connect(require_database()) as connection:
        connection.execute(
            "INSERT INTO spike_question_authority(question_id, revision) VALUES (%s, %s) "
            "ON CONFLICT (question_id) DO UPDATE SET revision = EXCLUDED.revision",
            (question_id, revision),
        )


def validate_answer(
    *, question_id: str, revision: int, idempotency_key: str, answer: HumanAnswer
) -> tuple[HumanAnswer, bool]:
    """Test-only stand-in for Harnessa's future durable question authority."""
    answer_json = json.dumps(answer, sort_keys=True)
    with psycopg.connect(require_database()) as connection:
        current = connection.execute(
            "SELECT revision FROM spike_question_authority WHERE question_id = %s",
            (question_id,),
        ).fetchone()
        if current is None or current[0] != revision:
            raise StaleAnswer("answer targets an unknown or stale question revision")
        prior = connection.execute(
            "SELECT question_id, revision, answer FROM spike_answer_receipts "
            "WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            if prior[0] != question_id or prior[1] != revision or prior[2] != answer:
                raise StaleAnswer("answer idempotency key conflicts with its original request")
            return answer, True
        connection.execute(
            "INSERT INTO spike_answer_receipts(idempotency_key, question_id, revision, answer) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (idempotency_key, question_id, revision, answer_json),
        )
    return answer, False


def record_effect_attempt(stable_key: str) -> None:
    """Model an external effect intent; only one acceptance may be recorded per key."""
    with psycopg.connect(require_database()) as connection:
        connection.execute(
            "INSERT INTO spike_effect_intents(stable_key, attempts, accepted_count) "
            "VALUES (%s, 1, 1) ON CONFLICT (stable_key) DO UPDATE "
            "SET attempts = spike_effect_intents.attempts + 1",
            (stable_key,),
        )


def effect_state(stable_key: str) -> tuple[int, int]:
    with psycopg.connect(require_database()) as connection:
        row = connection.execute(
            "SELECT attempts, accepted_count FROM spike_effect_intents WHERE stable_key = %s",
            (stable_key,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def build_approval_graph(saver: AsyncPostgresSaver):
    async def wait_for_answer(state: RunState) -> dict[str, object]:
        record_effect_attempt(state["effect_key"])
        answer = interrupt(
            {
                "question_id": state["question_id"],
                "revision": state["question_revision"],
            },
            response_schema=HumanAnswer,
        )
        return {"answer": answer, "outcome": "completed"}

    builder = StateGraph(RunState)
    builder.add_node("wait_for_answer", wait_for_answer)
    builder.add_edge(START, "wait_for_answer")
    builder.add_edge("wait_for_answer", END)
    return builder.compile(checkpointer=saver)


def config_for(thread_id: str) -> dict[str, object]:
    # A Harnessa Run.id is the stable LangGraph thread ID for this attempt.
    return {"configurable": {"thread_id": thread_id}}


def test_async_postgres_setup_is_repeatable_and_forward_only() -> None:
    async def exercise() -> None:
        create_fixture_tables()
        async with AsyncPostgresSaver.from_conn_string(require_database()) as saver:
            await saver.setup()
            first_state = migration_state()
            await saver.setup()
            assert migration_state() == first_state
            assert first_state[0] > 0
            assert {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"} <= table_names()
            # The package exposes setup/forward migration, not a schema downgrade API.
            assert not hasattr(saver, "downgrade")

    asyncio.run(exercise())


def test_human_wait_survives_restart_and_harnessa_fixture_deduplicates_answer_and_effect() -> None:
    async def exercise() -> None:
        create_fixture_tables()
        reset_fixture_tables()
        thread_id = str(uuid4())
        question_id = str(uuid4())
        revision = 7
        effect_key = f"effect:{thread_id}:approval"
        insert_question(question_id, revision)
        initial_state: RunState = {
            "run_id": thread_id,
            "question_id": question_id,
            "question_revision": revision,
            "effect_key": effect_key,
            "answer": None,
            "outcome": "waiting",
        }
        config = config_for(thread_id)

        async with AsyncPostgresSaver.from_conn_string(require_database()) as first_saver:
            await first_saver.setup()
            first_graph = build_approval_graph(first_saver)
            paused = await first_graph.ainvoke(initial_state, config)
            assert paused["__interrupt__"][0].value == {
                "question_id": question_id,
                "revision": revision,
            }
        assert effect_state(effect_key) == (1, 1)

        accepted_answer: HumanAnswer = {"answer": "approve", "revision": revision}
        answer, replayed = validate_answer(
            question_id=question_id,
            revision=revision,
            idempotency_key=f"answer:{question_id}:v{revision}",
            answer=accepted_answer,
        )
        assert replayed is False

        # New saver and graph model process restart during a human wait.
        async with AsyncPostgresSaver.from_conn_string(require_database()) as second_saver:
            await second_saver.setup()
            second_graph = build_approval_graph(second_saver)
            resumed = await second_graph.ainvoke(Command(resume=answer), config)
            assert resumed["outcome"] == "completed"
            assert resumed["answer"] == accepted_answer

        answer, replayed = validate_answer(
            question_id=question_id,
            revision=revision,
            idempotency_key=f"answer:{question_id}:v{revision}",
            answer=accepted_answer,
        )
        assert replayed is True
        with pytest.raises(StaleAnswer):
            validate_answer(
                question_id=question_id,
                revision=revision - 1,
                idempotency_key=f"stale:{question_id}",
                answer={"answer": "reject", "revision": revision - 1},
            )
        # LangGraph reruns the interrupted node from its beginning; the stable Harnessa key
        # keeps the accepted external effect at one while recording both attempts.
        assert effect_state(effect_key) == (2, 1)

    asyncio.run(exercise())


def test_duplicate_dispatch_is_gated_by_authority_key_before_graph_invocation() -> None:
    async def exercise() -> None:
        create_fixture_tables()
        reset_fixture_tables()
        dispatch_key = f"dispatch:{uuid4()}"
        run_id = str(uuid4())
        with psycopg.connect(require_database()) as connection:
            inserted = connection.execute(
                "INSERT INTO spike_dispatch_receipts(idempotency_key, run_id) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING run_id",
                (dispatch_key, run_id),
            ).fetchone()
            duplicate = connection.execute(
                "INSERT INTO spike_dispatch_receipts(idempotency_key, run_id) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING run_id",
                (dispatch_key, str(uuid4())),
            ).fetchone()
        assert inserted is not None
        assert duplicate is None
        # Dispatch authority is deliberately outside LangGraph; this is a fixture contract,
        # not evidence that the current Harnessa product has question APIs.

    asyncio.run(exercise())
