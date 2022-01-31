# -*- coding: utf-8 -*-

"""Base module for all KGE models."""

from __future__ import annotations

import functools
import inspect
import itertools
import logging
import os
import pickle
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, ClassVar, Collection, Iterable, Mapping, Optional, Sequence, Tuple, Type, Union

import pandas as pd
import torch
from class_resolver import HintOrType
from docdata import parse_docdata
from torch import nn

from .inverse import RelationInverter, relation_inverter_resolver
from ..losses import Loss, MarginRankingLoss, loss_resolver
from ..nn import Embedding, EmbeddingSpecification, RepresentationModule
from ..regularizers import NoRegularizer, Regularizer
from ..triples import CoreTriplesFactory
from ..typing import ScorePack
from ..utils import NoRandomSeedNecessary, extend_batch, set_random_seed

__all__ = [
    "Model",
    "_OldAbstractModel",
    "EntityRelationEmbeddingModel",
]

logger = logging.getLogger(__name__)


def _resolve_num(
    triples_factory: Optional[CoreTriplesFactory],
    num_entities: Optional[int],
    num_relations: Optional[int],
) -> Tuple[int, int]:
    if triples_factory is None:
        if num_entities is None or num_relations is None:
            raise ValueError("If no triples factory is provided, num_entities and num_relations must be provided.")
    else:
        num_entities = num_entities or triples_factory.num_entities
        if num_entities != triples_factory.num_entities:
            raise ValueError(
                f"Inconsistent number of entities between {triples_factory} and num_entities={num_entities}",
            )
        num_relations = num_relations or triples_factory.num_relations
        if num_relations != triples_factory.num_relations:
            raise ValueError(
                f"Inconsistent number of relations between {triples_factory} and num_relations={num_relations}",
            )
    return num_entities, num_relations


