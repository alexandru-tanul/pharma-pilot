"""Safe read-only SQL executor for DrugCentral database (DuckDB over Parquet)."""

import re

from app import duckdb as duckdb_manager

# Safety constants
MAX_ROWS = 200
_ALLOWED = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|VACUUM|REINDEX|TRUNCATE)\b",
    re.IGNORECASE,
)


class DrugCentralQueryError(Exception):
    pass


def _validate_query(sql: str) -> str:
    """Validate that a query is a read-only SELECT. Returns cleaned SQL."""
    sql = sql.strip().rstrip(";")

    if not _ALLOWED.match(sql):
        raise DrugCentralQueryError("Only SELECT queries are allowed.")

    if _FORBIDDEN.search(sql):
        raise DrugCentralQueryError("Query contains forbidden keywords.")

    # Enforce row limit if not already present
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"

    return sql


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query against DrugCentral (DuckDB).

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
    except DrugCentralQueryError:
        raise
    except Exception as e:
        raise DrugCentralQueryError(f"SQL error: {e}")


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


# ── Schema description for the LLM system prompt ─────────────────────────────

DRUGCENTRAL_SCHEMA = """
## DrugCentral Database (local, read-only)

You have access to a local copy of DrugCentral, a comprehensive pharmaceutical database.
Use the query_drugcentral tool with SQL SELECT queries against these views:

1. v_drug_info(drug_id, drug_name, cas_reg_no, molecular_formula, mol_weight, smiles, synonyms)
   Basic drug information. The synonyms column is comma-separated alternative names.

2. v_drug_indications(drug_name, drug_id, condition_name, relationship_type, umls_cui, snomed_conceptid)
   Drug-disease relationships. relationship_type is one of: 'indication', 'contraindication', 'off-label use', 'reduce risk'.

3. v_drug_targets(drug_name, drug_id, target_name, target_class, target_gene, uniprot_accession, organism, act_value, act_type, mechanism_of_action, moa_source)
   Drug targets with bioactivity data and mechanism of action.

4. v_disease_drugs(disease_name, relationship_name, drug_name, drug_id, umls_cui, snomed_conceptid)
   Lookup all drugs for a disease (indications + off-label use + risk reduction).

5. v_drug_classification(drug_name, drug_id, class_type, class_name, class_code, class_source)
   Pharmacological classification from MeSH, CHEBI, FDA, EPC, etc.

6. v_drug_interactions(drug_class_1, drug_class_2, ddi_risk, interaction_description)
   Drug-drug interaction data with risk levels and descriptions.

7. v_adverse_events(drug_name, drug_id, adverse_event, meddra_code, event_level, log_likelihood_ratio, llr_threshold)
   FDA FAERS adverse events (statistically significant only: LLR > threshold).

8. v_drug_approvals(drug_name, drug_id, approval_date, approval_type, applicant, orphan)
   Regulatory approval dates and types (FDA, EMA, Health Canada, etc.).

9. v_drug_properties(drug_name, drug_id, property_name, property_category, property_value, units)
   Chemical and physical properties. IMPORTANT: property_name values are database identifiers,
   not common abbreviations (e.g., 'ALOGPS_LOGP' not 'logP', 'Molecular Weight' not 'MW').
   TIP: Query SELECT DISTINCT property_name FROM v_drug_properties ORDER BY property_name
   to discover available names before filtering by specific properties.

10. v_drug_identifiers(drug_name, drug_id, database_name, external_id)
    Cross-references to DrugBank, ChEMBL, PubChem, UNII, etc.

11. v_drug_atc(drug_name, drug_id, atc_code, anatomical_group, therapeutic_subgroup, pharmacological_subgroup, chemical_subgroup, chemical_substance)
    WHO ATC therapeutic classification hierarchy.

### SQL Tips
- Drug names are case-sensitive. Use `LOWER(drug_name) LIKE '%aspirin%'` for flexible matching.
- Use v_drug_info to find drug_id first, then query other views by drug_id for efficiency.
- For disease lookups, use v_disease_drugs with `LOWER(disease_name) LIKE '%term%'`.
- Always include LIMIT (max 200 rows).
- You can query underlying tables directly too: structures, omop_relationship, act_table_full, target_dictionary, faers, pharma_class, atc, ddi, approval, identifier, property, etc.
""".strip()


def get_schema_summary() -> str:
    """Return the DrugCentral schema description for injection into the system prompt."""
    return DRUGCENTRAL_SCHEMA
