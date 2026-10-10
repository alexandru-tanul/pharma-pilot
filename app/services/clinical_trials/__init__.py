"""Clinical Trials (AACT) service — single tool entry point for the LLM."""

from app.services.clinical_trials.query import (
    execute_query,
    format_results_for_llm,
    get_schema_summary,
    ClinicalTrialsQueryError,
)
from app.services.sql_errors import build_sql_error_message


async def query_clinical_trials(sql: str, max_result_chars: int = 50_000) -> str:
    """Execute a SQL query against the AACT clinical trials database and return formatted results.

    This is the function called by the LLM tool dispatcher.
    The LLM generates SQL based on the schema in get_schema_summary().

    Args:
        sql: A SELECT query targeting AACT views/tables.
        max_result_chars: Maximum characters in the formatted output.

    Returns:
        Formatted text with query results (or error message).
    """
    try:
        result = await execute_query(sql)
        return format_results_for_llm(result, max_chars=max_result_chars)
    except ClinicalTrialsQueryError as e:
        return await build_sql_error_message(e, sql, "Clinical Trials")
    except Exception as e:
        return await build_sql_error_message(e, sql, "Clinical Trials")
