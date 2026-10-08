# WHAT'S WRONG : DataFrame.append() was REMOVED in pandas 2.0
# EXPECTED ERR : AttributeError: 'DataFrame' object has no attribute 'append'
# CORRECT FIX  : use pd.concat([...]), OR pin pandas<2.0
import pandas as pd
df = pd.DataFrame({"a": [1, 2]})
df = df.append({"a": 3}, ignore_index=True)
print(df)
