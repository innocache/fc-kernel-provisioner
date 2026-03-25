#!/usr/bin/env python3
"""Generate a sample sales CSV for data analyst agent testing."""

import numpy as np
import pandas as pd

np.random.seed(42)
dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
products = ["Widget A", "Widget B", "Widget C", "Gadget X", "Gadget Y"]
regions = ["North", "South", "East", "West"]
rows = []
for d in dates:
    for p in np.random.choice(products, size=3, replace=False):
        r = np.random.choice(regions)
        units = np.random.randint(50, 500)
        rows.append({
            "date": d, "product": p, "region": r,
            "units": units, "revenue": round(units * np.random.uniform(30, 80), 2),
        })
df = pd.DataFrame(rows)
df.to_csv("test_sales.csv", index=False)
print(f"Generated {len(df)} rows → test_sales.csv")
