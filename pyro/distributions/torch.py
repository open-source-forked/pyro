# Copyright (c) 2017-2019 Uber Technologies, Inc.
# SPDX-License-Identifier: Apache-2.0

import torch
from torch.distributions import constraints
from torch.distributions.utils import lazy_property

from pyro.distributions.constraints import IndependentConstraint
from pyro.distributions.torch_distribution import TorchDistributionMixin
from pyro.distributions.util import sum_rightmost


class Beta(torch.distributions.Beta, TorchDistributionMixin):
    def conjugate_update(self, other):
        """
        EXPERIMENTAL.
        """
        assert isinstance(other, Beta)
        concentration1 = self.concentration1 + other.concentration1 - 1
        concentration0 = self.concentration0 + other.concentration0 - 1
        updated = Beta(concentration1, concentration0)

        def _log_normalizer(d):
            x = d.concentration1
            y = d.concentration0
            return (x + y).lgamma() - x.lgamma() - y.lgamma()

        log_normalizer = _log_normalizer(self) + _log_normalizer(other) - _log_normalizer(updated)
        return updated, log_normalizer


# This overloads .log_prob() and .enumerate_support() to speed up evaluating
# log_prob on the support of this variable: we can completely avoid tensor ops
# and merely reshape the self.logits tensor. This is especially important for
# Pyro models that use enumeration.
class Categorical(torch.distributions.Categorical, TorchDistributionMixin):

    def log_prob(self, value):
        if getattr(value, '_pyro_categorical_support', None) == id(self):
            # Assume value is a reshaped torch.arange(event_shape[0]).
            # In this case we can call .reshape() rather than torch.gather().
            if not torch._C._get_tracing_state():
                if self._validate_args:
                    self._validate_sample(value)
                assert value.size(0) == self.logits.size(-1)
            logits = self.logits
            if logits.dim() <= value.dim():
                logits = logits.reshape((1,) * (1 + value.dim() - logits.dim()) + logits.shape)
            if not torch._C._get_tracing_state():
                assert logits.size(-1 - value.dim()) == 1
            return logits.transpose(-1 - value.dim(), -1).squeeze(-1)
        return super().log_prob(value)

    def enumerate_support(self, expand=True):
        result = super().enumerate_support(expand=expand)
        if not expand:
            result._pyro_categorical_support = id(self)
        return result


class Dirichlet(torch.distributions.Dirichlet, TorchDistributionMixin):
    def conjugate_update(self, other):
        """
        EXPERIMENTAL.
        """
        assert isinstance(other, Dirichlet)
        concentration = self.concentration + other.concentration - 1
        updated = Dirichlet(concentration)

        def _log_normalizer(d):
            c = d.concentration
            return c.sum(-1).lgamma() - c.lgamma().sum(-1)

        log_normalizer = _log_normalizer(self) + _log_normalizer(other) - _log_normalizer(updated)
        return updated, log_normalizer


class Gamma(torch.distributions.Gamma, TorchDistributionMixin):
    def conjugate_update(self, other):
        """
        EXPERIMENTAL.
        """
        assert isinstance(other, Gamma)
        concentration = self.concentration + other.concentration - 1
        rate = self.rate + other.rate
        updated = Gamma(concentration, rate)

        def _log_normalizer(d):
            c = d.concentration
            return d.rate.log() * c - c.lgamma()

        log_normalizer = _log_normalizer(self) + _log_normalizer(other) - _log_normalizer(updated)
        return updated, log_normalizer


class Geometric(torch.distributions.Geometric, TorchDistributionMixin):
    # TODO: move upstream
    def log_prob(self, value):
        if self._validate_args:
            self._validate_sample(value)
        return (-value - 1) * torch.nn.functional.softplus(self.logits) + self.logits


class LogNormal(torch.distributions.LogNormal, TorchDistributionMixin):
    def __init__(self, loc, scale, validate_args=None):
        base_dist = Normal(loc, scale)
        # This differs from torch.distributions.LogNormal only in that base_dist is
        # a pyro.distributions.Normal rather than a torch.distributions.Normal.
        super(torch.distributions.LogNormal, self).__init__(
            base_dist, torch.distributions.transforms.ExpTransform(), validate_args=validate_args)

    def expand(self, batch_shape, _instance=None):
        new = self._get_checked_instance(LogNormal, _instance)
        return super(torch.distributions.LogNormal, self).expand(batch_shape, _instance=new)


class MultivariateNormal(torch.distributions.MultivariateNormal, TorchDistributionMixin):
    support = IndependentConstraint(constraints.real, 1)  # TODO move upstream

    # TODO: remove this in the PyTorch release > 1.4.0
    @lazy_property
    def precision_matrix(self):
        identity = torch.eye(self.loc.size(-1), device=self.loc.device, dtype=self.loc.dtype)
        return torch.cholesky_solve(identity, self._unbroadcasted_scale_tril).expand(
            self._batch_shape + self._event_shape + self._event_shape)


class Normal(torch.distributions.Normal, TorchDistributionMixin):
    pass


class Independent(torch.distributions.Independent, TorchDistributionMixin):
    @constraints.dependent_property
    def support(self):
        return IndependentConstraint(self.base_dist.support, self.reinterpreted_batch_ndims)

    @property
    def _validate_args(self):
        return self.base_dist._validate_args

    @_validate_args.setter
    def _validate_args(self, value):
        self.base_dist._validate_args = value

    def conjugate_update(self, other):
        """
        EXPERIMENTAL.
        """
        n = self.reintepreted_batch_ndims
        updated, log_normalizer = self.base_dist.conjugate_update(other.to_event(-n))
        updated = updated.to_event(n)
        log_normalizer = sum_rightmost(log_normalizer, n)
        return updated, log_normalizer


class Uniform(torch.distributions.Uniform, TorchDistributionMixin):
    def __init__(self, low, high, validate_args=None):
        self._unbroadcasted_low = low
        self._unbroadcasted_high = high
        super().__init__(low, high, validate_args=validate_args)

    def expand(self, batch_shape, _instance=None):
        new = self._get_checked_instance(Uniform, _instance)
        new = super().expand(batch_shape, _instance=new)
        new._unbroadcasted_low = self._unbroadcasted_low
        new._unbroadcasted_high = self._unbroadcasted_high
        return new

    @constraints.dependent_property
    def support(self):
        return constraints.interval(self._unbroadcasted_low, self._unbroadcasted_high)


# Programmatically load all distributions from PyTorch.
__all__ = []
for _name, _Dist in torch.distributions.__dict__.items():
    if not isinstance(_Dist, type):
        continue
    if not issubclass(_Dist, torch.distributions.Distribution):
        continue
    if _Dist is torch.distributions.Distribution:
        continue

    try:
        _PyroDist = locals()[_name]
    except KeyError:
        _PyroDist = type(_name, (_Dist, TorchDistributionMixin), {})
        _PyroDist.__module__ = __name__
        locals()[_name] = _PyroDist

    _PyroDist.__doc__ = '''
    Wraps :class:`{}.{}` with
    :class:`~pyro.distributions.torch_distribution.TorchDistributionMixin`.
    '''.format(_Dist.__module__, _Dist.__name__)

    __all__.append(_name)


# Create sphinx documentation.
__doc__ = '\n\n'.join([

    '''
    {0}
    ----------------------------------------------------------------
    .. autoclass:: pyro.distributions.{0}
    '''.format(_name)
    for _name in sorted(__all__)
])
