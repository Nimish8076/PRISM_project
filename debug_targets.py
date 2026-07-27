from agent import _get_targets, METRICS
t = _get_targets()
print("Targets loaded:", t)
print()
for metric, cfg in METRICS.items():
    col = cfg["target_col"]
    print(f"{metric}: target_col='{col}' -> value =", t.get(col))