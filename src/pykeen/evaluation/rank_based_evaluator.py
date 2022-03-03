# -*- coding: utf-8 -*-

"""Implementation of ranked based evaluator."""

import itertools as itt
import logging
import math
import random
from collections import defaultdict
from typing import DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union, cast

import numpy as np
import pandas as pd
import torch

from .evaluator import Evaluator, MetricResults, prepare_filter_triples
from .metrics import (
    ADJUSTED_ARITHMETIC_MEAN_RANK,
    ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX,
    ARITHMETIC_MEAN_RANK,
    GEOMETRIC_MEAN_RANK,
    HARMONIC_MEAN_RANK,
    INVERSE_ARITHMETIC_MEAN_RANK,
    INVERSE_GEOMETRIC_MEAN_RANK,
    INVERSE_HARMONIC_MEAN_RANK,
    INVERSE_MEDIAN_RANK,
    MEDIAN_RANK,
    MetricKey,
    RANK_COUNT,
    RANK_MAD,
    RANK_STD,
    RANK_VARIANCE,
    get_macro_ranking_metrics,
    get_ranking_metrics,
)
from .ranks import Ranks
from .utils import MetricAnnotation, ValueRange
from ..constants import TARGET_TO_INDEX
from ..triples.triples_factory import CoreTriplesFactory
from ..typing import (
    EXPECTED_RANKS,
    ExtendedRankType,
    ExtendedTarget,
    LABEL_HEAD,
    LABEL_RELATION,
    LABEL_TAIL,
    MappedTriples,
    RANK_TYPES,
    RankType,
    SIDES,
    SIDE_BOTH,
    Target,
)

__all__ = [
    "RankBasedEvaluator",
    "RankBasedMetricResults",
]

logger = logging.getLogger(__name__)

