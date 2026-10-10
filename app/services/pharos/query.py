"""Safe read-only SQL executor for Pharos/TCRD database (DuckDB over Parquet)."""

import re

from app import duckdb as duckdb_manager

# Safety constants
MAX_ROWS = 200
_ALLOWED = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|VACUUM|REINDEX|TRUNCATE)\b",
    re.IGNORECASE,
)


class PharosQueryError(Exception):
    pass


def _validate_query(sql: str) -> str:
    """Validate that a query is a read-only SELECT. Returns cleaned SQL."""
    sql = sql.strip().rstrip(";")

    if not _ALLOWED.match(sql):
        raise PharosQueryError("Only SELECT queries are allowed.")

    if _FORBIDDEN.search(sql):
        raise PharosQueryError("Query contains forbidden keywords.")

    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"

    return sql


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query against Pharos/TCRD (DuckDB).

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
    except PharosQueryError:
        raise
    except Exception as e:
        raise PharosQueryError(f"SQL error: {e}")


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


# ── Schema description for LLM system prompt ────────────────────────────────

PHAROS_SCHEMA = """
## Pharos / TCRD Database (local, read-only)

~20,000 human protein targets with druggability classification, disease associations, and ligand bioactivity.
Use the query_pharos tool with SQL SELECT queries against these tables:

### Core Tables

1. **protein** (20,412 rows) — Human proteins
   - id (INTEGER), sym (VARCHAR, gene symbol e.g. 'EGFR'), name (VARCHAR, UniProt name e.g. 'EGFR_HUMAN')
   - description (VARCHAR), uniprot (VARCHAR, accession e.g. 'P00533'), geneid (INTEGER)
   - family (VARCHAR, full family description), preferred_symbol (VARCHAR)

2. **target** (20,412 rows) — Druggability classification
   - id (INTEGER, same as protein.id), tdl (VARCHAR: 'Tclin', 'Tchem', 'Tbio', 'Tdark')
   - fam (VARCHAR, short family e.g. 'Kinase', 'GPCR', 'Ion channel', 'Enzyme')
   - idg (BOOLEAN, IDG designated target), name (VARCHAR)

3. **t2tc** (20,412 rows) — Links protein to target (1:1 mapping)
   - protein_id (INTEGER → protein.id), target_id (INTEGER → target.id)

### TDL (Target Development Level) — KEY CONCEPT
- **Tclin**: Clinical targets — approved drugs act through them
- **Tchem**: Chemical targets — potent small molecule binders exist
- **Tbio**: Biological targets — well-studied biology but no drugs
- **Tdark**: Dark/understudied targets (~31% of proteome)
Filter with: `WHERE t.tdl = 'Tdark'` (exact string, case-sensitive)

### Disease Tables

4. **ncats_disease** (13,953 rows) — Disease catalog
   - name (VARCHAR, e.g. 'Alzheimer disease'), mondoid (VARCHAR, e.g. 'MONDO:0004975')
   - mondo_description (VARCHAR), target_count (INTEGER), gard_rare (BOOLEAN)

5. **disease** (363,134 rows) — Protein-disease associations
   - protein_id (INTEGER → protein.id), name (VARCHAR, disease name)
   - dtype (VARCHAR, data source e.g. 'UniProt Disease', 'Monarch', 'DisGeNET', 'CTD')
   - did (VARCHAR, disease ID), score (DECIMAL), source (VARCHAR)

### Ligand/Drug Tables

6. **ncats_ligands** (355,932 rows) — Drugs and chemical compounds
   - id (UINTEGER), name (VARCHAR, e.g. 'imatinib'), isDrug (BOOLEAN)
   - smiles (VARCHAR), actCnt (INTEGER, activity count), targetCount (INTEGER)
   - description (VARCHAR), "PubChem" (VARCHAR), "ChEMBL" (VARCHAR)

7. **ncats_ligand_activity** (645,196 rows) — Ligand-target bioactivity
   - ncats_ligand_id (UINTEGER → ncats_ligands.id), target_id (INTEGER → protein.id)
   - act_value (DECIMAL, pActivity: higher = more potent, 7.0 ≈ 100nM)
   - act_type (VARCHAR: 'IC50', 'Ki', 'EC50', 'Kd'), action_type (VARCHAR, mechanism)

8. **drug_activity** (4,436 rows) — Known drug-target interactions
   - target_id (INTEGER → protein.id), drug (VARCHAR, drug name)
   - act_value (DECIMAL), act_type (VARCHAR), action_type (VARCHAR)
   - has_moa (BOOLEAN, mechanism of action known)

### Other Tables

9. **tinx_novelty** (18,982 rows) — Target novelty scores
   - protein_id (INTEGER → protein.id), score (DECIMAL, 0-1: higher = more understudied)

10. **ncats_ppi** (11,691,825 rows) — Protein-protein interactions
    - protein_id (INTEGER), other_id (INTEGER), score (INTEGER)
    - ppitypes (VARCHAR), interaction_type (VARCHAR)
    WARNING: This table is very large. Always filter by protein_id and use LIMIT.

### Common JOIN Patterns

**Target info with TDL:**
```sql
SELECT p.sym, p.name, t.tdl, t.fam, p.uniprot
FROM protein p
JOIN target t ON p.id = t.id
WHERE t.tdl = 'Tdark'
LIMIT 50
```

**Disease targets:**
```sql
SELECT DISTINCT p.sym, t.tdl, t.fam, d.name AS disease
FROM disease d
JOIN protein p ON d.protein_id = p.id
JOIN target t ON p.id = t.id
WHERE LOWER(d.name) LIKE '%alzheimer%'
LIMIT 50
```

**Ligand bioactivity:**
```sql
SELECT l.name, l.isDrug, la.act_type, la.act_value, p.sym
FROM ncats_ligand_activity la
JOIN ncats_ligands l ON la.ncats_ligand_id = l.id
JOIN protein p ON la.target_id = p.id
WHERE LOWER(l.name) LIKE '%imatinib%'
LIMIT 50
```

**Understudied targets with bioactivity:**
```sql
SELECT p.sym, t.tdl, t.fam, tn.score AS novelty, COUNT(la.id) AS activity_count
FROM protein p
JOIN target t ON p.id = t.id
LEFT JOIN tinx_novelty tn ON p.id = tn.protein_id
LEFT JOIN ncats_ligand_activity la ON p.id = la.target_id
WHERE t.tdl = 'Tdark'
GROUP BY p.sym, t.tdl, t.fam, tn.score
HAVING COUNT(la.id) > 0
ORDER BY activity_count DESC
LIMIT 50
```

### SQL Tips
- JOIN protein to target via: `protein.id = target.id` (direct, or through t2tc)
- Use LOWER() LIKE for text searches: `LOWER(p.sym) LIKE '%egfr%'`
- Gene symbols are UPPERCASE in protein.sym: 'EGFR', 'BRCA1', 'TP53'
- TDL values are exact strings: 'Tclin', 'Tchem', 'Tbio', 'Tdark'
- ncats_ligands column names with spaces need quotes: "PubChem", "ChEMBL"
- Always include LIMIT (max 200 rows)
- ncats_ppi is 11M rows — always filter by protein_id
""".strip()


def get_schema_summary() -> str:
    """Return the Pharos schema description for injection into the system prompt."""
    return PHAROS_SCHEMA
