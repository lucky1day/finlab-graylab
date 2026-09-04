from __future__ import annotations

from types import SimpleNamespace

from backend import db


def test_dashboard_engine_has_bounded_read_headroom(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        db.DatabaseConfig,
        "from_env",
        staticmethod(
            lambda: SimpleNamespace(
                user="reader",
                password="secret",
                host="127.0.0.1",
                port=3306,
                database="bond_db",
                charset="utf8mb4",
            )
        ),
    )

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    db.get_dashboard_engine.cache_clear()
    try:
        db.get_dashboard_engine()
    finally:
        db.get_dashboard_engine.cache_clear()

    assert captured["connect_args"] == {
        "connect_timeout": 0.5,
        "read_timeout": 2.0,
        "write_timeout": 0.5,
        "init_command": "SET SESSION MAX_EXECUTION_TIME=1000",
    }
    assert captured["pool_pre_ping"] is True
    assert captured["pool_recycle"] == 300
