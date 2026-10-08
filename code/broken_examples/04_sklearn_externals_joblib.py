# WHAT'S WRONG : sklearn.externals.joblib was removed from scikit-learn
# EXPECTED ERR : ImportError / ModuleNotFoundError: sklearn.externals.joblib
# CORRECT FIX  : import joblib   (as a standalone package)
from sklearn.externals import joblib
print(joblib.__version__)
