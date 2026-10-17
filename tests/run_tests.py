import importlib, inspect, sys, traceback

def run_module(modname):
    import pytest as _pytest
    try:
        mod = importlib.import_module(modname)
    except _pytest.Skipped as e:
        print(f"SKIPPED module {modname}: {e}")
        return 0, 0
    passed, failed = 0, 0
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_"):
            continue
        params = getattr(fn, "__pytest_params__", None)
        if params:
            names, argvalues = params
            for values in argvalues:
                if not isinstance(values, (tuple, list)):
                    values = (values,)
                kwargs = dict(zip(names, values))
                try:
                    fn(**kwargs)
                    passed += 1
                except Exception:
                    failed += 1
                    print(f"FAIL {modname}.{name}{kwargs}")
                    traceback.print_exc()
        else:
            try:
                fn()
                passed += 1
            except Exception:
                failed += 1
                print(f"FAIL {modname}.{name}")
                traceback.print_exc()
    return passed, failed

if __name__ == "__main__":
    total_p, total_f = 0, 0
    for modname in sys.argv[1:]:
        p, f = run_module(modname)
        total_p += p
        total_f += f
    print(f"\n==== {total_p} passed, {total_f} failed ====")
    sys.exit(1 if total_f else 0)