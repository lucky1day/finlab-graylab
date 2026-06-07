#!/usr/bin/env python3
"""Monthly prediction distribution stats for all tenors."""
import pandas as pd
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent

tenors = ["3y", "5y", "7y", "10y"]
configs = {
    "3y": "H=5 W=550 z252_lv120 vt=0",
    "5y": "H=5 W=756 z3_spr53_60 vt=1",
    "7y": "H=5 W=300 z2_bf vt=1",
    "10y": "H=5 W=240 ema60_z252_bf60 vt=1",
}

for tenor in tenors:
    path = ROOT / f"{tenor}_predictions.csv"
    if not path.exists():
        print(f"  {tenor}: file not found")
        continue

    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])

    # 3Y has 'pred' column (no vote), others have 'vote_pred'
    if "vote_pred" in df.columns:
        pred_col = "vote_pred"
    else:
        pred_col = "pred"
    true_col = "true_label"

    sep = "=" * 120
    print(sep)
    print(f"  {tenor.upper()} T+5 direction prediction | {configs[tenor]}")
    print(sep)

    hdr = (f"{'month':>9}  {'N':>4}  {'up_true':>4}  {'dn_true':>4}  "
           f"{'up_pred':>4}  {'dn_pred':>4}  {'accuracy':>16}  "
           f"{'up_precision':>18}  {'up_recall':>18}  "
           f"{'dn_precision':>18}  {'dn_recall':>18}")
    print(hdr)

    df["ym"] = df["date"].dt.to_period("M")

    monthly_accs = []
    total_correct = 0
    total_n = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    total_tn = 0

    for ym, g in df.groupby("ym"):
        n = len(g)
        true_up = int((g[true_col] == 1).sum())
        true_dn = int((g[true_col] == -1).sum())
        pred_up = int((g[pred_col] == 1).sum())
        pred_dn = int((g[pred_col] == -1).sum())

        tp = int(((g[pred_col] == 1) & (g[true_col] == 1)).sum())
        fp = int(((g[pred_col] == 1) & (g[true_col] == -1)).sum())
        fn = int(((g[pred_col] == -1) & (g[true_col] == 1)).sum())
        tn = int(((g[pred_col] == -1) & (g[true_col] == -1)).sum())

        correct = tp + tn
        acc = correct / n if n > 0 else 0

        up_prec = f"{tp}/{pred_up}={tp/pred_up*100:.1f}%" if pred_up > 0 else "N/A"
        up_rec = f"{tp}/{true_up}={tp/true_up*100:.1f}%" if true_up > 0 else "N/A"
        dn_prec = f"{tn}/{pred_dn}={tn/pred_dn*100:.1f}%" if pred_dn > 0 else "N/A"
        dn_rec = f"{tn}/{true_dn}={tn/true_dn*100:.1f}%" if true_dn > 0 else "N/A"

        acc_str = f"{acc*100:.2f}% ({correct}/{n})"

        print(f"{str(ym):>9}  {n:>4}  {true_up:>4}  {true_dn:>4}  "
              f"{pred_up:>4}  {pred_dn:>4}  {acc_str:>16}  "
              f"{up_prec:>18}  {up_rec:>18}  "
              f"{dn_prec:>18}  {dn_rec:>18}")

        monthly_accs.append(acc)
        total_correct += correct
        total_n += n
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_tn += tn

    print("-" * 120)
    print(f"  Overall accuracy (weighted):  {total_correct/total_n*100:.2f}% ({total_correct}/{total_n})")
    print(f"  Mean monthly accuracy:        {np.mean(monthly_accs)*100:.2f}%")
    print(f"  Best: {max(monthly_accs)*100:.2f}%  Worst: {min(monthly_accs)*100:.2f}%  Std: {np.std(monthly_accs)*100:.2f}%")

    pred_up_total = total_tp + total_fp
    pred_dn_total = total_tn + total_fn
    true_up_total = total_tp + total_fn
    true_dn_total = total_tn + total_fp

    if pred_up_total > 0:
        print(f"  Up precision:   {total_tp/pred_up_total*100:.2f}% ({total_tp}/{pred_up_total})")
    if true_up_total > 0:
        print(f"  Up recall:      {total_tp/true_up_total*100:.2f}% ({total_tp}/{true_up_total})")
    if pred_dn_total > 0:
        print(f"  Down precision: {total_tn/pred_dn_total*100:.2f}% ({total_tn}/{pred_dn_total})")
    if true_dn_total > 0:
        print(f"  Down recall:    {total_tn/true_dn_total*100:.2f}% ({total_tn}/{true_dn_total})")
    print()
