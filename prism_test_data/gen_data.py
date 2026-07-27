"""Generate a small LABELED test dataset for PRISM.

10 stores x 10 weeks. Each store has a known, deliberately-planted condition so we
can check whether PRISM flags exactly the right ones (and leaves the controls alone).
Franchisee targets: SSS 5.0, OSAT 85, EBITDA 20.8, FSA 93, hard floor 80.
"""
import os, csv

OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)

# 10 weekly keys (DD-MM-YYYY), latest = 10-03-2025
import datetime
start = datetime.date(2025, 1, 6)
WEEKS = [(start + datetime.timedelta(days=7 * i)).strftime("%d-%m-%Y") for i in range(10)]
LATEST = WEEKS[-1]

# Per-store spec. baseline = weeks 0..8, "_l" = latest-week override (week 9).
# sss/osat/ebitda in %, tx = weekly transactions, tk = avg ticket, fsa = latest audit score.
S = [
    # --- Group A: 3 stores, same FBC/AD/Region, all SSS collapse (both methods) ---
    dict(id=9001, city="Westford",  region="Test-West", fbc="Alex Rivera", ad="Dana Cole",
         owner="West Holdings", rvp="Pat Vance", sss=6.0, sss_l=1.4, tx=2000, tx_l=1600, tk=17.0, tk_l=17.0),
    dict(id=9002, city="Westgate",  region="Test-West", fbc="Alex Rivera", ad="Dana Cole",
         owner="West Holdings", rvp="Pat Vance", sss=6.0, sss_l=1.5, tx=2000, tx_l=1580, tk=17.0, tk_l=17.0),
    dict(id=9003, city="Westbrook", region="Test-West", fbc="Alex Rivera", ad="Dana Cole",
         owner="West Holdings", rvp="Pat Vance", sss=6.0, sss_l=1.3, tx=2000, tx_l=1550, tk=17.0, tk_l=17.0),

    # --- Group C: multi-metric store (SSS + EBITDA both collapse) ---
    dict(id=9004, city="Eastburg",  region="Test-East", fbc="Sam Lee", ad="Chris Bell",
         owner="East Holdings", rvp="Morgan Diaz", sss=6.0, sss_l=1.5, ebitda=22.0, ebitda_l=11.0,
         tx=2000, tx_l=1900, tk=17.0, tk_l=15.0),

    # --- Group D: food-safety ---
    dict(id=9005, city="Easton",    region="Test-East", fbc="Sam Lee", ad="Chris Bell",
         owner="East Holdings", rvp="Morgan Diaz", fsa=62, fsa_find="Cold-holding temperature out of range"),  # < 80 floor -> CRITICAL
    dict(id=9006, city="Eastvale",  region="Test-East", fbc="Sam Lee", ad="Chris Bell",
         owner="East Holdings", rvp="Morgan Diaz", fsa=88, fsa_find="Handwashing station unstocked"),           # < 93 target, >= 80 -> HIGH

    # --- Group F: OSAT sudden drop only (statistical, still above target) ---
    dict(id=9007, city="Eastland",  region="Test-East", fbc="Sam Lee", ad="Chris Bell",
         owner="East Holdings", rvp="Morgan Diaz", osat=90.0, osat_l=80.0,
         osat_sub=dict(food=3.9, service=4.5, clean=4.6, value=4.4)),  # FoodQuality lowest

    # --- Controls: stable, at/above target, should NOT flag ---
    dict(id=9010, city="Northgate", region="Test-North", fbc="Jordan Kim", ad="Lee Park",
         owner="North Holdings", rvp="Robin Shah"),
    dict(id=9011, city="Northvale", region="Test-North", fbc="Jordan Kim", ad="Lee Park",
         owner="North Holdings", rvp="Robin Shah"),
    dict(id=9012, city="Northbrook",region="Test-North", fbc="Jordan Kim", ad="Lee Park",
         owner="North Holdings", rvp="Robin Shah"),
]

# defaults (healthy)
DEF = dict(sss=6.0, osat=90.0, ebitda=22.0, tx=2000, tk=17.0, fsa=95, fsa_find="No priority findings",
           osat_sub=dict(food=4.6, service=4.6, clean=4.6, value=4.6))
def g(s, k): return s.get(k, DEF[k])
def gl(s, k): return s.get(k + "_l", g(s, k))   # latest override or baseline

# ---- Fact_StoreWeekly ----
cols = ["StoreID","FranchiseOwner","Region","FiscalWeekKey","FiscalYear","FiscalQuarter",
        "WeeklyAUV","SSS_Pct","SST_Pct","EBITDA_Pct","AvgTicket","WeeklyTransactions",
        "OSAT_Pct","Accuracy_Pct","FBC_Name","RegionalVP"]
