"""Shared test setup."""

import pytest

from scriptorium_server.grobid import Grobid, GrobidUnavailable


@pytest.fixture(autouse=True)
def no_real_grobid(monkeypatch):
    """No test may start or call the real GROBID (a 3-4 GB Java service):
    tests that need one pass a fake."""
    def refuse(self, *args, **kwargs):
        raise GrobidUnavailable("tests never start the real GROBID")

    monkeypatch.setattr(Grobid, "ensure", refuse)
    monkeypatch.setattr(Grobid, "parse", refuse)


# -- the test database ---------------------------------------------------------
#
# Tests that use the database take the `db` fixture: a connection to the
# test database (SCRIPTORIUM_TEST_DATABASE, default the local database
# scriptorium_test) inside a schema of its own, migrated, and dropped
# afterwards, so tests never see each other's rows.

import os  # noqa: E402
import uuid  # noqa: E402

import psycopg  # noqa: E402

from scriptorium_server import db as database  # noqa: E402

TEST_DATABASE = os.environ.get("SCRIPTORIUM_TEST_DATABASE", "postgresql:///scriptorium_test")


@pytest.fixture(scope="session")
def test_database() -> str:
    try:
        with psycopg.connect(TEST_DATABASE, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    except psycopg.Error as exc:
        raise RuntimeError(
            f"the tests need a PostgreSQL database with pgvector at {TEST_DATABASE}"
            " (create it with `createdb scriptorium_test`, or set"
            f" SCRIPTORIUM_TEST_DATABASE): {exc}") from exc
    return TEST_DATABASE


@pytest.fixture
def empty_schema(test_database):
    """(address, schema name) of a new, empty schema; dropped after the test."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    yield test_database, schema
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(empty_schema):
    """A connection to a migrated, empty copy of the schema."""
    conn = database.connect(*empty_schema)
    database.migrate(conn)
    yield conn
    conn.close()
