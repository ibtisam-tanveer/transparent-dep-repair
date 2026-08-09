# WHAT'S WRONG : ABCs moved to collections.abc; removed from collections in Py3.10
# EXPECTED ERR : ImportError: cannot import name 'Mapping' from 'collections'
# CORRECT FIX  : from collections.abc import Mapping
from collections import Mapping
print(Mapping)