with open(os.path.join(OUT,"Fact_StoreWeekly.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(cols)
    for s in S:
        for i,wk in enumerate(WEEKS):
            last = (i==len(WEEKS)-1)
            sss = gl(s,"sss") if last else g(s,"sss")
            osat= gl(s,"osat") if last else g(s,"osat")
            eb  = gl(s,"ebitda") if last else g(s,"ebitda")
            tx  = gl(s,"tx") if last else g(s,"tx")
            tk  = gl(s,"tk") if last else g(s,"tk")
            w.writerow([s["id"], s["owner"], s["region"], wk, 2025, "Q1",
                        round(tx*tk,2), sss, 2.5, eb, tk, tx, osat, 96.0, s["fbc"], s["rvp"]])

# ---- Dim_Store ----
with open(os.path.join(OUT,"Dim_Store.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["StoreID","StoreNumber","City","Region","FranchiseOwner","AuditedThisQtr","FirstPriorityFinding","AreaDirector"])
    for s in S:
        w.writerow([f"#{s['id']}", s["id"], s["city"], s["region"], s["owner"], "True", "False", s["ad"]])

# ---- Fact_FSAScore (two audits per store; latest = planted) ----
with open(os.path.join(OUT,"Fact_FSAScore.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["AuditID","StoreID","FranchiseOwner","Region","AuditDate","FiscalYear","FiscalQuarter","FSA_Score","FirstPriorityFinding","SecondPriorityFinding","AuditorCode"])
    aid=20001
    for s in S:
        w.writerow([f"AUD-{aid}", f"#{s['id']}", s["owner"], s["region"], "2025-01-15", 2025, "Q1", 95, "No priority findings", "None", "STR-01"]); aid+=1
        w.writerow([f"AUD-{aid}", f"#{s['id']}", s["owner"], s["region"], "2025-03-10", 2025, "Q1", g(s,"fsa"), s.get("fsa_find","No priority findings"), "None", "STR-02"]); aid+=1

# ---- Fact_OSAT (latest survey week; sub-scores) ----
with open(os.path.join(OUT,"Fact_OSAT.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["StoreID","FranchiseOwner","Region","SurveyWeek","FiscalYear","FiscalQuarter","FiscalMonth","TotalResponses","TopBoxResponses","OSAT_TopBox_Pct","FoodQuality_Score","Service_Score","Cleanliness_Score","ValueScore"])
    for s in S:
        sub=g(s,"osat_sub")
        w.writerow([f"#{s['id']}", s["owner"], s["region"], LATEST, 2025, "Q1", 3, 200, 150, 75.0,
                    sub["food"], sub["service"], sub["clean"], sub["value"]])

# ---- Ref_Targets (same as production) ----
with open(os.path.join(OUT,"Ref_Targets.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["Persona","AUVTarget","EBITDATarget","SSSTarget","SSTTarget","OLOTarget","LoyaltyTarget","FSATarget","OSATTarget","AccuracyTarget"])
    w.writerow(["Corporate",38461,19.5,4.0,2.0,30,25,90,80,95])
    w.writerow(["Regional",38900,19.4,4.2,2.2,31,26,91,81,95])
    w.writerow(["FBC",39200,19.6,4.5,2.5,32,27,92,82,96])
    w.writerow(["Franchisee",41200,20.8,5.0,3.0,35,30,93,85,97])

# ---- minimal dim stubs so all 8 tables load cleanly ----
owners=sorted({(s["owner"],s["region"],s["fbc"]) for s in S})
with open(os.path.join(OUT,"Dim_FranchiseOwner.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["FranchiseOwner","Region","StoreCount","Email","FBC"])
    for o,rg,fbc in owners: w.writerow([o,rg,3,f"{o.split()[0].lower()}@example.com",fbc])
with open(os.path.join(OUT,"Dim_Region.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["Region","RegionColor","SortOrder"])
    for i,rg in enumerate(sorted({s["region"] for s in S})): w.writerow([rg,"#0086EA",i+1])
with open(os.path.join(OUT,"Dim_Date.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["DateKey","Date","Year","Month","MonthName","Quarter","QuarterLabel","FiscalWeekKey","WeekOfYear","DayOfWeek","IsWeekStart"])
    for i,wk in enumerate(WEEKS):
        d=datetime.datetime.strptime(wk,"%d-%m-%Y").date()
        w.writerow([d.strftime("%Y%m%d"), d.isoformat(), 2025, d.month, d.strftime("%B"), "Q1","2025-Q1", wk, i+1, "Monday","True"])

print("Wrote", len(os.listdir(OUT)), "CSVs to", OUT, "| stores:", len(S), "| weeks:", len(WEEKS), "| latest:", LATEST)
