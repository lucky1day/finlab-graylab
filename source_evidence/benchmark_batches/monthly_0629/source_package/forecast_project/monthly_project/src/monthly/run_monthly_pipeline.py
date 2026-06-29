# Auto-generated encrypted entry shim.
from importlib import import_module as _import_module

if __package__:
    _impl = _import_module("._run_monthly_pipeline_impl", __package__)
else:
    _impl = _import_module("_run_monthly_pipeline_impl")

for _name, _value in vars(_impl).items():
    if not (_name.startswith("__") and _name.endswith("__")):
        globals()[_name] = _value

del _name, _value

if __name__ == "__main__":
    main()
