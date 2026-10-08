# WHAT'S WRONG : dict.has_key() was REMOVED in Python 3 (a Python 2-only method)
# EXPECTED ERR : AttributeError: 'dict' object has no attribute 'has_key'
# CORRECT FIX  : use `key in d` instead of `d.has_key(key)`
d = {"a": 1}
if d.has_key("a"):
    print("found")
