"""W4 二进制封装交付：确定性生成、策略合规、无数据库运行、方向映射、请求顺序无关、失败不写部分结果。"""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tarfile
import io
import lzma
import base64
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from shared.blackbox_v2._script_policy import validate_delivery_script
from shared.blackbox_v2.contracts import load_metadata
from shared.data_bridge.raw_wind import RAW_WIND_DATA_FILENAMES, export_raw_wind, read_raw_wind_frame
from tests.test_raw_wind_export import _engine

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "blackbox_v2" / "w4_wrapper"
SCHEME_ID = "wrapped_stub_scheme"
METADATA = {
    "schema_version": "1.0", "name": "stub", "owner": "platform", "description": "stub wrapped binary",
    "algorithm_version": "stub.1", "target_tenor": "5Y", "task_type": "T+1", "horizon": 1,
    "target_rule": "target_date_yield_vs_feature_date_yield", "input_profile": "raw_wind_v1",
}
SECRET = "SECRET-PASSWORD-123"

STUB_RUN_DAILY = '''
import argparse, csv, os
from pathlib import Path
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from daily import state

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_date"); parser.add_argument("--project-root"); parser.add_argument("--n-jobs")
    parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--frequencies")
    args = parser.parse_args()
    assert args.dry_run and os.environ.get("DRY_RUN") == "1"
    engine = create_engine(URL.create("mysql+pymysql", username="root", password="x", host="localhost", database="bond_db"))
    calendar = pd.read_sql("SELECT rdate, trade_flag FROM t_trade_calendar ORDER BY rdate", engine)
    trade = calendar[calendar.trade_flag.astype(str) == "1"].rdate.tolist()
    prior = max(day for day in trade if day < args.run_date)
    values = pd.read_sql(
        "SELECT rdate, indicators_code, indicators_value FROM api_wind_daily WHERE indicators_code IN (%s, %s) AND indicators_value IS NOT NULL AND rdate <= %s",
        engine, params=["A", "B", prior])
    score = float(values[values.indicators_code == "A"].indicators_value.sum())
    state.calls += 1  # 模块级状态：外壳必须在每个请求后清除，否则第二次结果会翻转
    direction = (1 if score > 0.15 else -1) * (1 if state.calls == 1 else -1)
    out = Path(args.project_root) / "daily_project" / "output" / args.run_date / "prediction"
    out.mkdir(parents=True)
    (out.parent / "bulky.bin").write_bytes(b"x" * 1_000_000)
    with (out / "daily_selected_prediction_rows.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rdate", "frequency", "prediction_date", "expected_prediction_date", "pred_label"])
        writer.writerow([args.run_date, args.frequencies, prior, prior, direction])
        writer.writerow([args.run_date, "OTHER", prior, prior, 0])
    print("this stdout must not leak")

if __name__ == "__main__":
    main()
'''


def _stub_package(root: Path) -> Path:
    package = root / "forecast_project"
    (package / "daily_project" / "src" / "daily").mkdir(parents=True)
    (package / "daily_project" / "src" / "daily" / "__init__.py").write_text("")
    (package / "daily_project" / "src" / "daily" / "state.py").write_text("calls = 0\n")
    (package / "daily_project" / "src" / "daily" / "run_daily.py").write_text(STUB_RUN_DAILY)
    (package / "db_config.py").write_text(f'DB_CONFIG = {{"user": "root", "password": "{SECRET}"}}\n')
    (package / "run.sh").write_text("#!/bin/sh\n")
    (package / "run.sh").chmod(0o755)
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "junk.pyc").write_bytes(b"junk")
    return package


def _build(tmp_path: Path, **overrides):
    from deploy.blackbox_v2.w4_wrapper.build_delivery import build_delivery

    return build_delivery(
        source_package=_stub_package(tmp_path / "src"), scheme_id=SCHEME_ID, frequency="D5Y",
        source_identity="a" * 64, metadata=METADATA, output_dir=tmp_path / "out", **overrides,
    )


