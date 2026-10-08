# Deliberately needs the same package a_missing_seaborn.py needs. Proves
# the repo agent shares ONE environment across files: by the time this
# file is discovered (sorted order puts it after a_missing_seaborn.py),
# seaborn is already installed because fixing that file installed it --
# this file should turn out "already passing", never needing its own fix.
import seaborn as sns

print(sns.__version__)
