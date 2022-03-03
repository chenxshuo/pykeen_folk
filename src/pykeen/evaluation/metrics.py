# -*- coding: utf-8 -*-

"""Utilities for metrics."""

import itertools as itt
import re
from typing import Iterable, Mapping, NamedTuple, Optional, Tuple, Union, cast

import numpy as np
from scipy import stats

from ..typing import ExtendedRankType, ExtendedTarget, RANK_REALISTIC, RANK_TYPES, RANK_TYPE_SYNONYMS, SIDES, SIDE_BOTH

__all__ = [
    "MetricKey",
]

ARITHMETIC_MEAN_RANK = "arithmetic_mean_rank"  # also known as mean rank (MR)
GEOMETRIC_MEAN_RANK = "geometric_mean_rank"
HARMONIC_MEAN_RANK = "harmonic_mean_rank"
MEDIAN_RANK = "median_rank"
INVERSE_ARITHMETIC_MEAN_RANK = "inverse_arithmetic_mean_rank"
INVERSE_GEOMETRIC_MEAN_RANK = "inverse_geometric_mean_rank"
INVERSE_HARMONIC_MEAN_RANK = "inverse_harmonic_mean_rank"  # also known as mean reciprocal rank (MRR)
INVERSE_MEDIAN_RANK = "inverse_median_rank"
ADJUSTED_ARITHMETIC_MEAN_RANK = "adjusted_arithmetic_mean_rank"
ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX = "adjusted_arithmetic_mean_rank_index"
RANK_STD = "rank_std"
RANK_VARIANCE = "rank_var"
RANK_MAD = "rank_mad"
RANK_COUNT = "rank_count"
TYPES_REALISTIC_ONLY = {ADJUSTED_ARITHMETIC_MEAN_RANK, ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX}
METRIC_SYNONYMS = {
    "adjusted_mean_rank": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "adjusted_mean_rank_index": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    "amr": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "aamr": ADJUSTED_ARITHMETIC_MEAN_RANK,
    "amri": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    "aamri": ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    "igmr": INVERSE_GEOMETRIC_MEAN_RANK,
    "iamr": INVERSE_ARITHMETIC_MEAN_RANK,
    "mr": ARITHMETIC_MEAN_RANK,
    "mean_rank": ARITHMETIC_MEAN_RANK,
    "mrr": INVERSE_HARMONIC_MEAN_RANK,
    "mean_reciprocal_rank": INVERSE_HARMONIC_MEAN_RANK,
}

_SIDE_PATTERN = "|".join(SIDES)
_TYPE_PATTERN = "|".join(itt.chain(RANK_TYPES, RANK_TYPE_SYNONYMS.keys()))
METRIC_PATTERN = re.compile(
    rf"(?P<name>[\w@]+)(\.(?P<side>{_SIDE_PATTERN}))?(\.(?P<type>{_TYPE_PATTERN}))?(\.(?P<k>\d+))?",
)
HITS_PATTERN = re.compile(r"(hits_at_|hits@|h@)(?P<k>\d+)")


