"""DuckDB connection manager for querying local Parquet data.

Manages an in-memory DuckDB connection with views over Parquet files.
All three biomedical data sources (DrugCentral, Clinical Trials AACT,
Pharos/TCRD) are loaded as read-only views at startup.

DuckDB's Python client is synchronous, so all queries are wrapped
in asyncio.to_thread() for FastAPI async compatibility.
"""

import asyncio
import duckdb
from pathlib import Path

from app.config import settings


DATASETS = ("drugcentral", "clinicaltrials", "pharos")

_conn: duckdb.DuckDBPyConnection | None = None
_loaded: set[str] = set()
_signature: tuple = ()


def loaded_datasets() -> set[str]:
    """Names of the datasets whose Parquet files were found at the last init()."""
    return set(_loaded)


def missing_datasets() -> list[str]:
    """Datasets that have no Parquet files on disk, in DATASETS order."""
    return [name for name in DATASETS if name not in _loaded]


def reload():
    """Close and reopen the connection, re-scanning the Parquet directory.

    Called after app.data_sync installs or updates a dataset, so new data is
    served without restarting the server.
    """
    close()
    init()


def _disk_signature() -> tuple:
    """Cheap fingerprint of the installed datasets: (name, release tag, file count)."""
    sig = []
    for name in DATASETS:
        path = settings.PARQUET_DIR / name
        release = path / ".release"
        tag = release.read_text(encoding="utf-8").strip() if release.exists() else ""
        count = len(list(path.glob("*.parquet"))) if path.exists() else 0
        sig.append((name, tag, count))
    return tuple(sig)


def ensure_current():
    """Reload if another process installed or updated a dataset since init().

    With several uvicorn workers each process has its own connection; the one
    that ran the download reloads itself, the others catch up here. Three
    small file reads, so it is safe to call per request.
    """
    if _conn is None or _disk_signature() != _signature:
        reload()


def init():
    """Open in-memory DuckDB connection and create views over Parquet files.

    Called once at application startup from lifespan.py.
    Creates a view for each .parquet file found in the data directories,
    prefixed by database name (e.g. drugcentral_structures, pharos_protein).
    Then creates simplified views for common query patterns.
    """
    global _conn, _loaded, _signature
    _conn = duckdb.connect(":memory:")
    _loaded = set()
    _signature = _disk_signature()

    parquet_dir = settings.PARQUET_DIR

    # Create raw views over each Parquet file
    for db_name in DATASETS:
        db_path = parquet_dir / db_name
        parquet_files = (
            [f for f in db_path.glob("*.parquet") if not f.name.startswith("._")]
            if db_path.exists() else []
        )
        if not parquet_files:
            print(f"[WARNING] No Parquet data for {db_name} in {db_path} (open /data to download it)")
            continue

        print(f"[DuckDB] Loading {len(parquet_files)} tables from {db_name}/")
        for f in parquet_files:
            view_name = f"{db_name}_{f.stem}"
            # Use forward slashes for DuckDB path compatibility
            file_path = str(f).replace("\\", "/")
            _conn.execute(
                f"CREATE VIEW {view_name} AS SELECT * FROM read_parquet('{file_path}')"
            )
        _loaded.add(db_name)

    _create_simplified_views()
    print(f"[DuckDB] Views created for: {', '.join(sorted(_loaded)) or 'no datasets'}")


