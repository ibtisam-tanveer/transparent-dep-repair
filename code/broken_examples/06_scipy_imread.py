# WHAT'S WRONG : scipy.misc.imread was removed from SciPy
# EXPECTED ERR : ImportError: cannot import name 'imread' from 'scipy.misc'
# CORRECT FIX  : use imageio.imread, OR pin an old scipy
from scipy.misc import imread
print(imread)