class Model(nn.Module, ABC):
    """A base module for KGE models.

    Subclasses of :class:`Model` can decide however they want on how to store entities' and
    relations' representations, how they want to be looked up, and how they should
    be scored. The :class:`OModel` provides a commonly used interface for models storing entity
    and relation representations in the form of :class:`pykeen.nn.Embedding`.
    """

    #: The default strategy for optimizing the model's hyper-parameters
    hpo_default: ClassVar[Mapping[str, Any]]

    _random_seed: Optional[int]

    #: The default loss function class
    loss_default: ClassVar[Type[Loss]] = MarginRankingLoss
    #: The default parameters for the default loss function class
    loss_default_kwargs: ClassVar[Optional[Mapping[str, Any]]] = dict(margin=1.0, reduction="mean")
    #: The instance of the loss
    loss: Loss

    #: the number of entities
    num_entities: int

    #: the number of relations
    num_relations: int

    #: whether to use inverse relations
    use_inverse_relations: bool
    relation_inverter: RelationInverter

    can_slice_h: ClassVar[bool]
    can_slice_r: ClassVar[bool]
    can_slice_t: ClassVar[bool]

    def __init__(
        self,
        *,
        triples_factory: Optional[CoreTriplesFactory],
        loss: HintOrType[Loss] = None,
        loss_kwargs: Optional[Mapping[str, Any]] = None,
        predict_with_sigmoid: bool = False,
        random_seed: Optional[int] = None,
        num_entities: Optional[int] = None,
        num_relations: Optional[int] = None,
        use_inverse_relations: bool = False,
    ) -> None:
        """Initialize the module.

        :param triples_factory:
            The triples factory facilitates access to the dataset.
        :param loss:
            The loss to use. If None is given, use the loss default specific to the model subclass.
        :param predict_with_sigmoid:
            Whether to apply sigmoid onto the scores when predicting scores. Applying sigmoid at prediction time may
            lead to exactly equal scores for certain triples with very high, or very low score. When not trained with
            applying sigmoid (or using BCEWithLogitsLoss), the scores are not calibrated to perform well with sigmoid.
        :param random_seed:
            A random seed to use for initialising the model's weights. **Should** be set when aiming at reproducibility.
        :param use_inverse_relations:
            whether to use the inverse relations modelling technique
        """
        super().__init__()

        # Random seeds have to set before the embeddings are initialized
        if random_seed is None:
            logger.warning("No random seed is specified. This may lead to non-reproducible results.")
            self._random_seed = None
        elif random_seed is not NoRandomSeedNecessary:
            set_random_seed(random_seed)
            self._random_seed = random_seed

        # Loss
        if loss is None:
            self.loss = self.loss_default(**(self.loss_default_kwargs or {}))
        else:
            self.loss = loss_resolver.make(loss, pos_kwargs=loss_kwargs)

        self.num_entities, self.num_relations = _resolve_num(
            triples_factory=triples_factory,
            num_entities=num_entities,
            num_relations=num_relations,
        )
        self.use_inverse_relations = use_inverse_relations
        self.relation_inverter = relation_inverter_resolver.make(num_relations=num_relations)

        """
        When predict_with_sigmoid is set to True, the sigmoid function is applied to the logits during evaluation and
        also for predictions after training, but has no effect on the training.
        """
        self.predict_with_sigmoid = predict_with_sigmoid

    @property
    def effective_num_relations(self) -> int:
        """Return the effective number of relations, i.e., including inverse relations."""
        if self.use_inverse_relations:
            return 2 * self.num_relations
        return self.num_relations

    def __init_subclass__(cls, **kwargs):
        """Initialize the subclass.

        This checks for all subclasses if they are tagged with :class:`abc.ABC` with :func:`inspect.isabstract`.
        All non-abstract deriving models should have citation information. Subclasses can further override
        ``__init_subclass__``, but need to remember to call ``super().__init_subclass__`` as well so this
        gets run.
        """
        if not inspect.isabstract(cls):
            parse_docdata(cls)

    @property
    def device(self) -> torch.device:
        """Return the model's device."""
        devices = self.get_devices()
        if len(devices) == 0:
            raise ValueError("Could not infer device, since there are neither parameters nor buffers.")
        elif len(devices) > 1:
            # prepare debug information
            _info = defaultdict(list)
            for name, tensor in itertools.chain(self.named_parameters(), self.named_buffers()):
                _info[tensor.data.device].append(name)
            info = {device: sorted(tensor_names) for device, tensor_names in _info.items()}
            raise ValueError(f"Ambiguous device! Found: {devices}\n\n{info}")
        else:
            return next(iter(devices))

    def get_devices(self) -> Collection[torch.device]:
        """Return the device(s) from each components of the model."""
        return {tensor.data.device for tensor in itertools.chain(self.parameters(), self.buffers())}

    def reset_parameters_(self):  # noqa: D401
        """Reset all parameters of the model and enforce model constraints."""
        self._reset_parameters_()
        # TODO: why do we need to empty the cache?
        torch.cuda.empty_cache()
        self.post_parameter_update()
        return self

    """Base methods"""

    def post_forward_pass(self):
        """Run after calculating the forward loss."""

    def _free_graph_and_cache(self):
        """Run to free the graph and cache."""

    """Abstract methods"""

    @abstractmethod
    def _reset_parameters_(self):  # noqa: D401
        """Reset all parameters of the model in-place."""

    def post_parameter_update(self) -> None:
        """Has to be called after each parameter update."""

    """Abstract methods - Scoring"""

    @abstractmethod
    def score_hrt(self, hrt_batch: torch.LongTensor) -> torch.FloatTensor:
        """Forward pass.

        This method takes head, relation and tail of each triple and calculates the corresponding score.

        :param hrt_batch: shape: (batch_size, 3), dtype: long
            The indices of (head, relation, tail) triples.

        :return: shape: (batch_size, 1), dtype: float
            The score for each triple.
        """

    @abstractmethod
    def score_t(self, hr_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using right side (tail) prediction.

        This method calculates the score for all possible tails for each (head, relation) pair.

        :param hr_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, relation) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each h-r pair, the scores for all possible tails.
        """

    @abstractmethod
    def score_r(self, ht_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using middle (relation) prediction.

        This method calculates the score for all possible relations for each (head, tail) pair.

        :param ht_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_relations), dtype: float
            For each h-t pair, the scores for all possible relations.
        """
        # TODO: this currently compute (batch_size, num_relations) instead,
        # i.e., scores for normal and inverse relations

    @abstractmethod
    def score_h(self, rt_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using left side (head) prediction.

        This method calculates the score for all possible heads for each (relation, tail) pair.

        :param rt_batch: shape: (batch_size, 2), dtype: long
            The indices of (relation, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each r-t pair, the scores for all possible heads.
        """

    @abstractmethod
    def collect_regularization_term(self) -> torch.FloatTensor:
        """Get the regularization term for the loss function."""

    """Concrete methods"""

    def get_grad_params(self) -> Iterable[nn.Parameter]:
        """Get the parameters that require gradients."""
        # TODO: Why do we need that? The optimizer takes care of filtering the parameters.
        return filter(lambda p: p.requires_grad, self.parameters())

    @property
    def num_parameter_bytes(self) -> int:
        """Calculate the number of bytes used for all parameters of the model."""
        return sum(param.numel() * param.element_size() for param in self.parameters(recurse=True))

    def save_state(self, path: Union[str, os.PathLike]) -> None:
        """Save the state of the model.

        :param path:
            Path of the file where to store the state in.
        """
        torch.save(self.state_dict(), path, pickle_protocol=pickle.HIGHEST_PROTOCOL)

    def load_state(self, path: Union[str, os.PathLike]) -> None:
        """Load the state of the model.

        :param path:
            Path of the file where to load the state from.
        """
        self.load_state_dict(torch.load(path, map_location=self.device))

    """Extended scoring methods"""

    def _prepare_batch(
        self,
        batch: Optional[torch.LongTensor],
        index_relation: int,
        invert_relation: bool,
    ) -> torch.LongTensor:
        if invert_relation and not self.use_inverse_relations:
            raise ValueError("Can only invert relations if use_inverse_relations is set to True")

        # TODO: with the current default inversion, we have to materialize the relation IDs
        if self.use_inverse_relations and batch is None:
            batch = torch.arange(self.num_relations, device=self.device)

        if batch is None:
            return None

        # send to device
        batch = batch.to(self.device)

        # map relation
        return self.relation_inverter.map(batch=batch, index=index_relation, invert=invert_relation)

    def score_hrt_extended(
        self,
        hrt_batch: torch.LongTensor,
        invert_relation: bool = False,
    ) -> torch.FloatTensor:
        """Forward pass.

        This method takes head, relation and tail of each triple and calculates the corresponding score.

        :param hrt_batch: shape: (batch_size, 3), dtype: long
            The indices of (head, relation, tail) triples.
        :param invert_relation:
            whether to invert the relation. If True, the model has to have enabled `use_inverse_relations`.

        :return: shape: (batch_size, 1), dtype: float
            The score for each triple.
        """
        return self.score_hrt(
            hrt_batch=self._prepare_batch(
                batch=hrt_batch,
                index_relation=1,
                invert_relation=invert_relation,
            ),
        )

    def score_h_extended(
        self,
        rt_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
        invert_relation: bool = False,
    ) -> torch.FloatTensor:
        """Forward pass using left side (head) prediction.

        This method calculates the score for all possible heads for each (relation, tail) pair.

        :param rt_batch: shape: (batch_size, 2), dtype: long
            The indices of (relation, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.
        :param invert_relation:
            whether to invert the relation. If True, the model has to have enabled `use_inverse_relations`.

        :return: shape: (batch_size, num_entities), dtype: float
            For each r-t pair, the scores for all possible heads.
        """
        return self.score_h(
            rt_batch=self._prepare_batch(
                batch=rt_batch,
                index_relation=0,
                invert_relation=invert_relation,
            ),
            slice_size=slice_size,
        )

    def score_r_extended(
        self,
        ht_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
        invert_relation: bool = False,
    ) -> torch.FloatTensor:
        """Forward pass using middle (relation) prediction.

        This method calculates the score for all possible relations for each (head, tail) pair.

        :param ht_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.
        :param invert_relation:
            whether to invert the relation. If True, the model has to have enabled `use_inverse_relations`.

        :return: shape: (batch_size, num_relations), dtype: float
            For each h-t pair, the scores for all possible relations.
        """
        if invert_relation:
            raise NotImplementedError
        return self.score_r(ht_batch=ht_batch, slice_size=slice_size)

    def score_t_extended(
        self,
        hr_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
        invert_relation: bool = False,
    ) -> torch.FloatTensor:
        """Forward pass using right side (tail) prediction.

        This method calculates the score for all possible tails for each (head, relation) pair.

        :param hr_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, relation) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.
        :param invert_relation:
            whether to invert the relation. If True, the model has to have enabled `use_inverse_relations`.

        :return: shape: (batch_size, num_entities), dtype: float
            For each h-r pair, the scores for all possible tails.
        """
        return self.score_t(
            hr_batch=self._prepare_batch(
                batch=hr_batch,
                index_relation=1,
                invert_relation=invert_relation,
            ),
            slice_size=slice_size,
        )

    """Prediction methods"""

    def predict_hrt(self, hrt_batch: torch.LongTensor) -> torch.FloatTensor:
        """Calculate the scores for triples.

        This method takes head, relation and tail of each triple and calculates the corresponding score.

        Additionally, the model is set to evaluation mode.

        :param hrt_batch: shape: (number of triples, 3), dtype: long
            The indices of (head, relation, tail) triples.

        :return: shape: (number of triples, 1), dtype: float
            The score for each triple.
        """
        self.eval()  # Enforce evaluation mode
        scores = self.score_hrt_extended(hrt_batch=hrt_batch)
        if self.predict_with_sigmoid:
            scores = torch.sigmoid(scores)
        return scores

    def predict_h(
        self,
        rt_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
    ) -> torch.FloatTensor:
        """Forward pass using left side (head) prediction for obtaining scores of all possible heads.

        This method calculates the score for all possible heads for each (relation, tail) pair.

        .. note::

            If the model has been trained with inverse relations, the task of predicting
            the head entities becomes the task of predicting the tail entities of the
            inverse triples, i.e., $f(*,r,t)$ is predicted by means of $f(t,r_{inv},*)$.

        Additionally, the model is set to evaluation mode.

        :param rt_batch: shape: (batch_size, 2), dtype: long
            The indices of (relation, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each r-t pair, the scores for all possible heads.
        """
        self.eval()  # Enforce evaluation mode
        if self.use_inverse_relations:
            scores = self.score_t_extended(hr_batch=rt_batch.flip(1), slice_size=slice_size, invert_relation=True)
        else:
            scores = self.score_h_extended(rt_batch=rt_batch, slice_size=slice_size)
        if self.predict_with_sigmoid:
            scores = torch.sigmoid(scores)
        return scores

    def predict_t(
        self,
        hr_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
    ) -> torch.FloatTensor:
        """Forward pass using right side (tail) prediction for obtaining scores of all possible tails.

        This method calculates the score for all possible tails for each (head, relation) pair.

        Additionally, the model is set to evaluation mode.

        :param hr_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, relation) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each h-r pair, the scores for all possible tails.

        .. note::

            We only expect the right side-predictions, i.e., $(h,r,*)$ to change its
            default behavior when the model has been trained with inverse relations
            (mainly because of the behavior of the LCWA training approach). This is why
            the :func:`predict_h` has different behavior depending on
            if inverse triples were used in training, and why this function has the same
            behavior regardless of the use of inverse triples.
        """
        self.eval()  # Enforce evaluation mode
        scores = self.score_t_extended(hr_batch, slice_size=slice_size)
        if self.predict_with_sigmoid:
            scores = torch.sigmoid(scores)
        return scores

    def predict_r(
        self,
        ht_batch: torch.LongTensor,
        slice_size: Optional[int] = None,
    ) -> torch.FloatTensor:
        """Forward pass using middle (relation) prediction for obtaining scores of all possible relations.

        This method calculates the score for all possible relations for each (head, tail) pair.

        Additionally, the model is set to evaluation mode.

        :param ht_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_real_relations), dtype: float
            For each h-t pair, the scores for all possible relations.
        """
        self.eval()  # Enforce evaluation mode
        scores = self.score_r_extended(ht_batch.to(self.device), slice_size=slice_size)
        if self.predict_with_sigmoid:
            scores = torch.sigmoid(scores)
        return scores

    def get_all_prediction_df(
        self,
        *,
        k: Optional[int] = None,
        batch_size: int = 1,
        **kwargs,
    ) -> Union[ScorePack, pd.DataFrame]:
        """Compute scores for all triples, optionally returning only the k highest scoring.

        .. note:: This operation is computationally very expensive for reasonably-sized knowledge graphs.
        .. warning:: Setting k=None may lead to huge memory requirements.

        :param k:
            The number of triples to return. Set to None, to keep all.
        :param batch_size:
            The batch size to use for calculating scores.
        :param kwargs: Additional kwargs to pass to :func:`pykeen.models.predict.get_all_prediction_df`.
        :return: shape: (k, 3)
            A tensor containing the k highest scoring triples, or all possible triples if k=None.
        """
        from .predict import get_all_prediction_df

        warnings.warn("Use pykeen.models.predict.get_all_prediction_df", DeprecationWarning)
        return get_all_prediction_df(model=self, k=k, batch_size=batch_size, **kwargs)

    def get_head_prediction_df(
        self,
        relation_label: str,
        tail_label: str,
        **kwargs,
    ) -> pd.DataFrame:
        """Predict heads for the given relation and tail (given by label).

        :param relation_label: The string label for the relation
        :param tail_label: The string label for the tail entity
        :param kwargs: Keyword arguments passed to :func:`pykeen.models.predict.get_head_prediction_df`

        The following example shows that after you train a model on the Nations dataset,
        you can score all entities w.r.t a given relation and tail entity.

        >>> from pykeen.pipeline import pipeline
        >>> result = pipeline(
        ...     dataset='Nations',
        ...     model='RotatE',
        ... )
        >>> df = result.model.get_head_prediction_df('accusation', 'brazil', triples_factory=result.training)
        """
        from .predict import get_head_prediction_df

        warnings.warn("Use pykeen.models.predict.get_head_prediction_df", DeprecationWarning)
        return get_head_prediction_df(self, relation_label=relation_label, tail_label=tail_label, **kwargs)

    def get_relation_prediction_df(
        self,
        head_label: str,
        tail_label: str,
        **kwargs,
    ) -> pd.DataFrame:
        """Predict relations for the given head and tail (given by label).

        :param head_label: The string label for the head entity
        :param tail_label: The string label for the tail entity
        :param kwargs: Keyword arguments passed to :func:`pykeen.models.predict.get_relation_prediction_df`
        """
        from .predict import get_relation_prediction_df

        warnings.warn("Use pykeen.models.predict.get_relation_prediction_df", DeprecationWarning)
        return get_relation_prediction_df(self, head_label=head_label, tail_label=tail_label, **kwargs)

    def get_tail_prediction_df(
        self,
        head_label: str,
        relation_label: str,
        **kwargs,
    ) -> pd.DataFrame:
        """Predict tails for the given head and relation (given by label).

        :param head_label: The string label for the head entity
        :param relation_label: The string label for the relation
        :param kwargs: Keyword arguments passed to :func:`pykeen.models.predict.get_tail_prediction_df`

        The following example shows that after you train a model on the Nations dataset,
        you can score all entities w.r.t a given head entity and relation.

        >>> from pykeen.pipeline import pipeline
        >>> result = pipeline(
        ...     dataset='Nations',
        ...     model='RotatE',
        ... )
        >>> df = result.model.get_tail_prediction_df('brazil', 'accusation', triples_factory=result.training)
        """
        from .predict import get_tail_prediction_df

        warnings.warn("Use pykeen.models.predict.get_tail_prediction_df", DeprecationWarning)
        return get_tail_prediction_df(self, head_label=head_label, relation_label=relation_label, **kwargs)

    """Inverse scoring"""

    def score_hrt_inverse(self, hrt_batch: torch.LongTensor) -> torch.FloatTensor:
        r"""Score triples based on inverse triples, i.e., compute $f(h,r,t)$ based on $f(t,r_{inv},h)$.

        When training with inverse relations, the model produces two (different) scores for a triple $(h,r,t) \in K$.
        The forward score is calculated from $f(h,r,t)$ and the inverse score is calculated from $f(t,r_{inv},h)$.
        This function enables users to inspect the scores obtained by using the corresponding inverse triples.
        """
        return self.score_hrt_extended(hrt_batch=hrt_batch.flip(1), invert_relation=True)

    def score_t_inverse(self, hr_batch: torch.LongTensor, slice_size: Optional[int] = None):
        """Score all tails for a batch of (h,r)-pairs using the head predictions for the inverses $(*,r_{inv},h)$."""
        return self.score_h_extended(rt_batch=hr_batch.flip(1), slice_size=slice_size, invert_relation=True)

    def score_h_inverse(self, rt_batch: torch.LongTensor, slice_size: Optional[int] = None):
        """Score all heads for a batch of (r,t)-pairs using the tail predictions for the inverses $(t,r_{inv},*)$."""
        return self.score_t_extended(hr_batch=rt_batch.flip(1), slice_size=slice_size, invert_relation=True)


class _OldAbstractModel(Model, ABC, autoreset=False):
    """A base module for PyKEEN 1.0-style KGE models."""

    #: The default regularizer class
    regularizer_default: ClassVar[Optional[Type[Regularizer]]] = None
    #: The default parameters for the default regularizer class
    regularizer_default_kwargs: ClassVar[Optional[Mapping[str, Any]]] = None
    #: The instance of the regularizer
    regularizer: Regularizer  # type: ignore

    can_slice_h = False
    can_slice_r = False
    can_slice_t = False

    def __init__(
        self,
        *,
        triples_factory: CoreTriplesFactory,
        regularizer: Optional[Regularizer] = None,
        **kwargs,
    ) -> None:
        """Initialize the module.

        :param triples_factory:
            The triples factory facilitates access to the dataset.
        :param regularizer:
            A regularizer to use for training.
        :param kwargs:
            additional keyword-based arguments passed to Model.__init__
        """
        super().__init__(triples_factory=triples_factory, **kwargs)
        # Regularizer
        if regularizer is not None:
            self.regularizer = regularizer
        elif self.regularizer_default is not None:
            self.regularizer = self.regularizer_default(
                **(self.regularizer_default_kwargs or {}),
            )
        else:
            self.regularizer = NoRegularizer()

        self._entity_ids = triples_factory.entity_ids
        self._relation_ids = triples_factory.relation_ids

    def __init_subclass__(cls, autoreset: bool = True, **kwargs):  # noqa:D105
        super().__init_subclass__(**kwargs)
        if autoreset:
            _add_post_reset_parameters(cls)

    def post_parameter_update(self) -> None:
        """Has to be called after each parameter update."""
        self.regularizer.reset()

    def regularize_if_necessary(self, *tensors: torch.FloatTensor) -> None:
        """Update the regularizer's term given some tensors, if regularization is requested.

        :param tensors: The tensors that should be passed to the regularizer to update its term.
        """
        if self.training:
            self.regularizer.update(*tensors)

    def score_t(self, hr_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using right side (tail) prediction.

        This method calculates the score for all possible tails for each (head, relation) pair.

        :param hr_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, relation) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each h-r pair, the scores for all possible tails.
        """
        logger.warning(
            "Calculations will fall back to using the score_hrt method, since this model does not have a specific "
            "score_t function. This might cause the calculations to take longer than necessary.",
        )
        # Extend the hr_batch such that each (h, r) pair is combined with all possible tails
        hrt_batch = extend_batch(batch=hr_batch, all_ids=list(self._entity_ids), dim=2)
        # Calculate the scores for each (h, r, t) triple using the generic interaction function
        expanded_scores = self.score_hrt(hrt_batch=hrt_batch)
        # Reshape the scores to match the pre-defined output shape of the score_t function.
        scores = expanded_scores.view(hr_batch.shape[0], -1)
        return scores

    def score_h(self, rt_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using left side (head) prediction.

        This method calculates the score for all possible heads for each (relation, tail) pair.

        :param rt_batch: shape: (batch_size, 2), dtype: long
            The indices of (relation, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_entities), dtype: float
            For each r-t pair, the scores for all possible heads.
        """
        logger.warning(
            "Calculations will fall back to using the score_hrt method, since this model does not have a specific "
            "score_h function. This might cause the calculations to take longer than necessary.",
        )
        # Extend the rt_batch such that each (r, t) pair is combined with all possible heads
        hrt_batch = extend_batch(batch=rt_batch, all_ids=list(self._entity_ids), dim=0)
        # Calculate the scores for each (h, r, t) triple using the generic interaction function
        expanded_scores = self.score_hrt(hrt_batch=hrt_batch)
        # Reshape the scores to match the pre-defined output shape of the score_h function.
        scores = expanded_scores.view(rt_batch.shape[0], -1)
        return scores

    def score_r(self, ht_batch: torch.LongTensor, slice_size: Optional[int] = None) -> torch.FloatTensor:
        """Forward pass using middle (relation) prediction.

        This method calculates the score for all possible relations for each (head, tail) pair.

        :param ht_batch: shape: (batch_size, 2), dtype: long
            The indices of (head, tail) pairs.
        :param slice_size: >0
            The divisor for the scoring function when using slicing.

        :return: shape: (batch_size, num_relations), dtype: float
            For each h-t pair, the scores for all possible relations.
        """
        logger.warning(
            "Calculations will fall back to using the score_hrt method, since this model does not have a specific "
            "score_r function. This might cause the calculations to take longer than necessary.",
        )
        # Extend the ht_batch such that each (h, t) pair is combined with all possible relations
        hrt_batch = extend_batch(batch=ht_batch, all_ids=list(self._relation_ids), dim=1)
        # Calculate the scores for each (h, r, t) triple using the generic interaction function
        expanded_scores = self.score_hrt(hrt_batch=hrt_batch)
        # Reshape the scores to match the pre-defined output shape of the score_r function.
        scores = expanded_scores.view(ht_batch.shape[0], -1)
        return scores

    def collect_regularization_term(self) -> torch.FloatTensor:  # noqa: D102
        return self.regularizer.term

    def post_forward_pass(self):
        """Run after calculating the forward loss."""
        self.regularizer.reset()

    def _free_graph_and_cache(self):
        self.regularizer.reset()


class EntityRelationEmbeddingModel(_OldAbstractModel, ABC, autoreset=False):
    """A base module for KGE models that have different embeddings for entities and relations."""

    #: Primary embeddings for entities
    entity_embeddings: Embedding
    #: Primary embeddings for relations
    relation_embeddings: Embedding

    def __init__(
        self,
        *,
        triples_factory: CoreTriplesFactory,
        entity_representations: EmbeddingSpecification,
        relation_representations: EmbeddingSpecification,
        **kwargs,
    ) -> None:
        """Initialize the entity embedding model.

        .. seealso:: Constructor of the base class :class:`pykeen.models.Model`
        """
        super().__init__(triples_factory=triples_factory, **kwargs)
        self.entity_embeddings = entity_representations.make(
            num_embeddings=triples_factory.num_entities,
            device=self.device,
        )
        self.relation_embeddings = relation_representations.make(
            num_embeddings=triples_factory.num_relations,
            device=self.device,
        )

    @property
    def embedding_dim(self) -> int:  # noqa:D401
        """The entity embedding dimension."""
        return self.entity_embeddings.embedding_dim

    @property
    def relation_dim(self) -> int:  # noqa:D401
        """The relation embedding dimension."""
        return self.relation_embeddings.embedding_dim

    @property
    def entity_representations(self) -> Sequence[RepresentationModule]:  # noqa:D401
        """The entity representations.

        This property provides forward compatibility with the new-style :class:`pykeen.models.ERModel`.
        """
        return [self.entity_embeddings]

    @property
    def relation_representations(self) -> Sequence[RepresentationModule]:  # noqa:D401
        """The relation representations.

        This property provides forward compatibility with the new-style :class:`pykeen.models.ERModel`.
        """
        return [self.relation_embeddings]

    def _reset_parameters_(self):  # noqa: D102
        self.entity_embeddings.reset_parameters()
        self.relation_embeddings.reset_parameters()

    def post_parameter_update(self) -> None:  # noqa: D102
        # make sure to call this first, to reset regularizer state!
        super().post_parameter_update()
        self.entity_embeddings.post_parameter_update()
        self.relation_embeddings.post_parameter_update()


def _add_post_reset_parameters(cls: Type[Model]) -> None:
    # The following lines add in a post-init hook to all subclasses
    # such that the reset_parameters_() function is run
    _original_init = cls.__init__

    @functools.wraps(_original_init)
    def _new_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.reset_parameters_()

    # sorry mypy, but this kind of evil must be permitted.
    cls.__init__ = _new_init  # type: ignore