def close():
    """Close DuckDB connection. Called at application shutdown."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None


async def execute_query(sql: str, params: list | None = None) -> list[dict]:
    """Execute a read-only SQL query and return results as list of dicts.

    Runs synchronous DuckDB query in a thread pool to avoid blocking
    the FastAPI event loop.
    """
    ensure_current()
    def _sync():
        if params:
            result = _conn.execute(sql, params)
        else:
            result = _conn.execute(sql)
        columns = [desc[0] for desc in result.description] if result.description else []
        return [dict(zip(columns, row)) for row in result.fetchall()]

    return await asyncio.to_thread(_sync)


async def execute_query_raw(sql: str, params: list | None = None) -> dict:
    """Execute a query and return raw result with columns and rows.

    Returns dict with 'columns' (list[str]), 'rows' (list[tuple]),
    and 'row_count' (int). Used by DrugCentral text-to-SQL service.
    """
    ensure_current()
    def _sync():
        if params:
            result = _conn.execute(sql, params)
        else:
            result = _conn.execute(sql)

        columns = [desc[0] for desc in result.description] if result.description else []
        rows = result.fetchall()
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }

    return await asyncio.to_thread(_sync)


async def get_table_columns(table_name: str) -> list[str]:
    """Return column names for a view or table, or an empty list if it does not exist."""
    def _sync():
        try:
            result = _conn.execute(f"DESCRIBE {table_name}")
            return [row[0] for row in result.fetchall()]
        except Exception:
            return []

    return await asyncio.to_thread(_sync)


def _create_simplified_views():
    """Create simplified views over raw Parquet tables.

    These views provide a cleaner query interface for the LLM,
    hiding JOIN complexity behind simple table names.
    """
    if "drugcentral" in _loaded:
        _create_drugcentral_views()
    if "clinicaltrials" in _loaded:
        _create_clinicaltrials_views()
    if "pharos" in _loaded:
        _create_pharos_views()


def _create_drugcentral_views():
    """Create DrugCentral views matching the DRUGCENTRAL_SCHEMA in query.py.

    The LLM generates SQL targeting these view names (v_drug_info, v_drug_targets, etc.)
    and the underlying table names (structures, synonyms, etc.). Both must exist.

    View definitions verified against real Parquet data on 2026-05-25.
    """
    # Expose underlying tables without prefix so the LLM can query them directly.
    # The schema tells the LLM: "You can query underlying tables directly too:
    # structures, omop_relationship, act_table_full, ..."
    underlying = [
        "structures", "synonyms", "act_table_full", "approval",
        "active_ingredient", "product", "struct2atc", "atc",
        "omop_relationship", "target_dictionary", "pharma_class",
        "ddi", "ddi_risk", "faers", "property", "property_type",
        "identifier", "id_type",
    ]
    for table in underlying:
        _conn.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM drugcentral_{table}")

    # v_drug_info: Basic drug information with aggregated synonyms
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_info AS
        SELECT s.id AS drug_id, s.name AS drug_name, s.cas_reg_no,
               s.cd_formula AS molecular_formula, s.cd_molweight AS mol_weight,
               s.smiles,
               STRING_AGG(DISTINCT syn.name, ', ') AS synonyms
        FROM structures s
        LEFT JOIN synonyms syn ON s.id = syn.id
        GROUP BY s.id, s.name, s.cas_reg_no, s.cd_formula, s.cd_molweight, s.smiles
    """)

    # v_drug_indications: Drug-disease relationships
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_indications AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               o.concept_name AS condition_name, o.relationship_name AS relationship_type,
               o.umls_cui, o.snomed_conceptid
        FROM structures s
        JOIN omop_relationship o ON s.id = o.struct_id
    """)

    # v_drug_targets: Drug targets with bioactivity and MOA
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_targets AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               a.target_name, a.target_class, a.gene AS target_gene,
               a.accession AS uniprot_accession, a.organism,
               a.act_value, a.act_type,
               CASE WHEN a.moa = 1 THEN 'Yes' ELSE 'No' END AS mechanism_of_action,
               a.moa_source
        FROM structures s
        JOIN act_table_full a ON s.id = a.struct_id
    """)

    # v_disease_drugs: Lookup drugs by disease name
    _conn.execute("""
        CREATE OR REPLACE VIEW v_disease_drugs AS
        SELECT o.concept_name AS disease_name, o.relationship_name,
               s.name AS drug_name, s.id AS drug_id,
               o.umls_cui, o.snomed_conceptid
        FROM omop_relationship o
        JOIN structures s ON o.struct_id = s.id
    """)

    # v_drug_classification: Pharmacological classification
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_classification AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               pc.type AS class_type, pc.name AS class_name,
               pc.class_code, pc.source AS class_source
        FROM structures s
        JOIN pharma_class pc ON s.id = pc.struct_id
    """)

    # v_drug_interactions: Drug-drug interaction data
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_interactions AS
        SELECT drug_class1 AS drug_class_1, drug_class2 AS drug_class_2,
               ddi_risk, description AS interaction_description
        FROM ddi
    """)

    # v_adverse_events: FDA FAERS adverse events (statistically significant only)
    _conn.execute("""
        CREATE OR REPLACE VIEW v_adverse_events AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               f.meddra_name AS adverse_event, f.meddra_code,
               f.level AS event_level, f.llr AS log_likelihood_ratio,
               f.llr_threshold
        FROM faers f
        JOIN structures s ON f.struct_id = s.id
        WHERE f.llr > f.llr_threshold
    """)

    # v_drug_approvals: Regulatory approval dates
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_approvals AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               a.approval AS approval_date, a.type AS approval_type,
               a.applicant, a.orphan
        FROM approval a
        JOIN structures s ON a.struct_id = s.id
    """)

    # v_drug_properties: Chemical and physical properties
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_properties AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               p.property_type_symbol AS property_name,
               pt.category AS property_category,
               p.value AS property_value,
               pt.units
        FROM property p
        JOIN structures s ON p.struct_id = s.id
        LEFT JOIN property_type pt ON p.property_type_id = pt.id
    """)

    # v_drug_identifiers: Cross-references to external databases
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_identifiers AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               i.id_type AS database_name, i.identifier AS external_id
        FROM identifier i
        JOIN structures s ON i.struct_id = s.id
    """)

    # v_drug_atc: WHO ATC therapeutic classification hierarchy
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_atc AS
        SELECT s.name AS drug_name, s.id AS drug_id,
               a.code AS atc_code, a.l1_name AS anatomical_group,
               a.l2_name AS therapeutic_subgroup, a.l3_name AS pharmacological_subgroup,
               a.l4_name AS chemical_subgroup, a.chemical_substance
        FROM structures s
        JOIN struct2atc sa ON s.id = sa.struct_id
        JOIN atc a ON sa.atc_code = a.code
    """)

    print("[DuckDB] DrugCentral: 11 v_* views + 18 underlying table aliases created")


