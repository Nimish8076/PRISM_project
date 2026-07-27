from data_connector import get_connection
import pandas as pd

conn = get_connection()

latest = pd.read_sql_query("SELECT MAX(FiscalWeekKey) AS mw FROM Fact_StoreWeekly", conn)
print("Latest week:", latest["mw"][0])

q = """
SELECT
  COUNT(*) AS total_stores,
  SUM(CASE WHEN SSS_Pct   < 5.0  THEN 1 ELSE 0 END) AS sss_below,
  SUM(CASE WHEN OSAT_Pct  < 85   THEN 1 ELSE 0 END) AS osat_below,
  SUM(CASE WHEN EBITDA_Pct< 20.8 THEN 1 ELSE 0 END) AS eb_below
FROM Fact_StoreWeekly
WHERE FiscalWeekKey = (SELECT MAX(FiscalWeekKey) FROM Fact_StoreWeekly)
"""
print(pd.read_sql_query(q, conn).to_string())

# Also check: how many DISTINCT stores have a row in that latest week?
q2 = """
SELECT COUNT(DISTINCT StoreID) AS stores_in_latest_week
FROM Fact_StoreWeekly
WHERE FiscalWeekKey = (SELECT MAX(FiscalWeekKey) FROM Fact_StoreWeekly)
"""
print(pd.read_sql_query(q2, conn).to_string())