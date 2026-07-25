from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import pymysql

from tests.test_daily_native_coordinator_mysql import (
    _temporary_mysql,
)


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@unittest.skipUnless(
    os.environ.get("BFL_SOURCE_RUNNER_MYSQL") == "1",
    "set BFL_SOURCE_RUNNER_MYSQL=1 to run source runner DB isolation",
)
class SourceRunnerDatabaseIsolationMySQLTests(unittest.TestCase):
    def test_all_private_runtimes_replace_packaged_credentials_with_readonly_clone_binding(
        self,
    ) -> None:
        from shared.daily_0629_source_runner import (
            _source_runtime as daily_runtime,
        )
        from shared.monthly_source_runner import (
            _source_runtime as monthly_runtime,
        )
        from shared.weekly_average_lgbm_source_runner import (
            _source_runtime as weekly_runtime,
        )
        from shared.data_contract import (
            CALENDAR_SOURCE_TABLES,
            FACTOR_SOURCE_TABLES,
            METADATA_SOURCE_TABLE,
        )
        from shared.source_runtime_database import (
            SourceRuntimeDatabaseConfig,
            preflight_source_runtime_database_access,
        )

        with _temporary_mysql() as server:
            schema, engine = server.create_schema("source")
            required_tables = (
                *FACTOR_SOURCE_TABLES,
                METADATA_SOURCE_TABLE,
                *CALENDAR_SOURCE_TABLES,
            )
            reader_user = f"bfl_src_{uuid.uuid4().hex[:10]}"
            reader_password = (
                uuid.uuid4().hex + uuid.uuid4().hex
            )
            admin = server._socket_admin_engine()
            try:
                with admin.begin() as connection:
                    for table in required_tables:
                        connection.exec_driver_sql(
                            f"CREATE TABLE `{schema}`.`{table}` "
                            "(id INT PRIMARY KEY)"
                        )
                    connection.exec_driver_sql(
                        f"CREATE TABLE `{schema}`."
                        "`source_drop_must_fail` "
                        "(id INT PRIMARY KEY)"
                    )
                    connection.exec_driver_sql(
                        f"CREATE USER '{reader_user}'@'127.0.0.1' "
                        f"IDENTIFIED BY '{reader_password}'"
                    )
                    for table in required_tables:
                        connection.exec_driver_sql(
                            f"GRANT SELECT ON `{schema}`.`{table}` "
                            f"TO '{reader_user}'@'127.0.0.1'"
                        )
            finally:
                admin.dispose()

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                source_root = root / "forecast_project"
                source_root.mkdir()
                packaged_config = source_root / "db_config.py"
                packaged_config.write_text(
                    "DB_CONFIG = {\n"
                    '    "user": "packaged-invalid",\n'
                    '    "password": "packaged-invalid-secret",\n'
                    '    "host": "127.0.0.1",\n'
                    f'    "port": {server.port},\n'
                    f'    "database": "{schema}",\n'
                    '    "charset": "utf8mb4",\n'
                    "}\n",
                    encoding="utf-8",
                )
                probe = source_root / "probe.py"
                probe.write_text(
                    "import json\n"
                    "import pymysql\n"
                    "from db_config import DB_CONFIG\n"
                    f"required_tables = {required_tables!r}\n"
                    "connection = pymysql.connect(**DB_CONFIG)\n"
                    "try:\n"
                    "    with connection.cursor() as cursor:\n"
                    "        cursor.execute("
                    '"SELECT DATABASE(), @@server_uuid")\n'
                    "        database_name, server_uuid = cursor.fetchone()\n"
                    "        readable_tables = []\n"
                    "        for table in required_tables:\n"
                    "            cursor.execute("
                    "'SELECT * FROM `' + table + '` LIMIT 0')\n"
                    "            readable_tables.append(table)\n"
                    "        write_probes = {\n"
                    '            "insert": "INSERT INTO '
                    '`api_wind_daily` (id) VALUES (9001)",\n'
                    '            "update": "UPDATE '
                    '`api_wind_daily` SET id=id WHERE 1=0",\n'
                    '            "delete": "DELETE FROM '
                    '`api_wind_daily` WHERE 1=0",\n'
                    '            "create": "CREATE TABLE '
                    'source_create_must_fail (id INT)",\n'
                    '            "alter": "ALTER TABLE '
                    '`api_wind_daily` ADD COLUMN unsafe INT",\n'
                    '            "drop": "DROP TABLE '
                    'source_drop_must_fail",\n'
                    "        }\n"
                    "        rejected_writes = []\n"
                    "        for operation, statement in "
                    "write_probes.items():\n"
                    "            try:\n"
                    "                cursor.execute(statement)\n"
                    "            except Exception:\n"
                    "                rejected_writes.append(operation)\n"
                    "    print(json.dumps({\n"
                    '        "database": database_name,\n'
                    '        "server_uuid": server_uuid,\n'
                    '        "readable_tables": readable_tables,\n'
                    '        "rejected_writes": rejected_writes,\n'
                    "    }, sort_keys=True))\n"
                    "finally:\n"
                    "    connection.close()\n",
                    encoding="utf-8",
                )
                source_hash = _tree_sha256(source_root)
                original_config = packaged_config.read_bytes()
                binding_path = root / "source-db.json"
                binding_path.write_text(
                    json.dumps(
                        {
                            "version":
                                "source-runtime-database-v1",
                            "user": reader_user,
                            "password": reader_password,
                            "host": "127.0.0.1",
                            "port": server.port,
                            "database": schema,
                            "charset": "utf8mb4",
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                binding_path.chmod(0o600)
                database_config = SourceRuntimeDatabaseConfig(
                    user=reader_user,
                    password=reader_password,
                    host="127.0.0.1",
                    port=server.port,
                    database=schema,
                    charset="utf8mb4",
                    config_path=binding_path,
                )
                preflight = preflight_source_runtime_database_access(
                    database_config
                )
                self.assertEqual(
                    preflight.tables,
                    required_tables,
                )

                with self.assertRaises(pymysql.MySQLError):
                    pymysql.connect(
                        user="packaged-invalid",
                        password="packaged-invalid-secret",
                        host="127.0.0.1",
                        port=server.port,
                        database=schema,
                        charset="utf8mb4",
                        connect_timeout=2,
                    )

                environment = {
                    "BFL_SOURCE_DB_CONFIG_ROOT":
                        str(binding_path.parent.resolve()),
                    "BFL_SOURCE_DB_CONFIG_PATH":
                        str(binding_path.resolve()),
                }
                for runtime in (
                    daily_runtime,
                    monthly_runtime,
                    weekly_runtime,
                ):
                    with (
                        self.subTest(runtime=runtime.__module__),
                        patch.dict(
                            os.environ,
                            environment,
                            clear=True,
                        ),
                        runtime(source_root, source_hash) as runtime_root,
                    ):
                        completed = subprocess.run(
                            [sys.executable, "probe.py"],
                            cwd=runtime_root,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            check=False,
                            timeout=10,
                        )
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stderr,
                        )
                        payload = json.loads(completed.stdout)
                        self.assertEqual(payload["database"], schema)
                        self.assertEqual(
                            payload["server_uuid"],
                            server.server_uuid,
                        )
                        self.assertEqual(
                            tuple(payload["readable_tables"]),
                            required_tables,
                        )
                        self.assertEqual(
                            set(payload["rejected_writes"]),
                            {
                                "insert",
                                "update",
                                "delete",
                                "create",
                                "alter",
                                "drop",
                            },
                        )

                self.assertEqual(
                    packaged_config.read_bytes(),
                    original_config,
                )
                self.assertEqual(_tree_sha256(source_root), source_hash)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
