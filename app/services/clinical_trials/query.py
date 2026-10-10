"""Safe read-only SQL executor for the Clinical Trials (AACT) database (DuckDB over Parquet)."""

import logging
import re

from app import duckdb as duckdb_manager

log = logging.getLogger(__name__)

# Safety constants
MAX_ROWS = 200
_ALLOWED = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|VACUUM|REINDEX|TRUNCATE)\b",
    re.IGNORECASE,
)


class ClinicalTrialsQueryError(Exception):
    pass


def _validate_query(sql: str) -> str:
    """Validate that a query is a read-only SELECT. Returns cleaned SQL."""
    sql = sql.strip().rstrip(";")

    if not _ALLOWED.match(sql):
        raise ClinicalTrialsQueryError("Only SELECT queries are allowed.")

    if _FORBIDDEN.search(sql):
        raise ClinicalTrialsQueryError("Query contains forbidden keywords.")

    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"

    return sql


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query against the clinical trials database (DuckDB).

    Returns dict with keys: columns, rows, row_count, truncated, query.
    """
    sql = _validate_query(sql)

    try:
        result = await duckdb_manager.execute_query_raw(sql)

        rows = [list(row) for row in result["rows"]]
        truncated = len(rows) > MAX_ROWS
        if truncated:
            rows = rows[:MAX_ROWS]

        return {
            "columns": result["columns"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "query": sql,
        }
    except ClinicalTrialsQueryError:
        raise
    except Exception as e:
        raise ClinicalTrialsQueryError(f"SQL error: {e}")


def format_results_for_llm(result: dict, max_chars: int = 50_000) -> str:
    """Format query results as readable text for the LLM.

    Builds the output row by row. If the running character count exceeds
    *max_chars*, remaining rows are dropped and a note is appended.
    Individual cell values are never truncated.
    """
    sql_line = f"SQL: {result['query']}"

    if not result["rows"]:
        return f"Query returned 0 rows.\n{sql_line}"

    total_rows = result["row_count"]

    # Build header
    header = " | ".join(result["columns"])
    separator = "-" * min(len(header), 120)
    # Reserve space for the header + sql line + a generous footer
    running = len(header) + len(separator) + len(sql_line) + 200

    # Build data rows incrementally
    data_lines: list[str] = []
    for row in result["rows"]:
        cells = [str(v) if v is not None else "NULL" for v in row]
        line = " | ".join(cells)
        if running + len(line) + 1 > max_chars and data_lines:
            break
        data_lines.append(line)
        running += len(line) + 1

    included = len(data_lines)
    was_capped = included < total_rows

    # Assemble output
    count_line = (
        f"Results: {included} of {total_rows} rows"
        if was_capped
        else f"Results: {total_rows} rows"
    )
    if result["truncated"]:
        count_line += " (query had more rows, capped at 200)"

    lines = [count_line, sql_line, "", header, separator] + data_lines

    if was_capped:
        lines.append(
            f"\n[Showing {included} of {total_rows} rows. "
            f"Refine your query with more specific filters or LIMIT "
            f"to get targeted results.]"
        )

    return "\n".join(lines)


# ── Schema description for the LLM system prompt ────────────────────────────

CLINICAL_TRIALS_SCHEMA = """
## Clinical Trials Database (local, read-only)

You have access to the AACT database (Aggregate Analysis of ClinicalTrials.gov)
containing 580,000+ clinical studies. Use the query_clinical_trials tool with SQL SELECT queries.

### Views:

1. v_studies(nct_id, title, phase, overall_status, enrollment, study_type,
             start_date, completion_date, results_first_posted_date)
   All clinical studies. Filter by phase or status (see exact values in SQL Tips below).

2. v_study_drugs(nct_id, drug_name, intervention_type, description)
   Drug interventions (DRUG, BIOLOGICAL, COMBINATION_PRODUCT only).
   Use LOWER(drug_name) LIKE '%term%' for searching.

3. v_study_conditions(nct_id, condition_name, mesh_term, mesh_type)
   Conditions/diseases in studies with MeSH mappings.
   mesh_type: 'mesh-list' (leaf term) or 'mesh-ancestor' (parent category).
   Use LOWER(condition_name) LIKE '%term%' or LOWER(mesh_term) LIKE '%term%'.

4. v_drug_trials(nct_id, title, phase, overall_status, drug_name, enrollment)
   Studies joined with drug interventions. Has drug_name but NO condition_name or mesh_term.
   To filter by condition, use v_drug_condition_trials instead.
   Example: SELECT * FROM v_drug_trials WHERE LOWER(drug_name) LIKE '%metformin%' LIMIT 50

