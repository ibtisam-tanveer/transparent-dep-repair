# WHAT'S WRONG : yaml.load without a Loader was disabled in PyYAML 5.1+
# EXPECTED ERR : TypeError: load() missing 1 required positional argument: 'Loader'
# CORRECT FIX  : yaml.safe_load(text), OR yaml.load(text, Loader=yaml.SafeLoader)
import yaml
data = yaml.load("a: 1")
print(data)