RANKING_METRICS: Mapping[str, MetricAnnotation] = dict(
    arithmetic_mean_rank=MetricAnnotation(
        name="Mean Rank (MR)",
        increasing=False,
        value_range=ValueRange(lower=1.0, upper=None, lower_inclusive=True),
        description="The arithmetic mean over all ranks.",
        link="https://pykeen.readthedocs.io/en/stable/tutorial/understanding_evaluation.html#mean-rank",
    ),
    geometric_mean_rank=MetricAnnotation(
        name="Geometric Mean Rank (GMR)",
        increasing=False,
        value_range=ValueRange(lower=1.0, upper=None, lower_inclusive=True),
        description="The geometric mean over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    median_rank=MetricAnnotation(
        name="Median Rank",
        increasing=False,
        value_range=ValueRange(lower=1.0, upper=None, lower_inclusive=True),
        description="The median over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    harmonic_mean_rank=MetricAnnotation(
        name="Harmonic Mean Rank (HMR)",
        increasing=False,
        value_range=ValueRange(lower=1.0, upper=None, lower_inclusive=True),
        description="The harmonic mean over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    inverse_arithmetic_mean_rank=MetricAnnotation(
        name="Inverse Arithmetic Mean Rank (IAMR)",
        increasing=True,
        value_range=ValueRange(
            lower=0.0,
            upper=1.0,
            lower_inclusive=False,
            upper_inclusive=True,
        ),
        description="The inverse of the arithmetic mean over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    inverse_geometric_mean_rank=MetricAnnotation(
        name="Inverse Geometric Mean Rank (IGMR)",
        increasing=True,
        value_range=ValueRange(
            lower=0.0,
            upper=1.0,
            lower_inclusive=False,
            upper_inclusive=True,
        ),
        description="The inverse of the geometric mean over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    inverse_harmonic_mean_rank=MetricAnnotation(
        name="Mean Reciprocal Rank (MRR)",
        increasing=True,
        value_range=ValueRange(
            lower=0.0,
            upper=1.0,
            lower_inclusive=False,
            upper_inclusive=True,
        ),
        description="The inverse of the harmonic mean over all ranks.",
        link="https://en.wikipedia.org/wiki/Mean_reciprocal_rank",
    ),
    inverse_median_rank=MetricAnnotation(
        name="Inverse Median Rank",
        increasing=True,
        value_range=ValueRange(
            lower=0.0,
            upper=1.0,
            lower_inclusive=False,
            upper_inclusive=True,
        ),
        description="The inverse of the median over all ranks.",
        link="https://cthoyt.com/2021/04/19/pythagorean-mean-ranks.html",
    ),
    rank_count=MetricAnnotation(
        name="Rank Count",
        increasing=True,  # TODO check
        description="The number of considered ranks, a non-negative number. "
        "Low numbers may indicate unreliable results.",
        value_range=ValueRange(lower=1.0, upper=None, lower_inclusive=True),
        link="https://pykeen.readthedocs.io/en/stable/reference/evaluation.html",
    ),
    rank_std=MetricAnnotation(
        name="Rank Standard Deviation",
        value_range=ValueRange(lower=0.0, upper=None, lower_inclusive=True),
        increasing=False,
        description="The standard deviation over all ranks.",
        link="https://pykeen.readthedocs.io/en/stable/reference/evaluation.html",
    ),
    rank_var=MetricAnnotation(
        name="Rank Variance",
        value_range=ValueRange(lower=0.0, upper=None, lower_inclusive=True),
        increasing=False,
        description="The variance over all ranks.",
        link="https://pykeen.readthedocs.io/en/stable/reference/evaluation.html",
    ),
    rank_mad=MetricAnnotation(
        name="Rank Median Absolute Deviation",
        increasing=False,
        value_range=ValueRange(lower=0.0, upper=None, lower_inclusive=True),
        description="The median absolute deviation over all ranks.",
        link="https://pykeen.readthedocs.io/en/stable/reference/evaluation.html",
    ),
    hits_at_k=MetricAnnotation(
        name="Hits @ K",
        value_range=ValueRange(lower=0.0, upper=1.0, lower_inclusive=True, upper_inclusive=True),
        increasing=True,
        description="The relative frequency of ranks not larger than a given k.",
        link="https://pykeen.readthedocs.io/en/stable/tutorial/understanding_evaluation.html#hits-k",
    ),
    adjusted_arithmetic_mean_rank=MetricAnnotation(
        name="Adjusted Arithmetic Mean Rank (AAMR)",
        increasing=False,
        value_range=ValueRange(lower=0.0, upper=2.0, lower_inclusive=False, upper_inclusive=False),
        description="The mean over all chance-adjusted ranks.",
        link="https://arxiv.org/abs/2002.06914",
    ),
    adjusted_arithmetic_mean_rank_index=MetricAnnotation(
        name="Adjusted Arithmetic Mean Rank Index (AAMRI)",
        increasing=True,
        value_range=ValueRange(lower=-1, upper=1.0, lower_inclusive=True, upper_inclusive=True),
        description="The re-indexed adjusted mean rank (AAMR)",
        link="https://arxiv.org/abs/2002.06914",
    ),
)


