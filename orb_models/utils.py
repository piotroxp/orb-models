"""Experiment utilities."""

import math
import random
import re
from collections import defaultdict
from typing import Dict, List, Mapping, Optional, Tuple, TypeVar

import numpy as np
import torch

from orb_models.forcefield import base

T = TypeVar("T")


def init_device(device_id: Optional[int] = None) -> torch.device:
    """Initialize a device.

    Initializes a device based on the device id provided,
    if not provided, it will use device_id = 0 if GPU is available.
    """
    if not device_id:
        device_id = 0
    if torch.cuda.is_available():
        device = f"cuda:{device_id}"
        torch.cuda.set_device(device_id)
        torch.cuda.empty_cache()
    else:
        device = "cpu"
    return torch.device(device)


def worker_init_fn(id: int):
    """Set seeds per worker, so augmentations etc are not duplicated across workers.

    Unused id arg is a requirement for the Dataloader interface.

    By default, each worker will have its PyTorch seed set to base_seed + worker_id,
    where base_seed is a long generated by main process using its RNG
    (thereby, consuming a RNG state mandatorily) or a specified generator.
    However, seeds for other libraries may be duplicated upon initializing workers,
    causing each worker to return identical random numbers.

    In worker_init_fn, you may access the PyTorch seed set for each worker with either
    torch.utils.data.get_worker_info().seed or torch.initial_seed(), and use it to seed
    other libraries before data loading.
    """
    uint64_seed = torch.initial_seed()
    ss = np.random.SeedSequence([uint64_seed])
    np.random.seed(ss.generate_state(4))
    random.seed(uint64_seed)


def ensure_detached(x: base.Metric) -> base.Metric:
    """Ensure that the tensor is detached and on the CPU."""
    if isinstance(x, torch.Tensor):
        return x.detach()
    return x


def to_item(x: base.Metric) -> base.Metric:
    """Convert a tensor to a python scalar."""
    if isinstance(x, torch.Tensor):
        return x.cpu().item()
    return x


def prefix_keys(
    dict_to_prefix: Dict[str, T], prefix: str, sep: str = "/"
) -> Dict[str, T]:
    """Add a prefix to dictionary keys with a seperator."""
    return {f"{prefix}{sep}{k}": v for k, v in dict_to_prefix.items()}


def seed_everything(seed: int, rank: int = 0) -> None:
    """Set the seed for all pseudo random number generators."""
    random.seed(seed + rank)
    np.random.seed(seed + rank)
    torch.manual_seed(seed + rank)


class ScalarMetricTracker:
    """Keep track of average scalar metric values."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset the AverageMetrics."""
        self.sums = defaultdict(float)
        self.counts = defaultdict(int)

    def update(self, metrics: Mapping[str, base.Metric]) -> None:
        """Update the metric counts with new values."""
        for k, v in metrics.items():
            if isinstance(v, torch.Tensor) and v.nelement() > 1:
                continue  # only track scalar metrics
            if isinstance(v, torch.Tensor) and v.isnan().any():
                continue
            self.sums[k] += ensure_detached(v)
            self.counts[k] += 1

    def get_metrics(self):
        """Get the metric values, possibly reducing across gpu processes."""
        return {k: to_item(v) / self.counts[k] for k, v in self.sums.items()}


def gradient_clipping(
    model: torch.nn.Module, clip_value: float
) -> List[torch.utils.hooks.RemovableHandle]:
    """Add gradient clipping hooks to a model.

    This is the correct way to implement gradient clipping, because
    gradients are clipped as gradients are computed, rather than after
    all gradients are computed - this means expoding gradients are less likely,
    because they are "caught" earlier.

    Args:
        model: The model to add hooks to.
        clip_value: The upper and lower threshold to clip the gradients to.

    Returns:
        A list of handles to remove the hooks from the parameters.
    """
    handles = []

    def _clip(grad):
        if grad is None:
            return grad
        return grad.clamp(min=-clip_value, max=clip_value)

    for parameter in model.parameters():
        if parameter.requires_grad:
            h = parameter.register_hook(lambda grad: _clip(grad))
            handles.append(h)

    return handles


