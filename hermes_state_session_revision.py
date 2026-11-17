"""Validate the upstream session-list trigger contract for writers and readers."""
import sqlite3

from hermes_state_common import SESSIONS_LIST_REVISION_SQL


def session_list_revision_triggers_match(conn: sqlite3.Connection | sqlite3.Cursor) -> bool:
    """Foreign or partially installed triggers cannot authorize a constant-time revision read."""
    expected = {}
    statement = ""
    for line in SESSIONS_LIST_REVISION_SQL.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            ddl = statement.strip().replace("IF NOT EXISTS ", "", 1).rstrip(";")
            if ddl.startswith("CREATE TRIGGER "):
                expected[ddl.split()[2]] = ddl
            statement = ""
    installed = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'sessions'"
    ))
    return all(installed.get(name) == ddl for name, ddl in expected.items())