class RankBasedMetricResults(MetricResults):
    """Results from computing metrics."""

    metrics = RANKING_METRICS

    @classmethod
    def from_dict(cls, **kwargs):
        """Create an instance from kwargs."""
        return cls(kwargs)

    def get_metric(self, name: str) -> float:
        """Get the rank-based metric.

        :param name: The name of the metric, created by concatenating three parts:

            1. The side (one of "head", "tail", or "both"). Most publications exclusively report "both".
            2. The type (one of "optimistic", "pessimistic", "realistic")
            3. The metric name ("adjusted_mean_rank_index", "adjusted_mean_rank", "mean_rank, "mean_reciprocal_rank",
               "inverse_geometric_mean_rank",
               or "hits@k" where k defaults to 10 but can be substituted for an integer. By default, 1, 3, 5, and 10
               are available. Other K's can be calculated by setting the appropriate variable in the
               ``evaluation_kwargs`` in the :func:`pykeen.pipeline.pipeline` or setting ``ks`` in the
               :class:`pykeen.evaluation.RankBasedEvaluator`.

            In general, all metrics are available for all combinations of sides/types except AMR and AMRI, which
            are only calculated for the average type. This is because the calculation of the expected MR in the
            optimistic and pessimistic case scenarios is still an active area of research and therefore has no
            implementation yet.
        :return: The value for the metric
        :raises ValueError: if an invalid name is given.

        Get the average MR

        >>> metric_results.get('both.realistic.mean_rank')

        If you only give a metric name, it assumes that it's for "both" sides and "realistic" type.

        >>> metric_results.get('adjusted_mean_rank_index')

        This function will do its best to infer what's going on if you only specify one part.

        >>> metric_results.get('left.mean_rank')
        >>> metric_results.get('optimistic.mean_rank')

        Get the default Hits @ K (where $k=10$)

        >>> metric_results.get('hits@k')

        Get a given Hits @ K

        >>> metric_results.get('hits@5')
        """
        return self._get_metric(MetricKey.lookup(name))

    def _get_metric(self, metric_key: MetricKey) -> float:
        if not metric_key.name.startswith("hits"):
            return self.data[metric_key.name][metric_key.side][metric_key.rank_type]
        assert metric_key.k is not None
        return self.data["hits_at_k"][metric_key.side][metric_key.rank_type][metric_key.k]

    def to_flat_dict(self):  # noqa: D102
        return {f"{side}.{rank_type}.{metric_name}": value for side, rank_type, metric_name, value in self._iter_rows()}

    def to_df(self) -> pd.DataFrame:
        """Output the metrics as a pandas dataframe."""
        return pd.DataFrame(list(self._iter_rows()), columns=["Side", "Type", "Metric", "Value"])

    def _iter_rows(self) -> Iterable[Tuple[ExtendedTarget, RankType, str, Union[float, int]]]:
        for metric, metric_data in self.data.items():
            for side, side_data in metric_data.items():
                for rank_type, rank_data in side_data.items():
                    # special treatment for hits_at_k
                    if metric == "hits_at_k":
                        for k, v in rank_data.items():
                            yield side, rank_type, f"hits_at_{k}", v
                    else:
                        yield side, rank_type, metric, rank_data