def get_optim(
    lr: float, total_steps: int, model: torch.nn.Module
) -> Tuple[torch.optim.Optimizer, Optional[torch.optim.lr_scheduler._LRScheduler]]:
    """Configure optimizers, LR schedulers and EMA."""

    # Initialize parameter groups
    params = []

    # Split parameters based on the regex
    for name, param in model.named_parameters():
        if re.search(r"(.*bias|.*layer_norm.*|.*batch_norm.*)", name):
            params.append({"params": param, "weight_decay": 0.0})
        else:
            params.append({"params": param})

    # Create the optimizer with the parameter groups
    optimizer = torch.optim.Adam(params, lr=lr)

    # Create the learning rate scheduler
    div_factor = 10  # max lr will be 10 times larger than initial lr
    final_div_factor = 10  # min lr will be 10 times smaller than initial lr
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, 
        max_lr=lr * div_factor, 
        total_steps=total_steps, 
        pct_start=0.05, 
        div_factor=div_factor,
        final_div_factor=final_div_factor,
    )

    return optimizer, scheduler


def rand_angles(*shape, requires_grad=False, dtype=None, device=None):
    r"""random rotation angles

    Parameters
    ----------
    *shape : int

    Returns
    -------
    alpha : `torch.Tensor`
        tensor of shape :math:`(\mathrm{shape})`

    beta : `torch.Tensor`
        tensor of shape :math:`(\mathrm{shape})`

    gamma : `torch.Tensor`
        tensor of shape :math:`(\mathrm{shape})`
    """
    alpha, gamma = 2 * math.pi * torch.rand(2, *shape, dtype=dtype, device=device)
    beta = torch.rand(shape, dtype=dtype, device=device).mul(2).sub(1).acos()
    alpha = alpha.detach().requires_grad_(requires_grad)
    beta = beta.detach().requires_grad_(requires_grad)
    gamma = gamma.detach().requires_grad_(requires_grad)
    return alpha, beta, gamma


def matrix_x(angle: torch.Tensor) -> torch.Tensor:
    r"""matrix of rotation around X axis

    Parameters
    ----------
    angle : `torch.Tensor`
        tensor of any shape :math:`(...)`

    Returns
    -------
    `torch.Tensor`
        matrices of shape :math:`(..., 3, 3)`
    """
    c = angle.cos()
    s = angle.sin()
    o = torch.ones_like(angle)
    z = torch.zeros_like(angle)
    return torch.stack(
        [
            torch.stack([o, z, z], dim=-1),
            torch.stack([z, c, -s], dim=-1),
            torch.stack([z, s, c], dim=-1),
        ],
        dim=-2,
    )


def matrix_y(angle: torch.Tensor) -> torch.Tensor:
    r"""matrix of rotation around Y axis

    Parameters
    ----------
    angle : `torch.Tensor`
        tensor of any shape :math:`(...)`

    Returns
    -------
    `torch.Tensor`
        matrices of shape :math:`(..., 3, 3)`
    """
    c = angle.cos()
    s = angle.sin()
    o = torch.ones_like(angle)
    z = torch.zeros_like(angle)
    return torch.stack(
        [
            torch.stack([c, z, s], dim=-1),
            torch.stack([z, o, z], dim=-1),
            torch.stack([-s, z, c], dim=-1),
        ],
        dim=-2,
    )


def matrix_z(angle: torch.Tensor) -> torch.Tensor:
    r"""matrix of rotation around Z axis

    Parameters
    ----------
    angle : `torch.Tensor`
        tensor of any shape :math:`(...)`

    Returns
    -------
    `torch.Tensor`
        matrices of shape :math:`(..., 3, 3)`
    """
    c = angle.cos()
    s = angle.sin()
    o = torch.ones_like(angle)
    z = torch.zeros_like(angle)
    return torch.stack(
        [
            torch.stack([c, -s, z], dim=-1),
            torch.stack([s, c, z], dim=-1),
            torch.stack([z, z, o], dim=-1),
        ],
        dim=-2,
    )


def angles_to_matrix(alpha, beta, gamma):
    r"""conversion from angles to matrix

    Parameters
    ----------
    alpha : `torch.Tensor`
        tensor of shape :math:`(...)`

    beta : `torch.Tensor`
        tensor of shape :math:`(...)`

    gamma : `torch.Tensor`
        tensor of shape :math:`(...)`

    Returns
    -------
    `torch.Tensor`
        matrices of shape :math:`(..., 3, 3)`
    """
    alpha, beta, gamma = torch.broadcast_tensors(alpha, beta, gamma)
    return matrix_y(alpha) @ matrix_x(beta) @ matrix_y(gamma)


def rand_matrix(*shape, requires_grad=False, dtype=None, device=None):
    r"""random rotation matrix

    Parameters
    ----------
    *shape : int

    Returns
    -------
    `torch.Tensor`
        tensor of shape :math:`(\mathrm{shape}, 3, 3)`
    """
    R = angles_to_matrix(*rand_angles(*shape, dtype=dtype, device=device))
    return R.detach().requires_grad_(requires_grad)
