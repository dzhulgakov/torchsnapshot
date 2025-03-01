#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-ignore-all-errors[56]

import os
import sys

from pathlib import Path
from typing import cast, Dict, List

import pytest

import torch
import torch.distributed as dist
import torchsnapshot

from torch.distributed._shard.sharded_tensor import ShardedTensor
from torchsnapshot.io_preparer import ShardedTensorIOPreparer
from torchsnapshot.test_utils import run_with_pet


try:
    import torchrec
except Exception as e:
    # pyre-ignore
    pytest.skip(f"Failed to import torchrec due to {e}", allow_module_level=True)


from fbgemm_gpu.split_embedding_configs import EmbOptimType
from torchrec.distributed import DistributedModelParallel, ModuleSharder
from torchrec.distributed.embeddingbag import EmbeddingBagCollectionSharder
from torchrec.distributed.planner import EmbeddingShardingPlanner, Topology
from torchrec.distributed.planner.types import ParameterConstraints
from torchrec.distributed.types import ShardingType

from torchrec.models.dlrm import DLRM, DLRMTrain

_EMBEDDING_DIM = 128
_NUM_EMBEDDINGS = 20000
_DENSE_IN_FEATURES = 128
_NUM_CLASSES = 8

_TABLES = [
    torchrec.EmbeddingBagConfig(
        name="t1",
        embedding_dim=_EMBEDDING_DIM,
        num_embeddings=_NUM_EMBEDDINGS,
        feature_names=["f1"],
        pooling=torchrec.PoolingType.SUM,
    ),
    torchrec.EmbeddingBagConfig(
        name="t2",
        embedding_dim=_EMBEDDING_DIM,
        num_embeddings=_NUM_EMBEDDINGS,
        feature_names=["f2"],
        pooling=torchrec.PoolingType.SUM,
    ),
    torchrec.EmbeddingBagConfig(
        name="t3",
        embedding_dim=_EMBEDDING_DIM,
        num_embeddings=_NUM_EMBEDDINGS,
        feature_names=["f3"],
        pooling=torchrec.PoolingType.SUM,
    ),
    torchrec.EmbeddingBagConfig(
        name="t4",
        embedding_dim=_EMBEDDING_DIM,
        num_embeddings=_NUM_EMBEDDINGS,
        feature_names=["f4"],
        pooling=torchrec.PoolingType.SUM,
    ),
]

_SHARDERS: List[ModuleSharder] = [
    EmbeddingBagCollectionSharder(
        fused_params={
            "optimizer": EmbOptimType.EXACT_ROWWISE_ADAGRAD,
            "learning_rate": 0.01,
            "eps": 0.01,
        }
    )
]


def _initialize_dmp(
    device: torch.device, sharding_type: str
) -> DistributedModelParallel:
    dlrm_model = DLRM(
        embedding_bag_collection=torchrec.EmbeddingBagCollection(
            device=torch.device("meta"),
            tables=_TABLES,
        ),
        dense_in_features=_DENSE_IN_FEATURES,
        dense_arch_layer_sizes=[64, _EMBEDDING_DIM],
        over_arch_layer_sizes=[64, _NUM_CLASSES],
    )
    model = DLRMTrain(dlrm_model)

    plan = EmbeddingShardingPlanner(
        topology=Topology(world_size=dist.get_world_size(), compute_device=device.type),
        constraints={
            table.name: ParameterConstraints(sharding_types=[sharding_type])
            for table in _TABLES
        },
    ).collective_plan(
        model,
        _SHARDERS,
        cast(dist.ProcessGroup, dist.group.WORLD),
    )

    return DistributedModelParallel(
        module=model,
        device=device,
        plan=plan,
        sharders=_SHARDERS,
    )


