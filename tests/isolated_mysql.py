"""迁移回归测试使用的隔离 MySQL 生命周期。"""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

MYSQLD = Path("/usr/local/mysql/bin/mysqld")
MYSQLADMIN = Path("/usr/local/mysql/bin/mysqladmin")
MYSQL_BASEDIR = Path("/usr/local/mysql")
DEFAULT_ROOT_PARENT = Path("/private/tmp")
ROOT_PREFIX = "bfl-real-replay-mysql-"
SCHEMA_PREFIX = "bfl_real_replay_"
_ROOT_NAME_PATTERN = re.compile(
    rf"{re.escape(ROOT_PREFIX)}[0-9a-f]{{20}}"
)
_SCHEMA_NAME_PATTERN = re.compile(
    rf"{re.escape(SCHEMA_PREFIX)}[0-9a-f]{{20}}"
)
_START_TIMEOUT_SECONDS = 30
_INITIALIZE_TIMEOUT_SECONDS = 45
_SHUTDOWN_TIMEOUT_SECONDS = 15
_FORCED_COLD_CACHE_DIRNAME = "liwei-phase-a-cache"


class IsolatedReplayMySQLError(RuntimeError):
    """临时 MySQL 生命周期的稳定、脱敏错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _ReplayMySQLPaths:
    """单次临时 MySQL 的精确私有路径。"""

    root: Path
    datadir: Path
    tmpdir: Path
    secure_file_dir: Path
    socket_path: Path
    pid_path: Path
    error_log: Path
    bootstrap_sql: Path
    client_config: Path

    @classmethod
    def from_root(cls, root: Path) -> _ReplayMySQLPaths:
        normalized = Path(root)
        return cls(
            root=normalized,
            datadir=normalized / "data",
            tmpdir=normalized / "tmp",
            secure_file_dir=normalized / "secure",
            socket_path=normalized / "mysql.sock",
            pid_path=normalized / "mysqld.pid",
            error_log=normalized / "mysqld.err",
            bootstrap_sql=normalized / "bootstrap.sql",
            client_config=normalized / "root-client.cnf",
        )


@dataclass(frozen=True)
class IsolatedReplayMySQLIdentity:
    """已经与 datadir、端口、socket 交叉验证的服务身份。"""

    version: str
    server_uuid: str
    port: int
    socket_path: Path
    datadir: Path


def _build_server_command(
    *,
    paths: _ReplayMySQLPaths,
    port: int,
) -> tuple[str, ...]:
    """构造不读取全局配置、仅 loopback 的固定 mysqld 参数。"""
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or port == 3306
    ):
        raise IsolatedReplayMySQLError("MYSQL_PORT_UNSAFE")
    return (
        os.fspath(MYSQLD),
        "--no-defaults",
        f"--basedir={MYSQL_BASEDIR}",
        f"--datadir={paths.datadir}",
        "--bind-address=127.0.0.1",
        f"--port={port}",
        f"--socket={paths.socket_path}",
        f"--pid-file={paths.pid_path}",
        f"--log-error={paths.error_log}",
        f"--tmpdir={paths.tmpdir}",
        f"--secure-file-priv={paths.secure_file_dir}",
        f"--init-file={paths.bootstrap_sql}",
        "--skip-name-resolve",
        "--skip-log-bin",
        "--mysqlx=OFF",
        "--local-infile=OFF",
        "--skip-symbolic-links",
        "--default-time-zone=+00:00",
        "--default-storage-engine=InnoDB",
        "--transaction-isolation=REPEATABLE-READ",
        "--character-set-server=utf8mb4",
        "--collation-server=utf8mb4_0900_ai_ci",
        "--innodb-buffer-pool-size=256M",
        "--log-timestamps=UTC",
        (
            "--sql-mode=STRICT_TRANS_TABLES,"
            "NO_ZERO_DATE,NO_ZERO_IN_DATE,"
            "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
        ),
    )


def _remove_verified_root(
    root: Path,
    *,
    root_parent: Path,
    expected_identity: tuple[int, int],
    process: subprocess.Popen[bytes] | None,
) -> None:
    """只在进程已退出且目录身份未变化时精确删除一次 root。"""
    if process is not None and process.poll() is None:
        raise IsolatedReplayMySQLError("MYSQL_CLEANUP_INCOMPLETE")
    candidate = Path(root)
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        parent_details = root_parent.lstat()
        valid_parent = (
            root_parent.is_absolute()
            and stat.S_ISDIR(parent_details.st_mode)
            and not stat.S_ISLNK(parent_details.st_mode)
        )
        valid_candidate = (
            candidate.is_absolute()
            and candidate.parent == root_parent
            and _ROOT_NAME_PATTERN.fullmatch(candidate.name) is not None
        )
        if not valid_parent or not valid_candidate:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_UNSAFE"
            )
        parent_flags = os.O_RDONLY
        parent_flags |= getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(root_parent, parent_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            opened_parent.st_dev != parent_details.st_dev
            or opened_parent.st_ino != parent_details.st_ino
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_UNSAFE"
            )
        details = os.stat(
            candidate.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        valid_root = (
            stat.S_ISDIR(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and details.st_uid == os.getuid()
            and stat.S_IMODE(details.st_mode) == 0o700
            and (details.st_dev, details.st_ino) == expected_identity
        )
        if not valid_root:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_UNSAFE"
            )
        root_flags = os.O_RDONLY
        root_flags |= getattr(os, "O_CLOEXEC", 0)
        root_flags |= getattr(os, "O_DIRECTORY", 0)
        root_flags |= getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(
            candidate.name,
            root_flags,
            dir_fd=parent_fd,
        )
        opened_root = os.fstat(root_fd)
        if (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != expected_identity:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_UNSAFE"
            )
        _clear_directory_fd(root_fd)
        current = os.stat(
            candidate.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            current.st_dev,
            current.st_ino,
        ) != expected_identity:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_UNSAFE"
            )
        os.rmdir(candidate.name, dir_fd=parent_fd)
    except IsolatedReplayMySQLError:
        raise
    except Exception:
        raise IsolatedReplayMySQLError(
            "MYSQL_CLEANUP_INCOMPLETE"
        ) from None
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _clear_directory_fd(directory_fd: int) -> None:
    """只通过已打开目录 fd 清理其内容，拒绝子目录替换。"""
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    for name in os.listdir(directory_fd):
        details = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(details.st_mode):
            child_fd = os.open(
                name,
                directory_flags,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev != details.st_dev
                    or opened.st_ino != details.st_ino
                ):
                    raise IsolatedReplayMySQLError(
                        "MYSQL_CLEANUP_UNSAFE"
                    )
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            current = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                current.st_dev != details.st_dev
                or current.st_ino != details.st_ino
            ):
                raise IsolatedReplayMySQLError(
                    "MYSQL_CLEANUP_UNSAFE"
                )
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


class IsolatedReplayMySQL:
    """启动、验证并精确回收一个本机隔离 MySQL 8.0.45。"""

    def __init__(
        self,
        *,
        root_parent: Path = DEFAULT_ROOT_PARENT,
    ) -> None:
        candidate = Path(root_parent)
        if not candidate.is_absolute():
            raise IsolatedReplayMySQLError(
                "MYSQL_ROOT_PARENT_UNSAFE"
            )
        try:
            resolved = candidate.resolve(strict=True)
            details = candidate.lstat()
        except OSError:
            raise IsolatedReplayMySQLError(
                "MYSQL_ROOT_PARENT_UNSAFE"
            ) from None
        if (
            candidate != resolved
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_ROOT_PARENT_UNSAFE"
            )
        if resolved != DEFAULT_ROOT_PARENT:
            if (
                details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o700
            ):
                raise IsolatedReplayMySQLError(
                    "MYSQL_ROOT_PARENT_UNSAFE"
                )
        self._root_parent = resolved
        self._paths: _ReplayMySQLPaths | None = None
        self._root_identity: tuple[int, int] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._root_password: str | None = None
        self._expected_server_uuid: str | None = None
        self._identity: IsolatedReplayMySQLIdentity | None = None
        self._engines: list[Engine] = []

    @property
    def root(self) -> Path:
        if self._paths is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        return self._paths.root

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def port(self) -> int:
        if self._port is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        return self._port

    @property
    def expected_server_uuid(self) -> str:
        if self._expected_server_uuid is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        return self._expected_server_uuid

    @property
    def identity(self) -> IsolatedReplayMySQLIdentity:
        if self._identity is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        return self._identity


    def create_forced_cold_cache_root(self) -> Path:
        """在本次隔离 root 内创建唯一、初始为空的 Liwei cache。"""
        if self._paths is None or self._root_identity is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        root = self._paths.root
        try:
            root_details = root.lstat()
        except OSError:
            raise IsolatedReplayMySQLError(
                "MYSQL_FORCED_COLD_CACHE_UNSAFE"
            ) from None
        if (
            not stat.S_ISDIR(root_details.st_mode)
            or stat.S_ISLNK(root_details.st_mode)
            or root_details.st_uid != os.getuid()
            or stat.S_IMODE(root_details.st_mode) != 0o700
            or (
                root_details.st_dev,
                root_details.st_ino,
            )
            != self._root_identity
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_FORCED_COLD_CACHE_UNSAFE"
            )
        cache_root = root / _FORCED_COLD_CACHE_DIRNAME
        try:
            cache_root.mkdir(mode=0o700)
            details = cache_root.lstat()
            valid = (
                stat.S_ISDIR(details.st_mode)
                and not stat.S_ISLNK(details.st_mode)
                and details.st_uid == os.getuid()
                and stat.S_IMODE(details.st_mode) == 0o700
                and not any(cache_root.iterdir())
            )
        except (FileExistsError, OSError):
            valid = False
        if not valid:
            raise IsolatedReplayMySQLError(
                "MYSQL_FORCED_COLD_CACHE_UNSAFE"
            )
        return cache_root

    def _start(self) -> IsolatedReplayMySQL:
        """创建私有 datadir，启动并交叉验证临时服务。"""
        if self._paths is not None:
            raise IsolatedReplayMySQLError("MYSQL_ALREADY_STARTED")
        self._validate_binaries()
        self._port = _reserve_loopback_port()
        root = self._create_root()
        self._paths = _ReplayMySQLPaths.from_root(root)
        details = root.lstat()
        self._root_identity = (details.st_dev, details.st_ino)
        try:
            self._prepare_filesystem()
            self._initialize_datadir()
            self._read_expected_server_uuid()
            self._write_private_credentials()
            self._process = subprocess.Popen(
                _build_server_command(
                    paths=self._paths,
                    port=self.port,
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=False,
            )
            self._wait_until_ready()
            self._paths.bootstrap_sql.unlink(missing_ok=True)
            self._assert_empty_root_password_rejected()
            self._identity = self._read_verified_identity()
            return self
        except BaseException as start_error:
            try:
                self._close()
            except IsolatedReplayMySQLError as cleanup_error:
                raise cleanup_error from start_error
            raise

    def create_replay_database(self) -> tuple[str, Engine]:
        """在隔离服务创建唯一 replay schema 和专用本地账号。"""
        if self._identity is None:
            raise IsolatedReplayMySQLError("MYSQL_NOT_STARTED")
        if self._database_isolation is not None:
            raise IsolatedReplayMySQLError(
                "MYSQL_DATABASE_ALREADY_CREATED"
            )
        suffix = uuid.uuid4().hex[:20]
        schema = f"{SCHEMA_PREFIX}{suffix}"
        if (
            schema == "bond_db"
            or _SCHEMA_NAME_PATTERN.fullmatch(schema) is None
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_SCHEMA_IDENTITY_UNSAFE"
            )
        username = f"bfl_rr_{suffix}"
        password = uuid.uuid4().hex + uuid.uuid4().hex
        admin = self._admin_engine()
        failure_code: str | None = None
        try:
            with admin.begin() as connection:
                bond_db_count = int(
                    connection.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM information_schema.schemata
                            WHERE schema_name = 'bond_db'
                            """
                        )
                    ).scalar_one()
                )
                if bond_db_count:
                    raise IsolatedReplayMySQLError(
                        "MYSQL_PRODUCTION_SCHEMA_PRESENT"
                    )
                connection.exec_driver_sql(
                    f"CREATE DATABASE `{schema}` "
                    "CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_0900_ai_ci"
                )
                connection.exec_driver_sql(
                    f"CREATE USER '{username}'@'127.0.0.1' "
                    "IDENTIFIED BY %s",
                    (password,),
                )
                connection.exec_driver_sql(
                    f"GRANT ALL PRIVILEGES ON `{schema}`.* "
                    f"TO '{username}'@'127.0.0.1'"
                )
        except IsolatedReplayMySQLError as exc:
            failure_code = exc.code
        except Exception:
            failure_code = "MYSQL_DATABASE_CREATE_FAILED"
        finally:
            try:
                admin.dispose()
            except Exception:
                failure_code = "MYSQL_DATABASE_CREATE_FAILED"
        if failure_code is not None:
            raise IsolatedReplayMySQLError(failure_code)
        engine = _create_tcp_engine(
            username=username,
            password=password,
            port=self.port,
            database=schema,
        )
        try:
            self._verify_replay_engine(engine, schema=schema)
        except BaseException:
            engine.dispose()
            raise
        self._engines.append(engine)
        return schema, engine

    def _close(self) -> None:
        """按 Engine→mysqld→精确 root 的顺序回收资源。"""
        cleanup_failed = False
        for engine in reversed(self._engines):
            try:
                engine.dispose()
            except Exception:
                cleanup_failed = True
        self._engines.clear()
        process = self._process
        if process is not None:
            try:
                self._stop_process(process)
            except Exception:
                cleanup_failed = True
        if self._paths is None:
            if cleanup_failed:
                raise IsolatedReplayMySQLError(
                    "MYSQL_CLEANUP_INCOMPLETE"
                )
            return
        if self._root_identity is None:
            raise IsolatedReplayMySQLError("MYSQL_CLEANUP_UNSAFE")
        removed = False
        if process is None or process.poll() is not None:
            try:
                _remove_verified_root(
                    self._paths.root,
                    root_parent=self._root_parent,
                    expected_identity=self._root_identity,
                    process=process,
                )
                removed = True
            except Exception:
                cleanup_failed = True
        else:
            cleanup_failed = True
        if removed:
            self._process = None
            self._paths = None
            self._root_identity = None
            self._identity = None
        if cleanup_failed:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_INCOMPLETE"
            )

    def __enter__(self) -> IsolatedReplayMySQL:
        return self._start()

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self._close()

    def _create_root(self) -> Path:
        for _attempt in range(10):
            root = self._root_parent / (
                ROOT_PREFIX + uuid.uuid4().hex[:20]
            )
            try:
                root.mkdir(mode=0o700)
            except FileExistsError:
                continue
            details = root.lstat()
            if (
                stat.S_ISDIR(details.st_mode)
                and not stat.S_ISLNK(details.st_mode)
                and details.st_uid == os.getuid()
                and stat.S_IMODE(details.st_mode) == 0o700
            ):
                return root
            raise IsolatedReplayMySQLError("MYSQL_ROOT_UNSAFE")
        raise IsolatedReplayMySQLError("MYSQL_ROOT_UNAVAILABLE")

    def _prepare_filesystem(self) -> None:
        assert self._paths is not None
        self._paths.datadir.mkdir(mode=0o700)
        self._paths.tmpdir.mkdir(mode=0o700)
        self._paths.secure_file_dir.mkdir(mode=0o700)

    def _initialize_datadir(self) -> None:
        assert self._paths is not None
        try:
            result = subprocess.run(
                (
                    os.fspath(MYSQLD),
                    "--no-defaults",
                    f"--basedir={MYSQL_BASEDIR}",
                    f"--datadir={self._paths.datadir}",
                    "--initialize-insecure",
                ),
                check=False,
                capture_output=True,
                timeout=_INITIALIZE_TIMEOUT_SECONDS,
            )
        except Exception:
            raise IsolatedReplayMySQLError(
                "MYSQL_INITIALIZE_FAILED"
            ) from None
        if result.returncode != 0:
            raise IsolatedReplayMySQLError(
                "MYSQL_INITIALIZE_FAILED"
            )

    def _read_expected_server_uuid(self) -> None:
        assert self._paths is not None
        try:
            auto_config = (
                self._paths.datadir / "auto.cnf"
            ).read_text(encoding="utf-8")
        except OSError:
            raise IsolatedReplayMySQLError(
                "MYSQL_SERVER_UUID_UNAVAILABLE"
            ) from None
        matched = re.search(
            r"(?m)^server-uuid=([0-9a-f-]{36})$",
            auto_config,
        )
        if matched is None:
            raise IsolatedReplayMySQLError(
                "MYSQL_SERVER_UUID_UNAVAILABLE"
            )
        self._expected_server_uuid = matched.group(1)

    def _write_private_credentials(self) -> None:
        assert self._paths is not None
        self._root_password = (
            uuid.uuid4().hex + uuid.uuid4().hex
        )
        _write_exclusive_private_file(
            self._paths.bootstrap_sql,
            (
                "ALTER USER 'root'@'localhost' IDENTIFIED BY "
                f"'{self._root_password}';\n"
            ).encode("utf-8"),
        )
        _write_exclusive_private_file(
            self._paths.client_config,
            (
                "[client]\n"
                "user=root\n"
                f"password={self._root_password}\n"
                f"socket={self._paths.socket_path}\n"
            ).encode("utf-8"),
        )

    def _wait_until_ready(self) -> None:
        assert self._paths is not None
        assert self._process is not None
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise IsolatedReplayMySQLError(
                    "MYSQL_START_FAILED"
                )
            try:
                result = subprocess.run(
                    (
                        os.fspath(MYSQLADMIN),
                        (
                            "--defaults-extra-file="
                            f"{self._paths.client_config}"
                        ),
                        "ping",
                        "--silent",
                    ),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                return
            time.sleep(0.1)
        raise IsolatedReplayMySQLError("MYSQL_START_TIMEOUT")

    def _admin_engine(self) -> Engine:
        assert self._paths is not None
        assert self._root_password is not None
        url = URL.create(
            drivername="mysql+pymysql",
            username="root",
            password=self._root_password,
            host="localhost",
            query={
                "charset": "utf8mb4",
                "unix_socket": os.fspath(self._paths.socket_path),
            },
        )
        return create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "init_command": "SET SESSION time_zone = '+00:00'",
            },
        )

    def _assert_empty_root_password_rejected(self) -> None:
        assert self._paths is not None
        url = URL.create(
            drivername="mysql+pymysql",
            username="root",
            password="",
            host="localhost",
            query={
                "charset": "utf8mb4",
                "unix_socket": os.fspath(self._paths.socket_path),
            },
        )
        engine = create_engine(
            url,
            future=True,
            hide_parameters=True,
        )
        try:
            try:
                with engine.connect():
                    pass
            except OperationalError:
                return
            raise IsolatedReplayMySQLError(
                "MYSQL_EMPTY_ROOT_PASSWORD"
            )
        finally:
            engine.dispose()

    def _read_verified_identity(
        self,
    ) -> IsolatedReplayMySQLIdentity:
        assert self._paths is not None
        admin = self._admin_engine()
        try:
            with admin.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT VERSION() AS version,
                               @@server_uuid AS server_uuid,
                               @@port AS server_port,
                               @@socket AS socket_path,
                               @@datadir AS datadir,
                               @@bind_address AS bind_address,
                               @@global.log_bin AS log_bin,
                               @@global.local_infile AS local_infile,
                               @@session.time_zone AS session_time_zone,
                               @@default_storage_engine AS storage_engine,
                               @@session.transaction_isolation
                                   AS isolation_level,
                               @@session.sql_mode AS sql_mode,
                               @@foreign_key_checks AS foreign_key_checks,
                               @@secure_file_priv AS secure_file_priv,
                               DATABASE() AS database_name
                        """
                    )
                ).mappings().one()
        finally:
            admin.dispose()
        sql_modes = {
            value.strip().upper()
            for value in str(row["sql_mode"]).split(",")
            if value.strip()
        }
        valid = (
            str(row["version"]).startswith("8.0.45")
            and str(row["server_uuid"])
            == self.expected_server_uuid
            and int(row["server_port"]) == self.port
            and self.port != 3306
            and Path(str(row["socket_path"]))
            == self._paths.socket_path
            and Path(str(row["datadir"])).resolve()
            == self._paths.datadir.resolve()
            and str(row["bind_address"]) == "127.0.0.1"
            and int(row["log_bin"]) == 0
            and int(row["local_infile"]) == 0
            and str(row["session_time_zone"]) == "+00:00"
            and str(row["storage_engine"]).casefold() == "innodb"
            and str(row["isolation_level"]).upper()
            == "REPEATABLE-READ"
            and bool(
                sql_modes
                & {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"}
            )
            and int(row["foreign_key_checks"]) == 1
            and Path(str(row["secure_file_priv"])).resolve()
            == self._paths.secure_file_dir.resolve()
            and row["database_name"] is None
        )
        if not valid:
            raise IsolatedReplayMySQLError(
                "MYSQL_IDENTITY_DRIFT"
            )
        return IsolatedReplayMySQLIdentity(
            version=str(row["version"]),
            server_uuid=str(row["server_uuid"]),
            port=int(row["server_port"]),
            socket_path=self._paths.socket_path,
            datadir=self._paths.datadir,
        )

    def _verify_replay_engine(
        self,
        engine: Engine,
        *,
        schema: str,
    ) -> None:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT DATABASE() AS database_name,
                           @@server_uuid AS server_uuid,
                           @@port AS server_port,
                           CURRENT_USER() AS authenticated_principal
                    """
                )
            ).mappings().one()
        if (
            str(row["database_name"]) != schema
            or _SCHEMA_NAME_PATTERN.fullmatch(schema) is None
            or str(row["server_uuid"])
            != self.identity.server_uuid
            or int(row["server_port"]) != self.port
            or not str(row["authenticated_principal"]).startswith(
                "bfl_rr_"
            )
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_SCHEMA_IDENTITY_DRIFT"
            )

    def _stop_process(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        assert self._paths is not None
        if process.poll() is None:
            try:
                subprocess.run(
                    (
                        os.fspath(MYSQLADMIN),
                        (
                            "--defaults-extra-file="
                            f"{self._paths.client_config}"
                        ),
                        "shutdown",
                    ),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                pass
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    raise IsolatedReplayMySQLError(
                        "MYSQL_CLEANUP_INCOMPLETE"
                    ) from None
        if process.poll() is None:
            raise IsolatedReplayMySQLError(
                "MYSQL_CLEANUP_INCOMPLETE"
            )

    @staticmethod
    def _validate_binaries() -> None:
        for binary in (MYSQLD, MYSQLADMIN):
            if (
                not binary.is_absolute()
                or not binary.is_file()
                or binary.is_symlink()
                or not os.access(binary, os.X_OK)
            ):
                raise IsolatedReplayMySQLError(
                    "MYSQL_BINARY_UNAVAILABLE"
                )


@contextmanager
def isolated_replay_mysql(
    *,
    root_parent: Path = DEFAULT_ROOT_PARENT,
) -> Iterator[IsolatedReplayMySQL]:
    """提供一个异常安全、精确回收的隔离 MySQL 生命周期。"""
    with IsolatedReplayMySQL(root_parent=root_parent) as server:
        yield server


def _reserve_loopback_port() -> int:
    for _attempt in range(10):
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        ) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port != 3306:
            return port
    raise IsolatedReplayMySQLError("MYSQL_PORT_UNAVAILABLE")


def _write_exclusive_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise IsolatedReplayMySQLError(
            "MYSQL_PRIVATE_FILE_UNSAFE"
        ) from None
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise IsolatedReplayMySQLError(
                "MYSQL_PRIVATE_FILE_UNSAFE"
            )
    finally:
        os.close(descriptor)


def _create_tcp_engine(
    *,
    username: str,
    password: str,
    port: int,
    database: str,
) -> Engine:
    url = URL.create(
        drivername="mysql+pymysql",
        username=username,
        password=password,
        host="127.0.0.1",
        port=port,
        database=database,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "init_command": "SET SESSION time_zone = '+00:00'",
        },
    )
