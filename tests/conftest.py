"""
Pytest configuration and shared fixtures.
Uses an in-memory SQLite database so tests never touch disk.
"""
import os
import pytest

_TEST_DB_URL = "sqlite:///./test_outreach.db"

# Point to test SQLite before any app code imports the engine
os.environ.setdefault("DATABASE_URL", _TEST_DB_URL)
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_placeholder")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base, get_db
from src.main import app


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(
        _TEST_DB_URL,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(db_engine):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
