"""Feature screening metrics: IC, AUC, Sharpe.

Compatible replacement for ic_screen() in bond_common.
Usage:
    from functools import partial
    bond_common.ic_screen = partial(feat_screen, metric="auc")
"""
import numpy as np

try:
    from numba import njit
except ImportError:
    def njit(*args, **kwargs):
        """在服务环境缺少 numba 时保留等价 Python 实现。"""
        if args and callable(args[0]):
            return args[0]

        def _wrap(function):
            return function

        return _wrap


@njit(cache=True)
def _ic_scores(fv, ll, n_feat):
    """IC = |Pearson(feature, label)|"""
    scores = np.zeros(n_feat)
    for j in range(n_feat):
        v = fv[:, j]
        mask = ~np.isnan(v)
        cnt = mask.sum()
        if cnt < 50:
            continue
        vv = v[mask]
        lm = ll[mask]
        vs = np.std(vv)
        if vs < 1e-12:
            continue
        ls = np.std(lm)
        if ls < 1e-12:
            continue
        vm = np.mean(vv)
        lmean = np.mean(lm)
        cov = 0.0
        for i in range(cnt):
            cov += (vv[i] - vm) * (lm[i] - lmean)
        cov /= cnt
        scores[j] = abs(cov / (vs * ls))
    return scores


@njit(cache=True)
def _auc_scores(fv, ll, n_feat):
    """AUC via rank-sum. ll must be 0/1 binary.
    Returns abs(AUC - 0.5) so both directions score high."""
    scores = np.zeros(n_feat)
    for j in range(n_feat):
        v = fv[:, j]
        mask = ~np.isnan(v)
        cnt = mask.sum()
        if cnt < 50:
            continue
        vv = v[mask]
        yy = ll[mask]
        n_pos = 0
        for i in range(cnt):
            if yy[i] > 0.5:
                n_pos += 1
        n_neg = cnt - n_pos
        if n_pos < 5 or n_neg < 5:
            continue
        order = np.argsort(vv)
        rank_sum = 0.0
        i = 0
        while i < cnt:
            k = i + 1
            while k < cnt and vv[order[k]] == vv[order[i]]:
                k += 1
            avg_rank = (i + 1 + k) / 2.0
            for m in range(i, k):
                if yy[order[m]] > 0.5:
                    rank_sum += avg_rank
            i = k
        auc = (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
        scores[j] = abs(auc - 0.5)
    return scores


@njit(cache=True)
def _sharpe_scores(fv, ll, n_feat):
    """Sharpe of sign(feature) as signal. Returns abs(sharpe)."""
    scores = np.zeros(n_feat)
    for j in range(n_feat):
        v = fv[:, j]
        mask = ~np.isnan(v)
        cnt = mask.sum()
        if cnt < 50:
            continue
        vv = v[mask]
        lm = ll[mask]
        n_active = 0
        pnl_sum = 0.0
        pnl_sq = 0.0
        for i in range(cnt):
            s = 0.0
            if vv[i] > 0:
                s = 1.0
            elif vv[i] < 0:
                s = -1.0
            if s != 0:
                p = s * lm[i]
                pnl_sum += p
                pnl_sq += p * p
                n_active += 1
        if n_active < 30:
            continue
        mean_pnl = pnl_sum / n_active
        var_pnl = pnl_sq / n_active - mean_pnl * mean_pnl
        if var_pnl < 1e-12:
            continue
        scores[j] = abs(mean_pnl / np.sqrt(var_pnl))
    return scores


def feat_screen(feat_vals, labels, n_self, mf_feat_cat, mf_col_names,
                top_self=50, top_mf=30, min_cats=6, *, metric="ic"):
    """Compatible replacement for ic_screen with configurable metric.

    Parameters
    ----------
    metric : str
        "ic" (Pearson correlation), "auc" (rank AUC), "sharpe" (signal Sharpe)
    """
    n_feat = feat_vals.shape[1]
    valid_rows = ~np.isnan(labels)
    fv = feat_vals[valid_rows]
    ll = labels[valid_rows]

    if np.std(ll) < 1e-12:
        return np.arange(min(top_self, n_self), dtype=np.intp), 0

    if metric == "ic":
        scores = _ic_scores(fv, ll, n_feat)
    elif metric == "auc":
        ll_bin = (ll > 0).astype(np.float64)
        scores = _auc_scores(fv, ll_bin, n_feat)
    elif metric == "sharpe":
        scores = _sharpe_scores(fv, ll, n_feat)
    else:
        raise ValueError(f"Unknown metric: {metric}")

    # --- selection logic (identical to original ic_screen) ---
    self_order = np.argsort(-scores[:n_self])
    sel_self = self_order[:min(top_self, len(self_order))]

    mf_scores = scores[n_self:]
    mf_order = np.argsort(-mf_scores)
    sel_mf: list = []
    cat_count: dict = {}
    for idx in mf_order:
        if idx >= len(mf_col_names):
            continue
        cat = mf_feat_cat.get(mf_col_names[idx], "other")
        if cat not in cat_count:
            cat_count[cat] = 0
        if cat_count[cat] < 4:
            sel_mf.append(idx)
            cat_count[cat] += 1
        if len(cat_count) >= min_cats and len(sel_mf) >= top_mf:
            break
    for idx in mf_order:
        if len(sel_mf) >= top_mf:
            break
        if idx not in sel_mf and idx < len(mf_col_names):
            sel_mf.append(idx)

    selected = np.sort(np.concatenate([
        sel_self.astype(np.intp),
        np.array(sel_mf, dtype=np.intp) + n_self]))
    n_cats = len(cat_count)
    return selected, n_cats