class RankBasedEvaluator(Evaluator):
    r"""A rank-based evaluator for KGE models.

    Calculates the following metrics:

    - Mean Rank (MR) with range $[1, \infty)$ where closer to 0 is better
    - Adjusted Mean Rank (AMR; [berrendorf2020]_) with range $(0, 2)$ where closer to 0 is better
    - Adjusted Mean Rank Index (AMRI; [berrendorf2020]_) with range $[-1, 1]$ where closer to 1 is better
    - Mean Reciprocal Rank (MRR) with range $(0, 1]$ where closer to 1 is better
    - Hits @ K with range $[0, 1]$ where closer to 1 is better.

    .. [berrendorf2020] Berrendorf, *et al.* (2020) `Interpretable and Fair
        Comparison of Link Prediction or Entity Alignment Methods with Adjusted Mean Rank
        <https://arxiv.org/abs/2002.06914>`_.
    """

    ks: Sequence[Union[int, float]]
    num_entities: Optional[int]
    ranks: Dict[Tuple[Target, ExtendedRankType], List[float]]

    def __init__(
        self,
        ks: Optional[Iterable[Union[int, float]]] = None,
        filtered: bool = True,
        **kwargs,
    ):
        """Initialize rank-based evaluator.

        :param ks:
            The values for which to calculate hits@k. Defaults to {1,3,5,10}.
        :param filtered:
            Whether to use the filtered evaluation protocol. If enabled, ranking another true triple higher than the
            currently considered one will not decrease the score.
        :param kwargs: Additional keyword arguments that are passed to the base class.
        """
        super().__init__(
            filtered=filtered,
            requires_positive_mask=False,
            **kwargs,
        )
        self.ks = tuple(ks) if ks is not None else (1, 3, 5, 10)
        for k in self.ks:
            if isinstance(k, float) and not (0 < k < 1):
                raise ValueError(
                    "If k is a float, it should represent a relative rank, i.e. a value between 0 and 1 (excl.)",
                )
        self.ranks = defaultdict(list)
        self.num_entities = None

    def process_scores_(
        self,
        hrt_batch: MappedTriples,
        target: Target,
        scores: torch.FloatTensor,
        true_scores: Optional[torch.FloatTensor] = None,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        if true_scores is None:
            raise ValueError(f"{self.__class__.__name__} needs the true scores!")

        batch_ranks = Ranks.from_scores(
            true_score=true_scores,
            all_scores=scores,
        )
        self.num_entities = scores.shape[1]
        for rank_type, v in batch_ranks.items():
            self.ranks[target, rank_type].extend(v.detach().cpu().tolist())

    def _get_ranks(self, side: ExtendedTarget, rank_type: ExtendedRankType) -> np.ndarray:
        if side == SIDE_BOTH:
            values: List[float] = sum(
                (self.ranks.get((_side, rank_type), []) for _side in (LABEL_HEAD, LABEL_TAIL)), []
            )
        else:
            values = self.ranks.get((cast(Target, side), rank_type), [])
        return np.asarray(values, dtype=np.float64)

    def finalize(self) -> RankBasedMetricResults:  # noqa: D102
        if self.num_entities is None:
            raise ValueError

        hits_at_k: DefaultDict[str, Dict[str, Dict[Union[int, float], float]]] = defaultdict(dict)
        asr: DefaultDict[str, DefaultDict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))

        for side, rank_type in itt.product(SIDES, RANK_TYPES):
            ranks = self._get_ranks(side=side, rank_type=rank_type)
            if len(ranks) < 1:
                continue
            hits_at_k[side][rank_type] = {
                k: np.mean(ranks <= (k if isinstance(k, int) else int(self.num_entities * k))).item() for k in self.ks
            }
            for metric_name, metric_value in get_ranking_metrics(ranks).items():
                asr[metric_name][side][rank_type] = metric_value

            expected_rank_type = EXPECTED_RANKS.get(rank_type)
            if expected_rank_type is not None:
                expected_ranks = self._get_ranks(side=side, rank_type=expected_rank_type)
                if 0 < len(expected_ranks):
                    # Adjusted mean rank calculation
                    expected_mean_rank = float(np.mean(expected_ranks))
                    asr[ADJUSTED_ARITHMETIC_MEAN_RANK][side][rank_type] = (
                        asr[ARITHMETIC_MEAN_RANK][side][rank_type] / expected_mean_rank
                    )
                    asr[ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX][side][rank_type] = 1.0 - (
                        asr[ARITHMETIC_MEAN_RANK][side][rank_type] - 1
                    ) / (expected_mean_rank - 1)

        # Clear buffers
        self.ranks.clear()

        return RankBasedMetricResults.from_dict(
            arithmetic_mean_rank=dict(asr[ARITHMETIC_MEAN_RANK]),
            geometric_mean_rank=dict(asr[GEOMETRIC_MEAN_RANK]),
            harmonic_mean_rank=dict(asr[HARMONIC_MEAN_RANK]),
            median_rank=dict(asr[MEDIAN_RANK]),
            inverse_arithmetic_mean_rank=dict(asr[INVERSE_ARITHMETIC_MEAN_RANK]),
            inverse_geometric_mean_rank=dict(asr[INVERSE_GEOMETRIC_MEAN_RANK]),
            inverse_harmonic_mean_rank=dict(asr[INVERSE_HARMONIC_MEAN_RANK]),
            inverse_median_rank=dict(asr[INVERSE_MEDIAN_RANK]),
            rank_count=dict(asr[RANK_COUNT]),  # type: ignore
            rank_std=dict(asr[RANK_STD]),
            rank_mad=dict(asr[RANK_MAD]),
            rank_var=dict(asr[RANK_VARIANCE]),
            adjusted_arithmetic_mean_rank=dict(asr[ADJUSTED_ARITHMETIC_MEAN_RANK]),
            adjusted_arithmetic_mean_rank_index=dict(asr[ADJUSTED_ARITHMETIC_MEAN_RANK_INDEX]),
            hits_at_k=dict(hits_at_k),
        )


