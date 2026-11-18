"""Statistics for the random-number benchmark. Standard library only."""
import itertools
import math
from functools import lru_cache


def uniform_mean_sd(k):
    """Mean and SD of a perfectly uniform pick from 1..k."""
    return (k + 1) / 2.0, math.sqrt((k * k - 1) / 12.0)


@lru_cache(maxsize=8192)
def expected_uniform_entropy(n, k):
    """Expected normalised plug-in entropy of n uniform draws from k values.

    The plug-in estimator is biased low when n is not much larger than k
    (200 draws from 1..1000 cannot exceed ln(200)/ln(1000) = 0.77), so every
    observed entropy is shown next to this reference instead of next to 1.0.
    Exact: E[H] = ln n - (k/n) * E[c ln c], with c ~ Binomial(n, 1/k).
    """
    if n <= 1 or k <= 1:
        return 0.0
    p = 1.0 / k
    log_p, log_q = math.log(p), math.log(1.0 - p)
    upper = min(n, int(n * p + 12 * math.sqrt(n * p * (1 - p)) + 30))
    lg_n1 = math.lgamma(n + 1)
    acc = 0.0
    for j in range(2, upper + 1):
        log_pmf = lg_n1 - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * log_p + (n - j) * log_q
        acc += math.exp(log_pmf) * j * math.log(j)
    return max(0.0, (math.log(n) - (k / n) * acc) / math.log(k))


def _gammaincc(a, x):
    """Regularised upper incomplete gamma Q(a, x)."""
    if x <= 0:
        return 1.0
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = 1.0 / a
        total = term
        denom = a
        for _ in range(2000):
            denom += 1
            term *= x / denom
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return min(1.0, max(0.0, 1.0 - total * math.exp(log_front)))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 2000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return min(1.0, max(0.0, h * math.exp(log_front)))


def chi2_sf(x, df):
    return _gammaincc(df / 2.0, x / 2.0)


def cell_stats(counts, k):
    """Summary of one distribution. counts maps number -> how often it was picked."""
    n = sum(counts.values())
    if n == 0:
        return {"n": 0}
    mean = sum(v * c for v, c in counts.items()) / n
    var = sum(c * (v - mean) ** 2 for v, c in counts.items()) / (n - 1) if n > 1 else 0.0
    mode, mode_count = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    h = -sum((c / n) * math.log(c / n) for c in counts.values())
    out = {
        "n": n,
        "mean": mean,
        "sd": math.sqrt(var),
        "mode": mode,
        "mode_share": mode_count / n,
        "entropy": h / math.log(k),
        "entropy_ref": expected_uniform_entropy(n, k),
        "distinct": len(counts),
        "chi2": None,
        "chi2_p": None,
    }
    expected = n / k
    if expected >= 5:  # the chi-square approximation is not valid below this
        chi2 = sum((counts.get(v, 0) - expected) ** 2 / expected for v in range(1, k + 1))
        out["chi2"] = chi2
        out["chi2_p"] = chi2_sf(chi2, k - 1)
    return out


def to_bins(counts, k):
    """Bin counts for display: at most 100 columns."""
    size = max(1, math.ceil(k / 100))
    bins = [0] * math.ceil(k / size)
    for v, c in counts.items():
        bins[(v - 1) // size] += c
    return bins, size


def jsd(a, b):
    """Jensen-Shannon divergence (base 2, 0..1) between two count dicts."""
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return None
    total = 0.0
    for key in set(a) | set(b):
        p = a.get(key, 0) / na
        q = b.get(key, 0) / nb
        m = (p + q) / 2
        if p:
            total += 0.5 * p * math.log2(p / m)
        if q:
            total += 0.5 * q * math.log2(q / m)
    return max(0.0, min(1.0, total))


def wasserstein(a, b, k):
    """Earth mover's distance between two count dicts over 1..k, scaled to 0..1.

    Unlike JSD it treats 6 and 7 as nearly the same answer.
    """
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb or k < 2:
        return None
    ca = cb = 0.0
    total = 0.0
    for v in range(1, k):
        ca += a.get(v, 0) / na
        cb += b.get(v, 0) / nb
        total += abs(ca - cb)
    return total / (k - 1)


def family_test(matrix, families):
    """Exact permutation test: are same-family models closer than cross-family ones?

    matrix[i][j] is a distance (None where there is no data). The statistic is
    mean(between) - mean(within); family labels are reassigned in every possible
    way that keeps the group sizes.
    """
    idx = [i for i in range(len(families)) if any(matrix[i][j] is not None for j in range(len(families)) if j != i)]
    labels = [families[i] for i in idx]
    groups = sorted(set(labels))
    if len(groups) != 2 or min(labels.count(g) for g in groups) < 2:
        return None

    def stat(assign):
        within, between = [], []
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                d = matrix[idx[x]][idx[y]]
                if d is None:
                    continue
                (within if assign[x] == assign[y] else between).append(d)
        if not within or not between:
            return None, None, None
        w, b = sum(within) / len(within), sum(between) / len(between)
        return b - w, w, b

    observed, within, between = stat(labels)
    if observed is None:
        return None
    size_a = labels.count(groups[0])
    extreme = total = 0
    for combo in itertools.combinations(range(len(idx)), size_a):
        chosen = set(combo)
        assign = [groups[0] if i in chosen else groups[1] for i in range(len(idx))]
        s, _, _ = stat(assign)
        if s is None:
            continue
        total += 1
        if s >= observed - 1e-12:
            extreme += 1
    return {"within": within, "between": between, "p": extreme / total, "splits": total, "models": len(idx)}
