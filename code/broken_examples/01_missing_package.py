# WHAT'S WRONG : imports a package that is not installed
# EXPECTED ERR : ModuleNotFoundError: No module named 'seaborn'
# CORRECT FIX  : pip install seaborn
import seaborn as sns
print(sns.__version__)