def sample_negatives(
    evaluation_triples: MappedTriples,
    additional_filter_triples: Union[None, MappedTriples, List[MappedTriples]] = None,
    num_samples: int = 50,
    num_entities: Optional[int] = None,
) -> Mapping[Target, torch.FloatTensor]:
    """
    Sample true negatives for sampled evaluation.

    :param evaluation_triples: shape: (n, 3)
        the evaluation triples
    :param additional_filter_triples:
        additional true triples which are to be filtered
    :param num_samples: >0
        the number of samples
    :param num_entities:
        the number of entities

    :return:
        A mapping of sides to negative samples
    """
    additional_filter_triples = prepare_filter_triples(
        mapped_triples=evaluation_triples,
        additional_filter_triples=additional_filter_triples,
    )
    num_entities = num_entities or (additional_filter_triples[:, [0, 2]].max().item() + 1)
    columns = [LABEL_HEAD, LABEL_RELATION, LABEL_TAIL]
    num_triples = evaluation_triples.shape[0]
    df = pd.DataFrame(data=evaluation_triples.numpy(), columns=columns)
    all_df = pd.DataFrame(data=additional_filter_triples.numpy(), columns=columns)
    id_df = df.reset_index()
    all_ids = set(range(num_entities))
    negatives = {}
    for side in [LABEL_HEAD, LABEL_TAIL]:
        this_negatives = cast(torch.FloatTensor, torch.empty(size=(num_triples, num_samples), dtype=torch.long))
        other = [c for c in columns if c != side]
        for _, group in pd.merge(id_df, all_df, on=other, suffixes=["_eval", "_all"]).groupby(
            by=other,
        ):
            pool = list(all_ids.difference(group[f"{side}_all"].unique().tolist()))
            if len(pool) < num_samples:
                logger.warning(
                    f"There are less than num_samples={num_samples} candidates for side={side}, triples={group}.",
                )
                # repeat
                pool = int(math.ceil(num_samples / len(pool))) * pool
            for i in group["index"].unique():
                this_negatives[i, :] = torch.as_tensor(
                    data=random.sample(population=pool, k=num_samples),
                    dtype=torch.long,
                )
        negatives[side] = this_negatives
    return negatives


class SampledRankBasedEvaluator(RankBasedEvaluator):
    """
    A rank-based evaluator using sampled negatives instead of all negatives, cf. [teru2020]_.

    Notice that this evaluator yields optimistic estimations of the metrics evaluated on all entities,
    cf. https://arxiv.org/abs/2106.06935.
    """

    negatives: Mapping[Target, torch.LongTensor]

    def __init__(
        self,
        evaluation_factory: CoreTriplesFactory,
        *,
        additional_filter_triples: Union[None, MappedTriples, List[MappedTriples]] = None,
        num_negatives: Optional[int] = None,
        head_negatives: Optional[torch.LongTensor] = None,
        tail_negatives: Optional[torch.LongTensor] = None,
        **kwargs,
    ):
        """
        Initialize the evaluator.

        :param evaluation_factory:
            the factory with evaluation triples
        :param head_negatives: shape: (num_triples, num_negatives)
            the entity IDs of negative samples for head prediction for each evaluation triple
        :param tail_negatives: shape: (num_triples, num_negatives)
            the entity IDs of negative samples for tail prediction for each evaluation triple
        :param kwargs:
            additional keyword-based arguments passed to RankBasedEvaluator.__init__
        """
        super().__init__(**kwargs)
        if head_negatives is None and tail_negatives is None:
            # default for inductive LP by [teru2020]
            num_negatives = num_negatives or 50
            logger.info(
                f"Sampling {num_negatives} negatives for each of the "
                f"{evaluation_factory.num_triples} evaluation triples.",
            )
            if num_negatives > evaluation_factory.num_entities:
                raise ValueError("Cannot use more negative samples than there are entities.")
            negatives = sample_negatives(
                evaluation_triples=evaluation_factory.mapped_triples,
                additional_filter_triples=additional_filter_triples,
                num_entities=evaluation_factory.num_entities,
                num_samples=num_negatives,
            )
        elif head_negatives is None or tail_negatives is None:
            raise ValueError("Either both, head and tail negatives must be provided, or none.")
        else:
            negatives = {
                LABEL_HEAD: head_negatives,
                LABEL_TAIL: tail_negatives,
            }

        # verify input
        for side, side_negatives in negatives.items():
            if side_negatives.shape[0] != evaluation_factory.num_triples:
                raise ValueError(f"Negatives for {side} are in wrong shape: {side_negatives.shape}")
        self.triple_to_index = {(h, r, t): i for i, (h, r, t) in enumerate(evaluation_factory.mapped_triples.tolist())}
        self.negative_samples = negatives
        self.num_entities = evaluation_factory.num_entities

    def process_scores_(
        self,
        hrt_batch: MappedTriples,
        target: Target,
        scores: torch.FloatTensor,
        true_scores: Optional[torch.FloatTensor] = None,
        dense_positive_mask: Optional[torch.FloatTensor] = None,
    ) -> None:  # noqa: D102
        if true_scores is None:
            raise ValueError(f"{self.__class__.__name__} needs the true scores!")

        num_entities = scores.shape[1]
        # TODO: do not require to compute all scores beforehand
        triple_indices = [self.triple_to_index[h, r, t] for h, r, t in hrt_batch.cpu().tolist()]
        negative_entity_ids = self.negative_samples[target][triple_indices]
        negative_scores = scores[
            torch.arange(hrt_batch.shape[0], device=hrt_batch.device).unsqueeze(dim=-1),
            negative_entity_ids,
        ]
        # super.evaluation assumes that the true scores are part of all_scores
        scores = torch.cat([true_scores, negative_scores], dim=-1)
        super().process_scores_(
            hrt_batch=hrt_batch,
            target=target,
            scores=scores,
            true_scores=true_scores,
            dense_positive_mask=dense_positive_mask,
        )
        # write back correct num_entities
        # TODO: should we give num_entities in the constructor instead of inferring it every time ranks are processed?
        self.num_entities = num_entities


