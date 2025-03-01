#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import asyncio

import copy
import fnmatch
import functools
import itertools
import logging
import os
import sys
import traceback

from collections import defaultdict
from datetime import timedelta
from functools import reduce
from operator import mul
from threading import Thread
from typing import Any, Callable, cast, Dict, List, Optional, Tuple, TypeVar

import numpy as np
import torch
import torch.distributed as dist
from torch.distributed._shard.sharded_tensor import ShardedTensor
from torch.nn.parallel import DistributedDataParallel as DDP
from torchsnapshot.serialization import dtype_to_element_size, string_to_dtype

from .batcher import batch_read_requests, batch_write_requests

from .dist_store import get_or_create_store, LinearBarrier

from .flatten import flatten, inflate
from .io_preparer import (
    _identity_tensor_prepare_func,
    Chunk,
    ChunkedTensorIOPreparer,
    get_storage_path,
    ObjectBufferConsumer,
    prepare_read,
    prepare_write,
)
from .io_types import ReadIO, ReadReq, StoragePlugin, WriteIO, WriteReq
from .manifest import (
    ChunkedTensorEntry,
    Entry,
    get_available_entries,
    is_replicated,
    Manifest,
    PrimitiveEntry,
    SnapshotMetadata,
)
from .pg_wrapper import PGWrapper
from .rng_state import RNGState
from .scheduler import (
    _MAX_PER_RANK_MEMORY_BUDGET_BYTES,
    get_process_memory_budget_bytes,
    PendingIOWork,
    sync_execute_read_reqs,
    sync_execute_write_reqs,
)
from .stateful import AppState, Stateful
from .storage_plugin import url_to_storage_plugin_in_event_loop
from .version import __version__ as torchsnapshot_version

logger: logging.Logger = logging.getLogger(__name__)

SNAPSHOT_METADATA_FNAME = ".snapshot_metadata"
T = TypeVar("T")
_CHUNKING_INSTRUCTION_T = Dict[str, List[Chunk]]


