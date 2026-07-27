from data_connector import get_connection
import pandas as pd

conn = get_connection()

# This is the EXACT query the agent runs for SSS_Pct
sortkey = "substr(FiscalWeekKey,7,4) || substr(FiscalWeekKey,4,2) || substr(FiscalWeekKey,1,2)"
sql = f"""
WITH ranked AS (
    SELECT
        StoreID,
        FiscalWeekKey,
        SSS_Pct AS metric_val,
        ROW_NUMBER() OVER (PARTITION BY StoreID ORDER BY {sortkey} DESC) AS rn,
        AVG(SSS_Pct) OVER (
            PARTITION BY StoreID
            ORDER BY {sortkey} DESC
            ROWS BETWEEN 1 AND 8
        ) AS trailing_avg
    FROM Fact_StoreWeekly
    WHERE SSS_Pct IS NOT NULL
)
SELECT StoreID, FiscalWeekKey, metric_val, trailing_avg
FROM ranked
WHERE rn = 1
"""

try:
    df = pd.read_sql_query(sql, conn)
    print("Query ran. Rows returned:", len(df))
    print(df.head(10).to_string())
    print("\nSSS target is 5.0 (Franchisee). Stores below 5.0 - 3.0 gap = below 2.0:")
    print("Count below 2.0:", (df["metric_val"] < 2.0).sum())
except Exception as e:
    print("QUERY FAILED:")
    print(e)