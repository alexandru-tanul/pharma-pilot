"""Shared helper for building self-correction SQL error messages.

When the LLM generates a SQL query that fails, this module produces a
structured error message containing the failed SQL, the error, and the
available columns on the table the query targeted.  This gives the LLM
enough context to fix its own mistake on the next iteration.
"""

import re

from app import duckdb as duckdb_manager

# Matches table/view names in FROM and JOIN clauses
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


def _extract_table_names(sql: str) -> list[str]:
    """Extract table/view names referenced in FROM and JOIN clauses."""
    return list(dict.fromkeys(_TABLE_RE.findall(sql)))


async def build_sql_error_message(
    error: Exception,
    sql: str,
    source_name: str,
) -> str:
    """Build a rich error message that helps the LLM self-correct.

    Args:
        error: The exception that was raised.
        sql: The SQL query that failed.
        source_name: Human-readable database name (e.g. "Clinical Trials").

    Returns:
        A formatted string containing the error, the failed SQL, available
        columns for each referenced table, and an instruction to retry.
    """
    lines = [
        f"SQL ERROR ({source_name}): {error}",
        f"Failed SQL: {sql}",
    ]

    # Try to list columns for every table/view referenced in the query
    tables = _extract_table_names(sql)
    for table in tables:
        columns = await duckdb_manager.get_table_columns(table)
        if columns:
            lines.append(f"Available columns in {table}: {', '.join(columns)}")

    lines.append("Please correct the query and try again.")
    return "\n".join(lines)
