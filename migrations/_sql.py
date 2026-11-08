"""迁移 SQL 的受限解析，不执行数据库操作。"""

from __future__ import annotations

from migrations._contracts import (
    MigrationSQLParseError,
)


def _error_location(sql: str, offset: int) -> str:
    """返回零偏移量对应的一基行列位置。"""
    line = sql.count("\n", 0, offset) + 1
    previous_newline = sql.rfind("\n", 0, offset)
    column = offset - previous_newline
    return f"line {line}, column {column}"


def _append_comment_separator(buffer: list[str]) -> None:
    """删除注释时保留 token 间隔，避免前后 SQL 单词粘连。"""
    if buffer and not buffer[-1].isspace():
        buffer.append(" ")


def _is_dash_comment_start(sql: str, offset: int) -> bool:
    """按 MySQL 语义识别要求后随空白的 ``--`` 行注释。"""
    if not sql.startswith("--", offset):
        return False
    following = offset + 2
    return following >= len(sql) or sql[following].isspace()


def _is_delimiter_directive(sql: str, offset: int) -> bool:
    """识别当前 statement 起点处不受支持的 DELIMITER 指令。"""
    keyword = "DELIMITER"
    if sql[offset : offset + len(keyword)].upper() != keyword:
        return False
    following = offset + len(keyword)
    return following >= len(sql) or sql[following].isspace()


def split_sql_statements(sql: str) -> list[str]:
    """安全拆分 MySQL migration statement。

    分号只有在引号、反引号和注释之外才是 statement 边界。普通注释会被
    移除，并在必要时保留 token 间隔。当前 migrations 不使用客户端
    ``DELIMITER`` 指令，因此遇到该指令或未闭合结构时直接 fail-closed。
    """
    statements: list[str] = []
    buffer: list[str] = []
    state = "normal"
    opened_at: int | None = None
    has_executable_content = False
    offset = 0

    def emit_statement() -> None:
        nonlocal has_executable_content
        statement = "".join(buffer).strip()
        if has_executable_content:
            if not statement:
                raise MigrationSQLParseError(
                    "statement contains no executable SQL"
                )
            statements.append(statement)
        buffer.clear()
        has_executable_content = False

    while offset < len(sql):
        char = sql[offset]

        if state == "line_comment":
            if char == "\n":
                buffer.append("\n")
                state = "normal"
                opened_at = None
            offset += 1
            continue

        if state == "block_comment":
            if sql.startswith("*/", offset):
                _append_comment_separator(buffer)
                state = "normal"
                opened_at = None
                offset += 2
            else:
                offset += 1
            continue

        if state in {"single_quote", "double_quote", "backtick"}:
            buffer.append(char)
            quote = {
                "single_quote": "'",
                "double_quote": '"',
                "backtick": "`",
            }[state]
            if char == "\\" and offset + 1 < len(sql):
                buffer.append(sql[offset + 1])
                offset += 2
                continue
            if char == quote:
                if offset + 1 < len(sql) and sql[offset + 1] == quote:
                    buffer.append(sql[offset + 1])
                    offset += 2
                    continue
                state = "normal"
                opened_at = None
            offset += 1
            continue

        if (
            not has_executable_content
            and char.isalpha()
            and _is_delimiter_directive(sql, offset)
        ):
            raise MigrationSQLParseError(
                "DELIMITER directives are not supported "
                f"({_error_location(sql, offset)})"
            )
        if _is_dash_comment_start(sql, offset):
            _append_comment_separator(buffer)
            state = "line_comment"
            opened_at = offset
            offset += 2
            continue
        if char == "#":
            _append_comment_separator(buffer)
            state = "line_comment"
            opened_at = offset
            offset += 1
            continue
        if sql.startswith("/*", offset):
            _append_comment_separator(buffer)
            state = "block_comment"
            opened_at = offset
            offset += 2
            continue
        if char in {"'", '"', "`"}:
            state = {
                "'": "single_quote",
                '"': "double_quote",
                "`": "backtick",
            }[char]
            opened_at = offset
            buffer.append(char)
            has_executable_content = True
            offset += 1
            continue
        if char == ";":
            emit_statement()
            offset += 1
            continue

        buffer.append(char)
        if not char.isspace():
            has_executable_content = True
        offset += 1

    if state == "line_comment":
        state = "normal"
        opened_at = None
    if state != "normal":
        assert opened_at is not None
        raise MigrationSQLParseError(
            f"unterminated {state.replace('_', ' ')} "
            f"({_error_location(sql, opened_at)})"
        )
    emit_statement()
    return statements
