from data_connector import get_connection

# Business descriptions for known columns — these never change unless YOU change them
# If a column isn't listed here, it still appears in the schema (just without a description)
COLUMN_DESCRIPTIONS = {
    "Fact_StoreWeekly": {
        "StoreID": "Plain integer (e.g. 8001) — use CAST trick to join to Dim_Store",
        "WeeklyAUV": "Average Unit Volume in dollars for that week",
        "SSS_Pct": "Same Store Sales % growth vs prior year",
        "SST_Pct": "Same Store Transactions % growth vs prior year",
        "EBITDA_Pct": "EBITDA as % of sales",
        "AvgTicket": "Average ticket/check size in dollars",
        "OLO_Pct": "Online ordering as % of sales",
        "ThreePD_Pct": "Third-party delivery as % of sales",
        "Loyalty_Pct": "Loyalty program participation %",
        "WeeklyTransactions": "Total transactions that week",
        "AUVTrend_Pct": "AUV trend vs prior period",
        "OpEx_Score": "Operational excellence score",
        "OSAT_Pct": "Customer satisfaction %",
        "Accuracy_Pct": "Order accuracy %",
        "FBC_Name": "Field Business Consultant name",
        "RegionalVP": "Regional VP name",
        "SSS_Dollar": "Same store sales in dollar terms",
        "CompTicketCount_PY": "Comparable ticket count prior year",
        "CompTicketCount_Change": "Change in comp ticket count",
    },
    "Dim_Store": {
        "StoreID": "Has '#' prefix (e.g. #8001) — join directly to Fact_OSAT and Fact_FSAScore",
        "AuditedThisQtr": "Whether the store was audited this quarter (TRUE/FALSE)",
        "FirstPriorityFinding": "Most critical finding from last food safety audit",
        "AreaDirector": "Area Director responsible for this store",
    },
    "Dim_FranchiseOwner": {
        "StoreCount": "Total number of stores owned by this franchise owner",
        "FBC": "Field Business Consultant assigned to this owner",
    },
    "Fact_OSAT": {
        "StoreID": "Has '#' prefix — joins directly to Dim_Store.StoreID",
        "SurveyWeek": "Fiscal week key for the survey period",
        "TotalResponses": "Total number of guest survey responses",
        "TopBoxResponses": "Number of top-box (highest rating) responses",
        "OSAT_TopBox_Pct": "Guest satisfaction top-box percentage",
        "FoodQuality_Score": "Food quality rating score",
        "Service_Score": "Service rating score",
        "Cleanliness_Score": "Cleanliness rating score",
        "ValueScore": "Value for money rating score",
    },
    "Fact_FSAScore": {
        "StoreID": "Has '#' prefix — joins directly to Dim_Store.StoreID",
        "FSA_Score": "Food Safety Audit score (0-100)",
        "FirstPriorityFinding": "Most critical food safety finding",
        "SecondPriorityFinding": "Second most critical food safety finding",
        "AuditorCode": "Code identifying the auditor",
    },
    "Ref_Targets": {
        "Persona": "Performance target tier: Corporate, Regional, FBC, or Franchisee",
        "AUVTarget": "Target Average Unit Volume in dollars",
        "EBITDATarget": "Target EBITDA %",
        "SSSTarget": "Target Same Store Sales growth %",
        "SSTTarget": "Target Same Store Transactions growth %",
        "OSATTarget": "Target guest satisfaction %",
        "FSATarget": "Target food safety audit score",
    },
}

# Static rules that never change — join logic and SQL generation instructions
STATIC_RULES = """
## CRITICAL JOIN RULES
- Fact_StoreWeekly.StoreID is a plain integer (e.g. 8001)
- Dim_Store.StoreID has a '#' prefix (e.g. #8001)
- To join these two: CAST(Fact_StoreWeekly.StoreID AS TEXT) = REPLACE(Dim_Store.StoreID, '#', '')
- Fact_OSAT.StoreID and Fact_FSAScore.StoreID already have '#' prefix — join directly to Dim_Store.StoreID

## SQL GENERATION RULES
1. Always use SQLite syntax
2. Use ROUND(value, 2) for percentages and dollar amounts
3. Use LIMIT 20 unless the question asks for all results or a specific count
4. For ranking questions (top/bottom N), use ORDER BY with LIMIT
5. For region comparisons, GROUP BY Region
6. Prefer AVG() for performance metrics, SUM() for volume metrics
7. When comparing to targets, JOIN Ref_Targets on Persona
8. Return ONLY the SQL query — no explanation, no markdown, no backticks
"""


def get_schema_context() -> str:
    """
    Dynamically generates the full schema context string for Claude.
    - Table names and column names/types are read live from SQLite
    - Row counts are computed from actual data
    - Business descriptions are merged in from COLUMN_DESCRIPTIONS above
    - If you add a new CSV, it appears automatically — no code change needed
    """
    conn = get_connection()

    # Get all table names currently loaded in SQLite
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    schema = "You are a SQL expert assistant for Jersey Mike's Subs data platform. Generate SQLite-compatible SQL queries based on the schema below.\n"
    schema += STATIC_RULES
    schema += "\n## TABLES (auto-generated from live data)\n"

    for (table_name,) in tables:
        # Get live row count
        row_count = conn.execute(
            f"SELECT COUNT(*) FROM [{table_name}]"
        ).fetchone()[0]

        # Get column info: (index, name, type, notnull, default, pk)
        columns = conn.execute(
            f"PRAGMA table_info([{table_name}])"
        ).fetchall()

        schema += f"\n### {table_name} ({row_count:,} rows)\n"

        # Get descriptions for this table if they exist
        table_descriptions = COLUMN_DESCRIPTIONS.get(table_name, {})

        for col in columns:
            col_name = col[1]
            col_type = col[2] if col[2] else "TEXT"
            description = table_descriptions.get(col_name, "")

            if description:
                schema += f"- {col_name} ({col_type}): {description}\n"
            else:
                schema += f"- {col_name} ({col_type})\n"

    return schema