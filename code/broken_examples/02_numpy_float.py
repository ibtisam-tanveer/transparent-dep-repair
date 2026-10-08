# WHAT'S WRONG : np.float was REMOVED in NumPy 1.24
# EXPECTED ERR : AttributeError: module 'numpy' has no attribute 'float'
# CORRECT FIX  : use built-in float(), OR pin numpy<1.24
import numpy as np
x = np.float(3.14)
print(x)
