from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册不会隐式连接数据库的 MySQL 集成测试开关。"""
    parser.addoption(
        "--mysql-integration",
        action="store_true",
        default=False,
        help="run tests that create an isolated temporary MySQL database",
    )


def pytest_configure(config: pytest.Config) -> None:
    """显式选择集成层时要求调用方注入管理连接。"""
    if not config.getoption("--mysql-integration"):
        return
    required = (
        "BFL_TEST_MYSQL_ADMIN_URL",
        "BFL_TEST_MYSQL_EXPECTED_SERVER_UUID",
        "BFL_TEST_MYSQL_ALLOW_DATABASE_DDL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise pytest.UsageError(
            "--mysql-integration requires explicit disposable MySQL guards: "
            + ", ".join(missing)
        )
    if os.environ["BFL_TEST_MYSQL_ALLOW_DATABASE_DDL"] != "1":
        raise pytest.UsageError(
            "BFL_TEST_MYSQL_ALLOW_DATABASE_DDL must equal 1"
        )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """公共单测默认跳过需要真实 MySQL 的独立测试层。"""
    if config.getoption("--mysql-integration"):
        return
    skipped = pytest.mark.skip(
        reason="enable with --mysql-integration and disposable MySQL guards"
    )
    for item in items:
        if "mysql_integration" in item.keywords:
            item.add_marker(skipped)