class MetricKey(NamedTuple):
    """A key for the kind of metric to resolve."""

    #: Name of the metric
    name: str
    #: Side of the metric, or "both"
    side: ExtendedTarget
    #: The rank type
    rank_type: ExtendedRankType
    #: The k if this represents a hits at k metric
    k: Optional[int]

    def __str__(self) -> str:  # noqa: D105
        components = [self.name, self.side, self.rank_type]
        if self.k:
            components.append(str(self.k))
        return ".".join(components)

    @classmethod
    def lookup(cls, s: str) -> "MetricKey":
        """Functional metric name normalization."""
        match = METRIC_PATTERN.match(s)
        if not match:
            raise ValueError(f"Invalid metric name: {s}")
        k: Union[None, str, int]
        name, side, rank_type, k = [match.group(key) for key in ("name", "side", "type", "k")]

        # normalize metric name
        if not name:
            raise ValueError("A metric name must be provided.")
        # handle spaces and case
        name = name.lower().replace(" ", "_")

        # special case for hits_at_k
        match = HITS_PATTERN.match(name)
        if match:
            name = "hits_at_k"
            k = match.group("k")
        if name == "hits_at_k":
            if k is None:
                k = 10
            # TODO: Fractional?
            try:
                k = int(k)
            except ValueError as error:
                raise ValueError(f"Invalid k={k} for hits_at_k") from error
            if k < 0:
                raise ValueError(f"For hits_at_k, you must provide a positive value of k, but found {k}.")
        assert k is None or isinstance(k, int)

        # synonym normalization
        name = METRIC_SYNONYMS.get(name, name)

        # normalize side
        side = side or SIDE_BOTH
        side = side.lower()
        if side not in SIDES:
            raise ValueError(f"Invalid side: {side}. Allowed are {SIDES}.")

        # normalize rank type
        rank_type = rank_type or RANK_REALISTIC
        rank_type = rank_type.lower()
        rank_type = RANK_TYPE_SYNONYMS.get(rank_type, rank_type)
        if rank_type not in RANK_TYPES:
            raise ValueError(f"Invalid rank type: {rank_type}. Allowed are {RANK_TYPES}.")
        elif rank_type != RANK_REALISTIC and name in TYPES_REALISTIC_ONLY:
            raise ValueError(f"Invalid rank type for {name}: {rank_type}. Allowed type: {RANK_REALISTIC}")

        return cls(name, cast(ExtendedTarget, side), cast(ExtendedRankType, rank_type), k)

    @classmethod
    def normalize(cls, s: str) -> str:
        """Normalize a metric key string."""
        return str(cls.lookup(s))


ALL_TYPE_FUNCS = {
    ARITHMETIC_MEAN_RANK: np.mean,  # This is MR
    HARMONIC_MEAN_RANK: stats.hmean,
    GEOMETRIC_MEAN_RANK: stats.gmean,
    MEDIAN_RANK: np.median,
    INVERSE_ARITHMETIC_MEAN_RANK: lambda x: np.reciprocal(np.mean(x)),
    INVERSE_GEOMETRIC_MEAN_RANK: lambda x: np.reciprocal(stats.gmean(x)),
    INVERSE_HARMONIC_MEAN_RANK: lambda x: np.reciprocal(stats.hmean(x)),  # This is MRR
    INVERSE_MEDIAN_RANK: lambda x: np.reciprocal(np.median(x)),
    # Extra stats stuff
    RANK_STD: np.std,
    RANK_VARIANCE: np.var,
    RANK_MAD: stats.median_abs_deviation,
    RANK_COUNT: lambda x: np.asarray(x.size),
}


def get_ranking_metrics(ranks: np.ndarray) -> Mapping[str, float]:
    """Calculate all rank-based metrics."""
    rv = {}
    for metric_name, metric_func in ALL_TYPE_FUNCS.items():
        rv[metric_name] = metric_func(ranks).item()
    return rv


def weighted_median(a: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Calculate weighted median."""
    indices = np.argsort(a)
    s_ranks = a[indices]
    s_weights = weights[indices]
    cum_sum = np.cumsum(np.r_[0, s_weights])
    cum_sum /= cum_sum[-1]
    idx = np.searchsorted(cum_sum, v=0.5)
    return s_ranks[idx]


def get_macro_ranking_metrics(ranks: np.ndarray, weights: np.ndarray) -> Iterable[Tuple[str, float]]:
    """Calculate all macro rank-based metrics."""
    mean = np.average(ranks, weights=weights)
    yield ARITHMETIC_MEAN_RANK, mean
    yield GEOMETRIC_MEAN_RANK, stats.gmean(ranks, weights=weights)
    # TODO: HARMONIC_MEAN_RANK
    yield HARMONIC_MEAN_RANK, float("nan")
    median = weighted_median(a=ranks, weights=weights)
    yield MEDIAN_RANK, median
    yield INVERSE_ARITHMETIC_MEAN_RANK, np.reciprocal(mean)
    yield INVERSE_GEOMETRIC_MEAN_RANK, np.reciprocal(stats.gmean(ranks, weights=weights))
    # TODO: INVERSE_HARMONIC_MEAN_RANK
    yield INVERSE_HARMONIC_MEAN_RANK, float("nan")
    yield INVERSE_MEDIAN_RANK, np.reciprocal(median)
    variance = np.average((ranks - mean) ** 2.0, weights=weights)
    yield RANK_STD, np.sqrt(variance)
    yield RANK_VARIANCE, variance
    # TODO: RANK_MAD
    yield RANK_MAD, float("nan")
    yield RANK_COUNT, np.asarray(ranks.size)
