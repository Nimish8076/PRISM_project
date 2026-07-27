from data_connector import get_connection
import pandas as pd

conn = get_connection()
df = pd.read_sql_query("SELECT StoreID, FiscalWeekKey, OSAT_Pct, EBITDA_Pct FROM Fact_StoreWeekly WHERE OSAT_Pct IS NOT NULL", conn)
df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")

# Latest row per store
latest = df.sort_values("_d").groupby("StoreID", as_index=False).last()

print("OSAT target=85, gap=5 -> flag if latest OSAT < 80")
print("  stores with latest OSAT < 80:", (latest["OSAT_Pct"] < 80).sum())
print("  latest OSAT min/max:", round(latest["OSAT_Pct"].min(),1), round(latest["OSAT_Pct"].max(),1))
print()
print("EBITDA target=20.8, gap=4 -> flag if latest EBITDA < 16.8")
print("  stores with latest EBITDA < 16.8:", (latest["EBITDA_Pct"] < 16.8).sum())
print("  latest EBITDA min/max:", round(latest["EBITDA_Pct"].min(),1), round(latest["EBITDA_Pct"].max(),1))