def _data_dir(tmp_path: Path) -> Path:
    engine = _engine()
    with engine.begin() as c:
        c.execute(text("DELETE FROM api_wind_daily"))
        c.execute(text("DELETE FROM t_trade_calendar"))
        c.execute(text(
            "INSERT INTO api_wind_daily VALUES (1,'2026-11-02','A',0.1,'x'),(2,'2026-11-03','A',0.1,'x'),(3,'2026-11-04','A',0.1,'x'),"
            "(4,'2026-11-02','B',9.0,'x'),(5,'2026-11-05','A',50.0,'x')"))
        c.execute(text("INSERT INTO t_trade_calendar VALUES ('2026-11-01','0'),('2026-11-02','1'),('2026-11-03','1'),('2026-11-04','1'),('2026-11-05','1')"))
    with engine.connect() as connection:
        artifact = export_raw_wind(connection, tmp_path / "artifact", generation_id="g", refresh_date="2026-11-05")
    data = tmp_path / "data"
    data.mkdir()
    for name in RAW_WIND_DATA_FILENAMES:
        (data / name).write_bytes((artifact.path / name).read_bytes())
    return data


def _run(script: Path, args: list[str], run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {"TZ": "Asia/Shanghai", "LANG": "C.UTF-8", "HOME": str(run_dir), "TMPDIR": str(run_dir), "PATH": str(Path(sys.executable).parent)}
    return subprocess.run([sys.executable, "-I", str(script), *args], cwd=run_dir, env=env, capture_output=True, text=True, timeout=300)


def _request(request_id: str, feature: str, target: str) -> dict:
    return {"request_id": request_id, "predict_date": feature, "feature_date": feature, "target_date": target,
            "daily_cutoff_key": feature, "weekly_cutoff_key": "202645", "monthly_cutoff_key": "202611"}


def test_generation_is_deterministic_policy_clean_and_credential_free(tmp_path):
    script, document = _build(tmp_path)
    second, _ = _build(tmp_path / "again")

    assert script.read_bytes() == second.read_bytes()
    validate_delivery_script(script)
    assert load_metadata(document).input_profile == "raw_wind_v1"
    assert SECRET not in script.read_text()
    module = _load_template(script)
    archive = lzma.decompress(base64.b64decode("".join(module.PAYLOAD_B64.split())))
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        names = handle.getnames()
        config = handle.extractfile("forecast_project/db_config.py").read().decode()
        mode = handle.getmember("forecast_project/run.sh").mode
    assert "forecast_project/__pycache__/junk.pyc" not in names and SECRET not in config and "DB_CONFIG" in config
    assert mode == 0o755 and all(m.mtime == 0 and m.uid == 0 for m in tarfile.open(fileobj=io.BytesIO(archive)).getmembers())


def _load_template(path: Path):
    spec = importlib.util.spec_from_file_location("w4_shell_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_predict_runs_without_a_database_and_leaves_a_clean_run_directory(tmp_path):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request("r1", "2026-11-03", "2026-11-04")))
    run_dir = tmp_path / "run"

    result = _run(script, ["predict", "--request", str(request), "--data-dir", str(data), "--output", str(run_dir / "out.json")], run_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # 封装算法的 stdout 不得泄漏
    assert json.loads((run_dir / "out.json").read_text()) == {
        "request_id": "r1", "predict_date": "2026-11-03", "feature_date": "2026-11-03", "target_date": "2026-11-04", "predicted_direction": 1,
    }
    assert [p.name for p in run_dir.iterdir()] == ["out.json"]  # 二进制包与运行产物全部清除


def test_backtest_matches_single_predictions_and_is_independent_of_request_order(tmp_path):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    requests = [_request("a", "2026-11-02", "2026-11-03"), _request("b", "2026-11-03", "2026-11-04"), _request("c", "2026-11-04", "2026-11-05")]

    def backtest(order):
        path = tmp_path / f"requests-{''.join(r['request_id'] for r in order)}.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(requests[0]))
            writer.writeheader()
            writer.writerows(order)
        run_dir = tmp_path / f"bt-{path.stem}"
        outcome = _run(script, ["backtest", "--requests", str(path), "--data-dir", str(data), "--output", str(run_dir / "bt.csv")], run_dir)
        assert outcome.returncode == 0, outcome.stderr
        rows = list(csv.DictReader((run_dir / "bt.csv").open()))
        assert [r["request_id"] for r in rows] == [r["request_id"] for r in order]
        return {r["request_id"]: r["predicted_direction"] for r in rows}

    forward, reverse = backtest(requests), backtest(list(reversed(requests)))
    assert forward == reverse == {"a": "-1", "b": "1", "c": "1"}


@pytest.mark.parametrize("case", ["non_trading_day", "tampered_data", "bad_dates", "unknown_field"])
def test_failures_exit_non_zero_without_partial_output(tmp_path, case):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    request = _request("r1", "2026-11-03", "2026-11-04")
    if case == "non_trading_day":
        request = _request("r1", "2026-10-30", "2026-11-01")
    elif case == "tampered_data":
        target = data / "raw_wind__api_wind_daily.csv"
        target.write_text(target.read_text().replace("2026-11-02,B,9.0", "2026-11-02,B,9.1"))
    elif case == "bad_dates":
        request["target_date"] = "2026-11-02"
    else:
        request["extra"] = "x"
    (tmp_path / "request.json").write_text(json.dumps(request))
    run_dir = tmp_path / "run"

    result = _run(script, ["predict", "--request", str(tmp_path / "request.json"), "--data-dir", str(data), "--output", str(run_dir / "out.json")], run_dir)

    assert result.returncode != 0 and result.stderr
    assert not (run_dir / "out.json").exists()


def test_source_date_context_mismatch_is_rejected(tmp_path):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    request = _request("r1", "2026-11-02", "2026-11-04")  # 特征日与源行 prediction_date（前一交易日 11-03）不一致
    (tmp_path / "request.json").write_text(json.dumps(request))
    run_dir = tmp_path / "run"

    result = _run(script, ["predict", "--request", str(tmp_path / "request.json"), "--data-dir", str(data), "--output", str(run_dir / "out.json")], run_dir)

    assert result.returncode != 0 and "differs from feature_date" in result.stderr


def test_loaded_tables_equal_the_platform_reference_reader_and_unknown_queries_fail_closed(tmp_path):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    module = _load_template(script)

    tables = module.load_raw_tables(data)
    from shared.data_bridge import raw_wind

    artifact_dir = next((tmp_path / "artifact").iterdir())
    for name, frame in tables.items():
        pd.testing.assert_frame_equal(frame, read_raw_wind_frame(artifact_dir, name), check_dtype=True, check_exact=True)

    answer = module._Answerer(tables)
    with pytest.raises(module.ShellError, match="database access is disabled"):
        answer("SELECT * FROM t_scheme_runs")
    with pytest.raises(module.ShellError, match="unsupported WHERE"):
        answer("SELECT rdate FROM api_wind_daily WHERE indicators_value > 1 OR rdate = 'x'")
    with pytest.raises(AttributeError, match="database access is disabled"):
        module._DisabledModule("sqlalchemy").text
    assert raw_wind.RAW_WIND_FILENAMES  # 模块可用性校验


def test_consecutive_requests_in_one_process_do_not_share_binary_state(tmp_path):
    script, _ = _build(tmp_path)
    data = _data_dir(tmp_path)
    driver = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('shell', sys.argv[1]); shell = importlib.util.module_from_spec(spec); spec.loader.exec_module(shell)\n"
        "from pathlib import Path\n"
        "runner = shell.Runner(Path(sys.argv[3]) / 'w4', Path(sys.argv[2]))\n"
        "req = {'request_id': 'r', 'predict_date': '2026-11-03', 'feature_date': '2026-11-03', 'target_date': '2026-11-04',"
        " 'daily_cutoff_key': 'x', 'weekly_cutoff_key': 'x', 'monthly_cutoff_key': 'x'}\n"
        "print(json.dumps([runner.run(req), runner.run(req), runner.run(req)]))\n"
    )
    run_dir = tmp_path / "drv"
    run_dir.mkdir()
    env = {"TZ": "Asia/Shanghai", "HOME": str(run_dir), "TMPDIR": str(run_dir), "PATH": str(Path(sys.executable).parent)}
    outcome = subprocess.run([sys.executable, "-I", "-c", driver, str(script), str(data), str(run_dir)], cwd=run_dir, env=env, capture_output=True, text=True, timeout=300)

    assert outcome.returncode == 0, outcome.stderr
    assert json.loads(outcome.stdout) == [1, 1, 1]  # 若模块状态被保留，第二次会翻转为 -1