class MacroRankBasedEvaluator(RankBasedEvaluator):
    """Rank-based evaluation with macro averages."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.keys = defaultdict(list)

    def process_scores_(
        self,
        hrt_batch: MappedTriples,
        target: Target,
        **kwargs,
    ) -> None:  # noqa: D102
        super().process_scores_(hrt_batch=hrt_batch, target=target, **kwargs)
        idx = TARGET_TO_INDEX[target]
        key_ids = [i for i in range(hrt_batch.shape[1]) if i != idx]
        self.keys[target].extend(hrt_batch[:, key_ids].detach().cpu().tolist())

    def _get_keys(self, side: ExtendedTarget) -> np.ndarray:
        if side == SIDE_BOTH:
            # TODO: has to have same order as _get_ranks
            return np.concatenate(
                [self._get_keys(side=single_side) for single_side in (LABEL_HEAD, LABEL_TAIL)], axis=0
            )
        assert side in (LABEL_HEAD, LABEL_TAIL)
        keys = self.keys[side]
        if keys:
            return np.asarray(self.keys[side], dtype=int)
        return np.empty(shape=(0, 2), dtype=int)

    def finalize(self) -> RankBasedMetricResults:  # noqa: D102
        if self.num_entities is None:
            raise ValueError

        hits_at_k: DefaultDict[str, Dict[str, Dict[Union[int, float], float]]] = defaultdict(dict)
        asr: DefaultDict[str, DefaultDict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))

        for side in SIDES:
            # compute weights, s.t. sum per key, e.g., (h, r), == 1
            keys = self._get_keys(side=side)
            unique_inverse, unique_counts = np.unique(keys, return_counts=True, return_inverse=True, axis=0)[1:]
            unique_weights = np.reciprocal(unique_counts.astype(float))
            weights = unique_weights[unique_inverse]

            # aggregate different rank types
            for rank_type in RANK_TYPES:
                ranks = self._get_ranks(side=side, rank_type=rank_type)
                assert ranks.shape == weights.shape
                if len(ranks) < 1:
                    continue
                hits_at_k[side][rank_type] = {
                    k: np.average(
                        ranks <= (k if isinstance(k, int) else int(self.num_entities * k)), weights=weights
                    ).item()
                    for k in self.ks
                }
                for metric_name, metric_value in get_macro_ranking_metrics(ranks=ranks, weights=weights):
                    if isinstance(metric_value, np.ndarray):
                        metric_value = metric_value.item()
                    asr[metric_name][side][rank_type] = metric_value
        data = {key: dict(value) for key, value in asr.items()}
        data["hits_at_k"] = dict(hits_at_k)
        return RankBasedMetricResults.from_dict(**data)