def _gather_dmp_state_dict(dmp: DistributedModelParallel) -> Dict[str, torch.Tensor]:
    """
    Gather a class::`DistributedDataParallel`'s state dict from all ranks.

    In the gathered state dict, class::`ShardedTensor`s are converted to
    class::`Tensor`s.
    """
    state_dict = dmp.state_dict().copy()
    for k, v in state_dict.items():
        # This covers both tensors and sharded tensors
        if isinstance(v, torch.Tensor):
            state_dict[k] = v.cpu()

    key_to_shards = {
        k: v.local_shards()
        for k, v in state_dict.items()
        if isinstance(v, ShardedTensor)
    }
    key_to_val = {
        f"{dist.get_rank()}/dmp/{k}": v
        for k, v in state_dict.items()
        if not isinstance(v, ShardedTensor)
    }

    object_list = [None] * dist.get_world_size()
    dist.all_gather_object(object_list, (key_to_shards, key_to_val))

    gathered = {}
    key_to_shards = {}
    # pyre-ignore
    for key_to_shards, key_to_val in object_list:
        gathered.update(key_to_val)
        for key, shards in key_to_shards.items():
            full_key = f"{dist.get_rank()}/dmp/{key}"
            gathered.setdefault(full_key, torch.empty(_NUM_EMBEDDINGS, _EMBEDDING_DIM))
            for shard in shards:
                offsets = shard.metadata.shard_offsets
                sizes = shard.metadata.shard_sizes
                # Assume 2D tensor
                gathered[full_key][
                    offsets[0] : offsets[0] + sizes[0],
                    offsets[1] : offsets[1] + sizes[1],
                ].copy_(shard.tensor)
    return gathered


def _sharding_types() -> List[str]:
    return [
        ShardingType.ROW_WISE.value,
        ShardingType.COLUMN_WISE.value,
        ShardingType.TABLE_WISE.value,
    ]


@pytest.mark.skipif(
    not torch.cuda.is_available(), reason="The test requires GPUs to run."
)
@pytest.mark.parametrize("src_sharding_type", _sharding_types())
@pytest.mark.parametrize("dst_sharding_type", _sharding_types())
@pytest.mark.parametrize("use_async", [True, False])
@run_with_pet(nproc=2)
def test_torchrec(
    src_sharding_type: str,
    dst_sharding_type: str,
    use_async: bool,
    tmp_path: Path,
) -> None:
    dist.init_process_group(backend="nccl")
    local_rank = int(os.environ["LOCAL_RANK"])
    device = torch.device(f"cuda:{local_rank}")
    torch.cuda.set_device(device)

    # First, initialize a dmp with a certain random seed
    # IMPORTANT: seed different rank differently
    torch.manual_seed(42 + dist.get_rank())
    src_dmp = _initialize_dmp(device=device, sharding_type=src_sharding_type)

    # Find the smallest shard size
    smallest_shard_sz = sys.maxsize
    for v in src_dmp.state_dict().values():
        if not isinstance(v, ShardedTensor):
            continue
        for shard in v.local_shards():
            smallest_shard_sz = min(
                smallest_shard_sz, shard.tensor.nelement() * shard.tensor.element_size()
            )

    # Make sure we are testing sharded tensor subdivision
    ShardedTensorIOPreparer.DEFAULT_MAX_SHARD_SIZE_BYTES = smallest_shard_sz // 2 - 1

    # Take a snapshot of src_dmp
    if use_async:
        future = torchsnapshot.Snapshot.async_take(
            path=str(tmp_path), app_state={"dmp": src_dmp}
        )
        snapshot = future.wait()
    else:
        snapshot = torchsnapshot.Snapshot.take(
            path=str(tmp_path), app_state={"dmp": src_dmp}
        )

    # Initialize another dmp with a different random seed
    torch.manual_seed(777 + dist.get_rank())
    dst_dmp = _initialize_dmp(device=device, sharding_type=dst_sharding_type)

    # Sanity check that the state dicts of the two dmps are different
    src_gathered = _gather_dmp_state_dict(src_dmp)
    dst_gathered = _gather_dmp_state_dict(dst_dmp)
    for key, src_tensor in src_gathered.items():
        assert not torch.allclose(src_tensor, dst_gathered[key])

    # Restore dst_dmp with src_dmp's snapshot, after which the state dicts of
    # the two dmps should be the same
    snapshot.restore(app_state={"dmp": dst_dmp})

    dst_gathered = _gather_dmp_state_dict(dst_dmp)
    for key, src_tensor in src_gathered.items():
        assert torch.allclose(src_tensor, dst_gathered[key])

    # Test reading tensor/sharded tensor into tensor with read_object
    for key, src in src_gathered.items():
        dst = torch.rand_like(src)
        assert not torch.allclose(src, dst)

        snapshot.read_object(path=key, obj_out=dst)
        assert torch.allclose(src, dst)