def _create_clinicaltrials_views():
    """Create Clinical Trials (AACT) views matching the CLINICAL_TRIALS_SCHEMA in query.py.

    These are the same 12 views from the _VIEWS_SQL constant in query.py,
    with `ctgov.` schema prefix removed (DuckDB has no schemas; the underlying
    tables are exposed as unprefixed aliases by init()).

    View definitions verified against real Parquet data on 2026-05-25.
    All Parquet columns are VARCHAR (AACT exports text); DuckDB casts implicitly.
    """
    # Expose underlying tables without the clinicaltrials_ prefix.
    # The _VIEWS_SQL references tables like "studies", "interventions", etc.
    underlying = [
        "studies", "interventions", "conditions", "browse_conditions",
        "sponsors", "outcomes", "outcome_analyses", "eligibilities",
        "design_groups", "outcome_measurements", "result_groups",
    ]
    for table in underlying:
        _conn.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM clinicaltrials_{table}"
        )

    # v_studies: Core study info
    _conn.execute("""
        CREATE OR REPLACE VIEW v_studies AS
        SELECT nct_id, brief_title AS title, phase, overall_status,
               enrollment, study_type, start_date, completion_date,
               results_first_posted_date
        FROM studies
    """)

    # v_study_drugs: Drug/biological interventions only
    _conn.execute("""
        CREATE OR REPLACE VIEW v_study_drugs AS
        SELECT nct_id, name AS drug_name, intervention_type, description
        FROM interventions
        WHERE intervention_type IN ('DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT')
    """)

    # v_study_conditions: Conditions with MeSH mappings
    _conn.execute("""
        CREATE OR REPLACE VIEW v_study_conditions AS
        SELECT c.nct_id, c.name AS condition_name, bc.mesh_term, bc.mesh_type
        FROM conditions c
        LEFT JOIN browse_conditions bc ON c.nct_id = bc.nct_id
    """)

    # v_drug_trials: Studies joined with drug interventions
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_trials AS
        SELECT s.nct_id, s.brief_title AS title, s.phase, s.overall_status,
               i.name AS drug_name, s.enrollment
        FROM studies s
        JOIN interventions i ON s.nct_id = i.nct_id
        WHERE i.intervention_type IN ('DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT')
    """)

    # v_condition_trials: Studies joined with conditions + MeSH
    _conn.execute("""
        CREATE OR REPLACE VIEW v_condition_trials AS
        SELECT s.nct_id, s.brief_title AS title, s.phase, s.overall_status,
               c.name AS condition_name, bc.mesh_term, s.enrollment
        FROM studies s
        JOIN conditions c ON s.nct_id = c.nct_id
        LEFT JOIN browse_conditions bc ON s.nct_id = bc.nct_id
    """)

    # v_drug_condition_trials: The ONLY view with both drug and condition
    _conn.execute("""
        CREATE OR REPLACE VIEW v_drug_condition_trials AS
        SELECT s.nct_id, s.brief_title AS title, s.phase, s.overall_status,
               i.name AS drug_name, c.name AS condition_name,
               bc.mesh_term, s.enrollment
        FROM studies s
        JOIN interventions i ON s.nct_id = i.nct_id
        JOIN conditions c ON s.nct_id = c.nct_id
        LEFT JOIN browse_conditions bc ON s.nct_id = bc.nct_id
        WHERE i.intervention_type IN ('DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT')
    """)

    # v_trial_summary: Study details with lead sponsor
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_summary AS
        SELECT s.nct_id, s.brief_title AS title, s.phase, s.overall_status,
               s.enrollment, s.study_type, s.start_date, s.completion_date,
               sp.name AS sponsor_name, sp.agency_class AS sponsor_class
        FROM studies s
        LEFT JOIN sponsors sp ON s.nct_id = sp.nct_id
              AND sp.lead_or_collaborator = 'lead'
    """)

    # v_trial_outcomes: Endpoint definitions
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_outcomes AS
        SELECT nct_id, outcome_type, title, description, time_frame, population
        FROM outcomes
    """)

    # v_trial_results: Statistical results of outcome analyses
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_results AS
        SELECT o.nct_id, o.title AS outcome_title, o.outcome_type,
               oa.method AS stat_method, oa.param_type, oa.param_value,
               oa.p_value, oa.p_value_modifier, oa.ci_percent,
               oa.ci_lower_limit, oa.ci_upper_limit,
               oa.estimate_description, oa.groups_description
        FROM outcomes o
        JOIN outcome_analyses oa ON o.nct_id = oa.nct_id AND o.id = oa.outcome_id
    """)

    # v_trial_eligibility: Inclusion/exclusion criteria
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_eligibility AS
        SELECT nct_id, gender, minimum_age, maximum_age, healthy_volunteers, criteria
        FROM eligibilities
    """)

    # v_trial_arms: Treatment arms and comparator groups
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_arms AS
        SELECT nct_id, group_type, title, description
        FROM design_groups
    """)

    # v_trial_measurements: Actual numeric results per treatment arm
    _conn.execute("""
        CREATE OR REPLACE VIEW v_trial_measurements AS
        SELECT om.nct_id, o.title AS outcome_title, o.outcome_type,
               rg.title AS group_title, om.classification, om.category,
               om.param_type, om.param_value_num AS value, om.units,
               om.dispersion_type, om.dispersion_value_num AS dispersion_value,
               om.dispersion_lower_limit, om.dispersion_upper_limit
        FROM outcome_measurements om
        JOIN outcomes o ON om.nct_id = o.nct_id AND om.outcome_id = o.id
        JOIN result_groups rg ON om.nct_id = rg.nct_id AND om.result_group_id = rg.id
    """)

    print("[DuckDB] ClinicalTrials: 12 v_* views + 11 underlying table aliases created")


def _create_pharos_views():
    """Create Pharos/TCRD table aliases used by pharos/query.py SQL queries.

    The Pharos query service uses structured SQL queries (not LLM-generated)
    that reference table names directly: protein, target, t2tc, disease,
    ncats_disease, ncats_ligands, ncats_ligand_activity, tinx_novelty.

    These aliases map the prefixed views (pharos_protein, etc.) to the
    unprefixed names used in the SQL queries.

    Table relationships verified against real Parquet data on 2026-05-25:
    - protein.id = t2tc.protein_id = t2tc.target_id (1:1 mapping, 20,412 rows)
    - t2tc.target_id = target.id
    - disease.protein_id = protein.id
    - ncats_ligand_activity.target_id = protein.id
    - ncats_ligand_activity.ncats_ligand_id = ncats_ligands.id
    - tinx_novelty.protein_id = protein.id
    """
    underlying = [
        "protein", "target", "t2tc", "disease", "ncats_disease",
        "ncats_ligands", "ncats_ligand_activity", "drug_activity",
        "ncats_ppi", "tinx_novelty", "alias",
    ]
    for table in underlying:
        _conn.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM pharos_{table}")

    print("[DuckDB] Pharos: 11 underlying table aliases created")