5. v_condition_trials(nct_id, title, phase, overall_status, condition_name,
                      mesh_term, enrollment)
   Studies joined with conditions + MeSH. Has condition_name and mesh_term but NO drug_name.
   To filter by drug, use v_drug_condition_trials instead.
   Example: SELECT * FROM v_condition_trials WHERE LOWER(mesh_term) LIKE '%breast neoplasms%'

6. v_drug_condition_trials(nct_id, title, phase, overall_status, drug_name,
                           condition_name, mesh_term, enrollment)
   The ONLY view with both drug_name AND condition_name/mesh_term. Use this when filtering by both.
   Example: SELECT * FROM v_drug_condition_trials
            WHERE LOWER(drug_name) LIKE '%pembrolizumab%'
            AND LOWER(condition_name) LIKE '%melanoma%' LIMIT 50

7. v_trial_summary(nct_id, title, phase, overall_status, enrollment, study_type,
                   start_date, completion_date, sponsor_name, sponsor_class)
   Study details with lead sponsor info. sponsor_class: 'INDUSTRY', 'NIH', 'FED',
   'OTHER', 'OTHER_GOV', 'NETWORK', 'AMBIG', 'INDIV', 'UNKNOWN'.

8. v_trial_outcomes(nct_id, outcome_type, title, description, time_frame, population)
   Primary and secondary outcome endpoint definitions.
   outcome_type: 'PRIMARY', 'SECONDARY', 'OTHER_PRE_SPECIFIED', 'POST_HOC'.
   Example: SELECT * FROM v_trial_outcomes WHERE nct_id = 'NCT04098666' AND outcome_type = 'PRIMARY'

9. v_trial_results(nct_id, outcome_title, outcome_type, stat_method, param_type, param_value,
                   p_value, p_value_modifier, ci_percent, ci_lower_limit, ci_upper_limit,
                   estimate_description, groups_description)
   Statistical results of outcome analyses — p-values, confidence intervals, effect sizes.
   Only studies that have posted results will have rows here.
   Example: SELECT * FROM v_trial_results WHERE nct_id = 'NCT04098666' LIMIT 20

10. v_trial_eligibility(nct_id, gender, minimum_age, maximum_age, healthy_volunteers, criteria)
    Inclusion/exclusion criteria. criteria contains the full text (can be long).
    Example: SELECT minimum_age, maximum_age, criteria FROM v_trial_eligibility
             WHERE nct_id = 'NCT04098666'

11. v_trial_arms(nct_id, group_type, title, description)
    Treatment arms and comparator groups.
    group_type: 'EXPERIMENTAL', 'ACTIVE_COMPARATOR', 'PLACEBO_COMPARATOR',
    'SHAM_COMPARATOR', 'NO_INTERVENTION', 'OTHER'.

12. v_trial_measurements(nct_id, outcome_title, outcome_type, group_title,
                         classification, category, param_type, value, units,
                         dispersion_type, dispersion_value, dispersion_lower_limit,
                         dispersion_upper_limit)
    Actual numeric results per treatment arm. Use with v_trial_results for the complete picture.
    group_title identifies the arm (e.g. 'Olaparib', 'Placebo').
    Example: SELECT * FROM v_trial_measurements WHERE nct_id = 'NCT04098666' LIMIT 50

### SQL Tips:
- Phase values (UPPERCASE, no spaces): 'EARLY_PHASE1', 'PHASE1', 'PHASE1/PHASE2',
  'PHASE2', 'PHASE2/PHASE3', 'PHASE3', 'PHASE4', 'NA'
- Status values (UPPERCASE): 'RECRUITING', 'NOT_YET_RECRUITING', 'ACTIVE_NOT_RECRUITING',
  'COMPLETED', 'TERMINATED', 'WITHDRAWN', 'SUSPENDED', 'ENROLLING_BY_INVITATION',
  'AVAILABLE', 'NO_LONGER_AVAILABLE', 'APPROVED_FOR_MARKETING', 'UNKNOWN', 'WITHHELD'
- Use LOWER() LIKE for all text searches (names are mixed-case).
- MeSH terms provide standardized vocabulary — prefer them over free-text conditions.
- Always include LIMIT (max 200 rows).
- IMPORTANT: v_drug_trials has NO condition columns; v_condition_trials has NO drug columns.
  Use v_drug_condition_trials when you need both, or JOIN v_drug_trials with v_study_conditions.
- AVOID subqueries like WHERE nct_id IN (SELECT ...) — they often timeout on large tables.
  Instead, use the pre-joined views (v_drug_trials, v_condition_trials, v_drug_condition_trials).
- To count trials: SELECT drug_name, COUNT(*) as trial_count FROM v_drug_trials
  WHERE LOWER(drug_name) LIKE '%term%' GROUP BY drug_name ORDER BY trial_count DESC
""".strip()


def get_schema_summary() -> str:
    """Return the clinical trials schema description for injection into the system prompt."""
    return CLINICAL_TRIALS_SCHEMA