class Snapshot:
    """
    Snapshot represents the persisted program state at one point in time.

    Basic usage:
    ::

        # Define the program state
        app_state = {"model": model, "optimizer": optimizer"}

        # At an appropriate time, persist the program state as a snapshot
        snapshot = Snapshot.take(path=path, app_state=app_state)

        # On resuming, restore the program state from a snapshot
        snapshot.restore(app_state)

    Overview:

        At high level, torchsnapshot saves each value in state dicts as a
        file/object in the corresponding storage system. It also saves a manifest
        describing the persisted values and the structure of the original state
        dict.

        Comparing with :py:func:`torch.save` and :py:func:`torch.load`, torchsnapshot:

        - Enables efficient random access of persisted model weights.

        - Accelerates persistence by parallelizing writes.

            - For replicated values, persistence is parallelized across ranks.

        - Enables flexible yet robust elasticity (changing world size on
          restore).


    Elasticity:

        Elasticity is implemented via correctly making persisted values
        available to a newly joined rank, and having it correctly restores the
        corresponding runtime objects from the persisted values.

        For the purpose of elasticity, all persisted values fall into one of
        the categories in [per-rank, replicated, sharded].

        per-rank:

            By default, all non-sharded values are treated as per-rank.

            On save, the value is only saved by the owning rank.

            On load, the value is only made available to the same rank.

        replicated:

            A user can suggest any non-sharded value as replicated via glob
            patterns.

            On save, the value is only saved once (can be by any rank).

            On load, the value is made available to all ranks, including newly
            joined ranks.

        sharded:

            Specific types are always treated as sharded (e.g. ShardedTensor).

            On save, all shard-owning ranks save their shards.

            On load, all shards are made available to all ranks, including
            newly joined rank. All ranks can read from all shards for
            restoring the runtime object from persisted values.
            (ShardedTensor resharding is powered by torch.dist.checkpoint).

        If all values within a snapshot are either replicated or sharded, the
        snapshot is automatically reshard-able.

        If a snapshot contains per-rank values, it cannot be resharded unless
        the per-rank values are explicitly coerced to replicated on load.
    """

    def __init__(
        self,
        path: str,
        pg: Optional[dist.ProcessGroup] = None,
    ) -> None:
        """
        Initializes the reference to an existing snapshot.

        Args:
            path: The location of the snapshot.
            pg: The process group for the processes restoring from the snapshot.
                When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
        """
        self.path: str = path
        self.pg: Optional[dist.ProcessGroup] = pg
        self._metadata: Optional[SnapshotMetadata] = None

    @classmethod
    def take(
        cls,
        path: str,
        app_state: AppState,
        pg: Optional[dist.ProcessGroup] = None,
        replicated: Optional[List[str]] = None,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> "Snapshot":
        """
        Take a snapshot from the program state.

        Args:
            app_state: The program state to take the snapshot from.
            path: The location to save the snapshot.
            pg: The process group for the processes taking the snapshot.
            When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
            replicated: A list of glob patterns for hinting the matching paths
                as replicated. Note that patterns not specified by all ranks
                are ignored.

        Returns:
            The newly taken snapshot.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.take")
        cls._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(pg=pg)

        path, replicated = cls._coalesce_path_and_replicated(
            path=path,
            pg_wrapper=pg_wrapper,
            app_state=app_state,
            replicated=replicated or [],
        )
        storage = url_to_storage_plugin_in_event_loop(
            url_path=path, event_loop=event_loop
        )
        pending_io_work, metadata = cls._take_impl(
            path=path,
            app_state=app_state,
            replicated=replicated or [],
            pg_wrapper=PGWrapper(pg),
            storage=storage,
            event_loop=event_loop,
            _custom_tensor_prepare_func=_custom_tensor_prepare_func,
        )
        pending_io_work.sync_complete(event_loop=event_loop)

        # IMPORTANT: commit snapshot metadata only after all ranks complete writing
        pg_wrapper.barrier()
        if pg_wrapper.get_rank() == 0:
            cls._write_snapshot_metadata(
                snapshot_metadata=metadata,
                storage=storage,
                event_loop=event_loop,
            )

        storage.sync_close(event_loop=event_loop)
        event_loop.close()
        snapshot = cls(path=path, pg=pg)
        snapshot._metadata = metadata
        return snapshot

    @classmethod
    def async_take(
        cls,
        path: str,
        app_state: AppState,
        pg: Optional[dist.ProcessGroup] = None,
        replicated: Optional[List[str]] = None,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> "PendingSnapshot":
        """
        Asynchronously take a snapshot from the program state.

        This method creates a consistent snapshot of the app state (i.e.
        changes to the app state after this method returns have no effect on
        the snapshot). The asynchronicity is a result of performing storage I/O
        in the background.

        Args:
            app_state: The program state to take the snapshot from.
            path: The location to save the snapshot.
            pg: The process group for the processes taking the snapshot.
            When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
            replicated: A list of glob patterns for hinting the matching paths
                as replicated. Note that patterns not specified by all ranks
                are ignored.

        Returns:
            A handle with which the newly taken snapshot can be obtained via
            `.wait()`. Note that waiting on the handle is optional. The
            snapshot will be committed regardless of whether `.wait()` is
            invoked.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.async_take")
        cls._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(pg=pg)
        path, replicated = cls._coalesce_path_and_replicated(
            path=path,
            pg_wrapper=pg_wrapper,
            app_state=app_state,
            replicated=replicated or [],
        )
        storage = url_to_storage_plugin_in_event_loop(
            url_path=path, event_loop=event_loop
        )

        pending_io_work, metadata = cls._take_impl(
            path=path,
            app_state=app_state,
            replicated=replicated or [],
            pg_wrapper=PGWrapper(pg),
            storage=storage,
            event_loop=event_loop,
            _custom_tensor_prepare_func=_custom_tensor_prepare_func,
        )
        # PendingSnapshot is responsible for closing `storage` and `event_loop`
        return PendingSnapshot(
            path=path,
            pending_io_work=pending_io_work,
            pg_wrapper=pg_wrapper,
            metadata=metadata,
            storage=storage,
            event_loop=event_loop,
        )

    @classmethod
    def _take_impl(
        cls,
        path: str,
        app_state: AppState,
        replicated: List[str],
        pg_wrapper: PGWrapper,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> Tuple[PendingIOWork, SnapshotMetadata]:
        app_state = app_state.copy()
        rng_state_item = cls._pop_rng_state(app_state=app_state)
        rng_state_dict = None

        if not _custom_tensor_prepare_func:
            _custom_tensor_prepare_func = _identity_tensor_prepare_func

        manifest: Manifest = {}
        flattened: Dict[str, Any] = {}

        # Invariant: for the same snapshot, the RNG state is the same after
        # .take() and .restore().
        # This can be achieved by ensuring .take() has no side effect on the
        # RNG state. Since we can't guarantee that the .state_dict() method on
        # stateful objects has no side effect on the RNG state, we retrieve the
        # RNG state before saving other stateful objects, and restore the RNG
        # state after saving other stateful objects.
        if rng_state_item is not None:
            key, stateful = rng_state_item
            rng_state_dict = stateful.state_dict()
            mnfst, fltnd = flatten(rng_state_dict, prefix=key)
            manifest.update(mnfst)
            flattened.update(fltnd)

        # Different ranks can register different sets of stateful objects,
        # whose .state_dict() methods may invoke collectives. To avoid
        # potential interleaving of different collectives, we first gather the
        # global key list, then invoke .state_dict() on stateful objects in
        # order with synchronization.
        global_keys = cls._gather_keys(
            keys=list(app_state.keys()), pg_wrapper=pg_wrapper
        )

        for key in global_keys:
            if key in app_state:
                state_dict = app_state[key].state_dict()
                mnfst, fltnd = flatten(state_dict, prefix=key)
                manifest.update(mnfst)
                flattened.update(fltnd)
            pg_wrapper.barrier()

        # Undo any potential side effects to the RNG state. The rest of this
        # function won't affect the RNG state or execute application code.
        if rng_state_item is not None:
            _, stateful = rng_state_item
            stateful.load_state_dict(cast(Dict[str, torch.Tensor], rng_state_dict))

        replicated_paths = cls._calculate_replicated_entries(
            flattened, replicated, pg_wrapper
        )

        object_entries: Dict[str, Entry] = {}
        write_reqs: List[WriteReq] = []

        chunking_instructions: Dict[str, List[Chunk]] = {}
        for logical_path, obj in flattened.items():
            # Chunk non-sharded tensors only
            if isinstance(obj, ShardedTensor) or not isinstance(obj, torch.Tensor):
                continue
            chunking_instruction = ChunkedTensorIOPreparer.chunk_tensor(obj)
            chunking_instructions[logical_path] = chunking_instruction

        chunking_instructions, filtered_logical_paths = cls._partition_logical_paths(
            replicated_paths, chunking_instructions, flattened, pg_wrapper
        )
        replicated_set = set(replicated_paths)
        for logical_path in chunking_instructions:
            entry, cwrs = ChunkedTensorIOPreparer.prepare_write(
                storage_path=get_storage_path(
                    flattened[logical_path],
                    logical_path,
                    pg_wrapper.get_rank(),
                    logical_path in replicated_set,
                ),
                tensor=flattened[logical_path],
                chunking_instruction=chunking_instructions[logical_path],
                _tensor_prepare_func=functools.partial(
                    _custom_tensor_prepare_func, logical_path
                ),
            )
            entry.replicated = logical_path in replicated_set
            object_entries[logical_path] = entry
            write_reqs.extend(cwrs)

        for logical_path in filtered_logical_paths:
            entry, item_write_reqs = prepare_write(
                obj=flattened[logical_path],
                logical_path=logical_path,
                rank=pg_wrapper.get_rank(),
                replicated=logical_path in replicated_set,
                _tensor_prepare_func=functools.partial(
                    _custom_tensor_prepare_func, logical_path
                ),
            )
            object_entries[logical_path] = entry
            write_reqs.extend(item_write_reqs)

        if os.environ.get("TORCHSNAPSHOT_ENABLE_BATCHING") is not None:
            entry_keys = list(object_entries.keys())
            entries = list(object_entries.values())
            entries, write_reqs = batch_write_requests(
                entries=entries, write_reqs=write_reqs
            )
            object_entries = dict(zip(entry_keys, entries))

        manifest.update(object_entries)
        manifest = cls._gather_manifest(manifest=manifest, pg=pg_wrapper)

        memory_budget_bytes = get_process_memory_budget_bytes(pg=pg_wrapper)
        pending_io_work = sync_execute_write_reqs(
            write_reqs=write_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes,
            rank=pg_wrapper.get_rank(),
            event_loop=event_loop,
        )
        metadata = SnapshotMetadata(
            version=torchsnapshot_version,
            world_size=pg_wrapper.get_world_size(),
            manifest=manifest,
        )
        return pending_io_work, metadata

    def restore(self, app_state: AppState) -> None:
        """
        Restores the program state from the snapshot.

        Args:
            app_state: The program state to restore from the snapshot.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.restore")
        self._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(self.pg)
        rank = pg_wrapper.get_rank()
        storage = url_to_storage_plugin_in_event_loop(
            url_path=self.path, event_loop=event_loop
        )

        app_state = app_state.copy()
        rng_state_item = self._pop_rng_state(app_state=app_state)

        global_keys = self._gather_keys(
            keys=list(app_state.keys()), pg_wrapper=pg_wrapper
        )
        available_entries = get_available_entries(
            manifest=self.metadata.manifest, rank=rank
        )
        for key in global_keys:
            self._load_stateful(
                rank=rank,
                stateful_key=key,
                stateful=app_state.get(key),
                available_entries=available_entries,
                storage=storage,
                pg=pg_wrapper,
                event_loop=event_loop,
            )
            pg_wrapper.barrier()

        # Restore the RNG state last to avoid potential side effects.
        if rng_state_item is not None:
            key, stateful = rng_state_item
            self._load_stateful(
                rank=rank,
                stateful_key=key,
                stateful=stateful,
                available_entries=available_entries,
                storage=storage,
                pg=pg_wrapper,
                event_loop=event_loop,
            )
        storage.sync_close(event_loop=event_loop)
        event_loop.close()

    @property
    def metadata(self) -> SnapshotMetadata:
        if self._metadata is None:
            event_loop = asyncio.new_event_loop()
            storage = url_to_storage_plugin_in_event_loop(
                url_path=self.path, event_loop=event_loop
            )
            self._metadata = self._read_snapshot_metadata(
                storage=storage, event_loop=event_loop
            )
            storage.sync_close(event_loop=event_loop)
            event_loop.close()
        return cast(SnapshotMetadata, self._metadata)

    def read_object(
        self,
        path: str,
        obj_out: Optional[T] = None,
        memory_budget_bytes: Optional[int] = None,
    ) -> T:
        """
        Read a persisted object from the snapshot's content.

        The persisted object to read is specified by its path in the snapshot
        metadata. Available paths can be obtained via `snapshot.get_manifest()`.

        A path in snapshot metadata follows the following format:

            ``RANK/STATEFUL_NAME/STATE_DICT_KEY[/NESTED_CONTAINER_KEY...]``

        The rank only matters when the persisted object is "per-rank".
        Arbitrary rank can be used when the persisted object is "replicated" or
        "sharded".

        If the persisted object is a sharded tensor, `obj_out` must be
        supplied. The supplied tensor can be either a tensor or sharded tensor.
        `read_object` will correctly populate `obj_out`'s data according to
        sharding spec.

        Args:
            path: The path to the persisted object.
            obj_out: If specified and the object type supports in-place load,
                `read_object` will directly read the persisted object into
                `obj_out`'s buffer.
            memory_budget_bytes: When specified, the read operation will keep
                the temporary memory buffer size below this threshold.

        Returns:
            The object read from the snapshot's content.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.read_object")
        # TODO: better message for malformatted path
        rank_str, unranked_path = path.split("/", 1)
        rank = int(rank_str)
        # Transform the manifest such that (1) replicated entries are made
        # available to the rank (2) sharded tensor shards saved by all ranks
        # are made available to the rank. The availability of the entries is
        # determined from the perspective of the rank specified in the path.
        manifest = get_available_entries(manifest=self.metadata.manifest, rank=rank)

        if unranked_path not in manifest:
            # TODO: show candidates based on edit distance
            raise RuntimeError(
                f'The supplied path "{path}" does not exist in the snapshot\'s manifest. '
                "Please verify the available paths within the snapshot via `snapshot.get_manifest()`."
            )
        if not isinstance(obj_out, (torch.Tensor, ShardedTensor)):
            logger.warning(
                f"`obj_out` is of type {type(obj_out)}, which does not support in-place load. "
                "Its state won't be changed after load. The loaded object will be returned."
            )

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(self.pg)
        storage = url_to_storage_plugin_in_event_loop(
            url_path=self.path, event_loop=event_loop
        )
        entry = manifest[unranked_path]
        if isinstance(entry, PrimitiveEntry):
            return cast(T, entry.get_value())
        read_reqs = prepare_read(
            entry=entry,
            obj_out=obj_out,
            # TODO: find a suitable buffer_size_limit_bytes to enable chunked
            # read even when memory_budget_bytes is not specified, as chunked
            # tensor read allows for pipelining HtoD copy and storage I/O when
            # reading a single tensor.
            buffer_size_limit_bytes=memory_budget_bytes,
        )
        box = []
        for read_req in read_reqs:
            buffer_consumer = read_req.buffer_consumer
            if isinstance(buffer_consumer, ObjectBufferConsumer):
                # ObjectBufferConsumer deals with objects that can not be
                # in-place restored. We need to replace the original object
                # in the flattened dictionary with the object materialized
                # by the buffer consumer.
                buffer_consumer.set_consume_callback(functools.partial(box.append))

        if os.environ.get("TORCHSNAPSHOT_ENABLE_BATCHING") is not None:
            read_reqs = batch_read_requests(read_reqs=read_reqs)

        sync_execute_read_reqs(
            read_reqs=read_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes
            or _MAX_PER_RANK_MEMORY_BUDGET_BYTES,
            rank=pg_wrapper.get_rank(),
            event_loop=event_loop,
        )
        storage.sync_close(event_loop=event_loop)
        event_loop.close()
        if len(box) != 0:
            if len(box) != 1:
                raise AssertionError(
                    f"Expect to load a single object from an entry (got {len(box)})."
                )
            return box[0]
        else:
            return cast(T, obj_out)

    def get_manifest(self) -> Dict[str, Entry]:
        """
        Returns the snapshot's manifest.

        Returns:
            The snapshot's manifest.
        """
        return copy.deepcopy(self.metadata.manifest)

    @staticmethod
    def _calculate_replicated_entries(
        flattened: Dict[str, Any], replicated: List[str], pg: PGWrapper
    ) -> List[str]:
        rank = pg.get_rank()
        world_size = pg.get_world_size()
        replicated_paths = []
        for path, val in flattened.items():
            if any(fnmatch.fnmatch(path, p) for p in replicated) and not isinstance(
                val, ShardedTensor
            ):
                replicated_paths.append(path)
        # pyre-ignore
        obj_list: List[List[str]] = [None] * world_size
        pg.all_gather_object(obj_list, replicated_paths)

        if rank == 0:
            # A path is only treated as replicated if:
            # (1) The path matches one of the patterns specified in `replicated`
            # (2) The path exists on all ranks
            # (3) The value is not sharded
            path_count = defaultdict(int)
            for paths in obj_list:
                for path in paths:
                    path_count[path] += 1
            replicated_paths = list(
                filter(lambda p: path_count[p] == world_size, replicated_paths)
            )
            replicated_paths_list = [replicated_paths]
        else:
            replicated_paths_list = [[]]
        pg.broadcast_object_list(replicated_paths_list, src=0)
        replicated_paths = replicated_paths_list[0]
        return replicated_paths

    @staticmethod
    def _validate_app_state(app_state: AppState) -> None:
        # performs runtime typechecking that all values are Stateful
        for key, value in app_state.items():
            if not isinstance(value, Stateful):
                value_type = type(value)
                raise TypeError(
                    f"Expected Stateful in app_state for key {key}, got {value_type}."
                )

    @classmethod
    def _load_stateful(
        cls,
        rank: int,
        stateful_key: str,
        stateful: Optional[Stateful],
        available_entries: Manifest,
        storage: StoragePlugin,
        pg: PGWrapper,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        if stateful is None:
            return

        # There are two ways to restore a stateful:
        # 1. Reconstruct the state dict from storage and use it to call .load_state_dict()
        # 2. Obtain the state dict via .state_dict(), restore its values from storage,
        # then use it to call .load_state_dict()
        #
        # When .state_dict() returns references to the original tensors, #2 is
        # more memory-efficient, because a tensor loaded from storage can be
        # freed as soon as its value is copied to the original tensor.
        state_dict = stateful.state_dict()
        mnfst, flattened = flatten(state_dict, prefix=stateful_key)
        del state_dict

        read_reqs: List[ReadReq] = []
        for logical_path, obj in flattened.items():
            if logical_path not in available_entries:
                raise RuntimeError(
                    f"""
When restoring from the snapshot, stateful object "{stateful_key}" requested
path "{logical_path}" which was not available to rank {rank}.

- If the entry does not exist in the snapshot, it means that the state dict
  entry was introduced after the snapshot was taken. To partially restore from
  the snapshot, please explicitly ignore the state dict entries missing from
  the snapshot.

- If the entry exists in the snapshot, it could mean that the world size has
  changed and the entry was not marked as replicated when the snapshot was
  taken. To resolve the issue, try any of:
    - Re-taking the snapshot with the new world size
    - Re-taking the snapshot with the original world size, ensuring all
          non-sharded values are marked as replicated
    - Coerce the missing entry into replicated on restore"""
                )

            entry = available_entries[logical_path]
            if isinstance(entry, PrimitiveEntry):
                # for primitive types, directly materialize from PrimitiveEntry
                flattened[logical_path] = entry.get_value()
                continue
            rrs = prepare_read(
                entry=entry,
                obj_out=obj,
            )
            for rr in rrs:
                buffer_consumer = rr.buffer_consumer
                if isinstance(buffer_consumer, ObjectBufferConsumer):
                    # ObjectBufferConsumer deals with objects that can not be
                    # in-place restored. We need to replace the original object
                    # in the flattened dictionary with the object materialized
                    # by the buffer consumer.
                    buffer_consumer.set_consume_callback(
                        functools.partial(dict.__setitem__, flattened, logical_path)
                    )
            read_reqs += rrs

        if os.environ.get("TORCHSNAPSHOT_ENABLE_BATCHING") is not None:
            read_reqs = batch_read_requests(read_reqs=read_reqs)

        memory_budget_bytes = get_process_memory_budget_bytes(pg=pg)
        sync_execute_read_reqs(
            read_reqs=read_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes,
            rank=pg.get_rank(),
            event_loop=event_loop,
        )

        state_dict = inflate(mnfst, flattened, prefix=stateful_key)
        stateful.load_state_dict(state_dict)

    @staticmethod
    def _write_snapshot_metadata(
        snapshot_metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        write_io = WriteIO(
            path=SNAPSHOT_METADATA_FNAME,
            buf=snapshot_metadata.to_yaml().encode("utf-8"),
        )
        storage.sync_write(write_io=write_io, event_loop=event_loop)

    @staticmethod
    def _read_snapshot_metadata(
        storage: StoragePlugin, event_loop: asyncio.AbstractEventLoop
    ) -> SnapshotMetadata:
        read_io = ReadIO(path=SNAPSHOT_METADATA_FNAME)
        storage.sync_read(read_io=read_io, event_loop=event_loop)
        yaml_str = read_io.buf.getvalue().decode("utf-8")
        return SnapshotMetadata.from_yaml(yaml_str)

    @classmethod
    def _coalesce_path_and_replicated(
        cls,
        path: str,
        pg_wrapper: PGWrapper,
        app_state: AppState,
        replicated: List[str],
    ) -> Tuple[str, List[str]]:

        rank = pg_wrapper.get_rank()

        # coalesce path
        # TODO: use a single all_gather for both path and replicated.
        # Only emit a single message for path inconsistency.
        obj_list = [path]
        pg_wrapper.broadcast_object_list(obj_list, src=0)
        if obj_list[0] != path:
            logger.warning(
                f"Rank {rank} specified a path ({path}) "
                f"different from rank 0 ({obj_list[0]}). Using path specified by rank 0."
            )

        # coalesce replicated
        replicated = cls._infer_replicated(replicated, app_state)
        # pyre-ignore[9]
        global_replicated: List[List[str]] = [None] * pg_wrapper.get_world_size()
        pg_wrapper.all_gather_object(global_replicated, replicated)

        replicated = cls._coalesce_replicated(replicated, global_replicated)
        if set(global_replicated[rank]) != set(replicated):
            logger.warning(
                f"Rank {rank} specified replicated paths: {set(global_replicated[rank])} "
                f"different from replicated paths verified across all ranks: {set(replicated)}"
            )
        return obj_list[0], replicated

    @classmethod
    def _partition_logical_paths(
        cls,
        replicated_paths: List[str],
        chunking_instructions: _CHUNKING_INSTRUCTION_T,
        flattened: Dict[str, Any],
        pg_wrapper: PGWrapper,
    ) -> Tuple[_CHUNKING_INSTRUCTION_T, List[str]]:
        """
        Returns:
            Chunking instruction (for chunkable tensors) and paths to write (for nonchunkable objects).
        """
        if pg_wrapper.get_rank() == 0:
            all_partition_results = cls._partition_replicated_paths(
                replicated_paths=replicated_paths,
                chunking_instructions=chunking_instructions,
                world_size=pg_wrapper.get_world_size(),
            )
        else:
            all_partition_results = None
        # Scatter partition results among ranks
        scatter_output_list = [None]
        pg_wrapper.scatter_object_list(
            scatter_output_list, all_partition_results, src=0
        )
        # pyre-ignore
        partition_result: Tuple[
            _CHUNKING_INSTRUCTION_T, List[str]
        ] = scatter_output_list[0]

        # Add non-replicated chunks and paths to the partition result
        replicated_set = set(replicated_paths)
        for path in flattened:
            if path not in replicated_set:
                if path in chunking_instructions:
                    partition_result[0][path] = chunking_instructions[path]
                else:
                    partition_result[1].append(path)
        return partition_result

    @staticmethod
    def _partition_replicated_paths(
        replicated_paths: List[str],
        chunking_instructions: Dict[str, List[Chunk]],
        world_size: int,
    ) -> List[Tuple[_CHUNKING_INSTRUCTION_T, List[str]]]:
        """Partitions a list of replicated paths.
        Returns a list of size `world_size`; each element of the list is a tuple of
        chunking instructions for nonchunked tensors and list of paths for nonchunked objs,
        which were assigned to a rank by a greedy partitioning alg.
        """
        partition_results = [({}, []) for i in range(world_size)]
        sizes = [0] * world_size
        chunked_paths: List[Tuple[str, Chunk, int]] = []
        nonchunked_paths: List[str] = []
        for path in replicated_paths:
            if path in chunking_instructions:
                for chunk in chunking_instructions[path]:
                    element_size = dtype_to_element_size(string_to_dtype(chunk.dtype))
                    chunked_paths.append(
                        (path, chunk, reduce(mul, chunk.sizes) * element_size)
                    )
            else:
                nonchunked_paths.append(path)

        chunked_paths.sort(key=lambda t: t[2], reverse=True)

        # Greedily assign replicated chunks among ranks, based on current sizes of ranks
        for path, chunk, size in chunked_paths:
            min_rank = np.argmin(sizes)
            if path in partition_results[min_rank][0]:
                partition_results[min_rank][0][path].append(chunk)
            else:
                partition_results[min_rank][0][path] = [chunk]
            sizes[min_rank] += size

        # Round-robin assign rest of replicated paths, which correspond to nonchunked objs
        for idx, path in enumerate(nonchunked_paths):
            partition_results[idx % world_size][1].append(path)
        return partition_results

    @staticmethod
    def _infer_replicated(replicated: List[str], app_state: AppState) -> List[str]:
        new_replicated = replicated.copy()
        if "**" in new_replicated:
            return new_replicated
        for key, val in app_state.items():
            if isinstance(val, DDP):
                ignored = set(cast(List[str], val.parameters_to_ignore))
                if not ignored:
                    new_replicated.append(os.path.join(key, "**"))
                    continue
                for name, _ in itertools.chain(
                    val.named_parameters(), val.named_buffers()
                ):
                    if name not in ignored:
                        new_replicated.append(os.path.join(key, name))
        return new_replicated

    @staticmethod
    def _coalesce_replicated(
        replicated: List[str], global_replicated: List[List[str]]
    ) -> List[str]:
        verified_replicated = list(set.intersection(*map(set, global_replicated)))
        return verified_replicated

    @staticmethod
    def _gather_keys(keys: List[str], pg_wrapper: PGWrapper) -> List[str]:
        # pyre-ignore
        gathered_keys: List[List[str]] = [None] * pg_wrapper.get_world_size()
        pg_wrapper.all_gather_object(gathered_keys, keys)
        return sorted(set(itertools.chain.from_iterable(gathered_keys)))

    @staticmethod
    def _pop_rng_state(
        app_state: AppState,
    ) -> Optional[Tuple[str, RNGState]]:
        rng_state_items = {
            key: stateful
            for key, stateful in app_state.items()
            if isinstance(stateful, RNGState)
        }
        if len(rng_state_items) > 1:
            raise RuntimeError(
                "Multiple RNGState objects in app state: "
                f"{list(rng_state_items.keys())}"
            )
        elif len(rng_state_items) == 1:
            key, stateful = list(rng_state_items.items())[0]
            del app_state[key]
            return key, stateful
        else:
            return None

    @staticmethod
    def _gather_manifest(manifest: Dict[str, Any], pg: PGWrapper) -> Dict[str, Any]:
        manifests = [None] * pg.get_world_size()
        pg.all_gather_object(manifests, manifest)
        manifests = cast(List[Manifest], manifests)

        global_manifest = {}
        replicated_entries = {}
        for manifest in manifests:
            for path, entry in manifest.items():
                if not is_replicated(entry):
                    continue
                if path in replicated_entries:
                    if not isinstance(entry, ChunkedTensorEntry):
                        raise AssertionError(
                            "Only one rank should emit the entry for a replicated path "
                            "unless the entry is ChunkedTensorEntry."
                        )
                    # Merge chunks across manifests
                    replicated_entries[path].chunks.extend(entry.chunks)
                else:
                    replicated_entries[path] = entry

        for path in replicated_entries:
            if isinstance(replicated_entries[path], ChunkedTensorEntry):
                replicated_entries[path].chunks.sort(key=lambda x: x.offsets)

        for rank, manifest in enumerate(manifests):
            for path, entry in replicated_entries.items():
                manifest[path] = entry
            for logical_path, entry in manifest.items():
                global_manifest[os.path.join(str(rank), logical_path)] = entry
        return global_manifest


class PendingSnapshot:
    DEFAULT_BARRIER_TIMEOUT = timedelta(seconds=1800)

    def __init__(
        self,
        path: str,
        pending_io_work: PendingIOWork,
        pg_wrapper: PGWrapper,
        metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.path = path
        self.pg: Optional[dist.ProcessGroup] = pg_wrapper.pg
        # pyre-ignore
        self.exc_info: Optional[Any] = None
        self._done = False

        self.thread = Thread(
            target=self._complete_snapshot,
            kwargs={
                "path": path,
                "rank": pg_wrapper.get_rank(),
                "world_size": pg_wrapper.get_world_size(),
                "pending_io_work": pending_io_work,
                "metadata": metadata,
                "storage": storage,
                "event_loop": event_loop,
                "store": get_or_create_store(pg_wrapper=pg_wrapper),
            },
        )
        self.thread.start()

    def _complete_snapshot(
        self,
        path: str,
        rank: int,
        world_size: int,
        pending_io_work: PendingIOWork,
        metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
        store: dist.TCPStore,
    ) -> None:
        # WARNING: do not use any collectives in this method

        # Use a dist.Store-based barrier for synchronization so that the
        # snapshot can be committed in the background thread.
        barrier = LinearBarrier(
            prefix=f"torchsnapshot_{path}",
            store=store,
            rank=rank,
            world_size=world_size,
            leader_rank=0,
        )
        try:
            pending_io_work.sync_complete(event_loop)
            barrier.arrive(timeout=self.DEFAULT_BARRIER_TIMEOUT)

            if rank == 0:
                Snapshot._write_snapshot_metadata(
                    snapshot_metadata=metadata,
                    storage=storage,
                    event_loop=event_loop,
                )
            barrier.depart(timeout=self.DEFAULT_BARRIER_TIMEOUT)
        except Exception as e:
            barrier.report_error(str(e))
            self.exc_info = sys.exc_info()
            logger.warning(
                f"Encountered exception while taking snapshot asynchronously:\n{e}"
            )
        finally:
            storage.sync_close(event_loop=event_loop)
            event_loop.close()
        self._done = True

    def wait(self) -> Snapshot:
        self.thread.join()
        if self.exc_info is not None:
            formatted = "".join(traceback.format_exception(*self.exc_info))
            raise RuntimeError(
                f"Encountered exception while taking snapshot asynchronously:\n{formatted}"
            )
        return Snapshot(path=self.path, pg=self.pg)

    def done(self) -> bool:
        return self._done
