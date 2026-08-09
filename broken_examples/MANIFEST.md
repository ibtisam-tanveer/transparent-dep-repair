# Broken examples — a controlled test set

Eight small Python files, each broken in a **known** way by a real dependency
problem. Because you know the correct fix for each, you can check whether your
prototype gets it right. This is the set to develop and debug on before moving
to a big benchmark (EnvBench) or the GigaScience notebooks.

| File | What's wrong | Type of problem | Correct fix |
|------|--------------|-----------------|-------------|
| 01_missing_package.py | `seaborn` not installed | missing package | `pip install seaborn` |
| 02_numpy_float.py | `np.float` removed in NumPy 1.24 | removed API | use `float()` or pin `numpy<1.24` |
| 03_numpy_int_bool.py | `np.int` / `np.bool` removed in NumPy 1.24 | removed API | use `int()`/`bool()` or pin `numpy<1.24` |
| 04_sklearn_externals_joblib.py | `sklearn.externals.joblib` removed | moved package | `import joblib` |
| 05_pandas_append.py | `DataFrame.append()` removed in pandas 2.0 | removed API | use `pd.concat` or pin `pandas<2.0` |
| 06_scipy_imread.py | `scipy.misc.imread` removed | removed API | use `imageio.imread` |
| 07_collections_abc.py | ABCs removed from `collections` in Py 3.10 | moved API | `from collections.abc import Mapping` |
| 08_pyyaml_load.py | `yaml.load` needs a `Loader` since PyYAML 5.1 | changed API | `yaml.safe_load(...)` |

## Two kinds of problem, on purpose

- **Missing / moved package** (01, 04, 06) — often fixable by installing or
  changing an import. A rule-based brain can handle some of these.
- **Removed / changed API** (02, 03, 05, 07, 08) — the package is installed but
  the code uses something that no longer exists. These usually need the *LLM*
  step, because fixing them means understanding the code. Great for showing the
  limits of a simple approach and where your contribution adds value.

## Note on running them

Some files only reach their "interesting" error once the package is installed
(e.g. 02 needs numpy present, or you'll see a missing-package error first). That
is realistic: a tool often fixes one layer and reveals the next. Your loop should
handle exactly that.
