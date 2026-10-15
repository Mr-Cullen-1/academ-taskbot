from fastapi.testclient import TestClient

from taskbot_api.config import Settings
from taskbot_api.main import create_app


def test_health_is_available_without_database() -> None:
    app = create_app(Settings(_env_file=None, database_url=None))
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_missing_database_configuration() -> None:
    app = create_app(Settings(_env_file=None, database_url=None))
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "not_configured"}


def test_ready_reports_database_failure(monkeypatch) -> None:
    class FailingConnection:
        async def __aenter__(self):
            raise OSError("database unavailable")

        async def __aexit__(self, *_args):
            return None

    class FailingEngine:
        def connect(self):
            return FailingConnection()

        async def dispose(self):
            return None

    monkeypatch.setattr("taskbot_api.main.build_engine", lambda _settings: FailingEngine())
    app = create_app(Settings(_env_file=None, database_url=None))
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}


def test_ready_reports_database_success(monkeypatch) -> None:
    class WorkingConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return None

    class WorkingEngine:
        def connect(self):
            return WorkingConnection()

        async def dispose(self):
            return None

    monkeypatch.setattr("taskbot_api.main.build_engine", lambda _settings: WorkingEngine())
    app = create_app(Settings(_env_file=None, database_url=None))
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "available"}
