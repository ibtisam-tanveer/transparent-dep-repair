# WHAT'S WRONG : np.int / np.bool were REMOVED in NumPy 1.24
# EXPECTED ERR : AttributeError: module 'numpy' has no attribute 'int'
# CORRECT FIX  : use built-in int() / bool(), OR pin numpy<1.24
import numpy as np
print(np.int(5), np.bool(True))
