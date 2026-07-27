from data_connector import get_connection
import pandas as pd

conn = get_connection()
df = pd.read_sql_query("SELECT Persona FROM Ref_Targets", conn)
print("Personas in table (with quotes to reveal spaces):")
for p in df["Persona"]:
    print(f"  '{p}'")

# Show what TARGET_PERSONA the agent is actually using
import agent
print()
print("Agent's TARGET_PERSONA =", repr(agent.TARGET_PERSONA))

# Try the exact query the agent runs
q = f"SELECT * FROM Ref_Targets WHERE Persona = '{agent.TARGET_PERSONA}'"
print("Query result rows:", len(pd.read_sql_query(q, conn)))