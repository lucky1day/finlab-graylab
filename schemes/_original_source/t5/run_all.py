#!/usr/bin/env python3
"""Run all 4 prediction scripts sequentially (3Y/5Y/7Y/10Y)."""
import subprocess, sys, time

scripts = [
    ("3Y", "predict_3y.py"),
    ("5Y", "predict_5y.py"),
    ("7Y", "predict_7y.py"),
    ("10Y", "predict_10y.py"),
]

for tenor, script in scripts:
    print("\n" + "#" * 90)
    print("  Running %s: %s" % (tenor, script))
    print("#" * 90)
    t0 = time.time()
    ret = subprocess.run(
        [sys.executable, script, "--data", "data/daily_output.csv", "--n-jobs", "4"],
        cwd=__import__("pathlib").Path(__file__).resolve().parent,
    )
    elapsed = time.time() - t0
    status = "OK" if ret.returncode == 0 else "FAILED (code=%d)" % ret.returncode
    print("  [%s] %s in %.1f seconds" % (tenor, status, elapsed))

print("\n" + "=" * 90)
print("  ALL DONE. Check *_predictions.csv for output.")
print("=" * 90)
