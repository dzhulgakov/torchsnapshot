#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-ignore-all-errors[56]

import io
import logging
import os
import random
import uuid

import pytest

import torch
import torchsnapshot
from torchsnapshot.io_types import ReadIO, WriteIO
from torchsnapshot.storage_plugins.s3 import S3StoragePlugin

logger: logging.Logger = logging.getLogger(__name__)

_TEST_BUCKET = "torchsnapshot-test"
_TENSOR_SZ = int(1_000_000 / 4)


@pytest.mark.skipif(os.environ.get("TORCHSNAPSHOT_ENABLE_AWS_TEST") is None, reason="")
def test_s3_read_write_via_snapshot() -> None:
    path = f"s3://{_TEST_BUCKET}/{uuid.uuid4()}"
    logger.info(path)

    tensor = torch.rand((_TENSOR_SZ,))
    app_state = {"state": torchsnapshot.StateDict(tensor=tensor)}
    snapshot = torchsnapshot.Snapshot.take(path=path, app_state=app_state)

    app_state["state"]["tensor"] = torch.rand((_TENSOR_SZ,))
    assert not torch.allclose(tensor, app_state["state"]["tensor"])

    snapshot.restore(app_state)
    assert torch.allclose(tensor, app_state["state"]["tensor"])


@pytest.mark.skipif(os.environ.get("TORCHSNAPSHOT_ENABLE_AWS_TEST") is None, reason="")
@pytest.mark.asyncio
async def test_s3_write_read_delete() -> None:
    path = f"{_TEST_BUCKET}/{uuid.uuid4()}"
    logger.info(path)
    plugin = S3StoragePlugin(root=path)

    tensor = torch.rand((_TENSOR_SZ,))
    buf = io.BytesIO()
    torch.save(tensor, buf)
    write_io = WriteIO(path="tensor", buf=memoryview(buf.getvalue()))

    await plugin.write(write_io=write_io)

    read_io = ReadIO(path="tensor")
    await plugin.read(read_io=read_io)
    loaded = torch.load(read_io.buf)
    assert torch.allclose(tensor, loaded)

    await plugin.delete(path="tensor")
    await plugin.close()


@pytest.mark.skipif(os.environ.get("TORCHSNAPSHOT_ENABLE_AWS_TEST") is None, reason="")
@pytest.mark.asyncio
async def test_s3_ranged_read() -> None:
    path = f"{_TEST_BUCKET}/{uuid.uuid4()}"
    logger.info(path)
    plugin = S3StoragePlugin(root=path)

    buf = bytes(random.getrandbits(8) for _ in range(2000))
    write_io = WriteIO(path="rand_bytes", buf=memoryview(buf))

    await plugin.write(write_io=write_io)

    read_io = ReadIO(path="rand_bytes", byte_range=(100, 200))
    await plugin.read(read_io=read_io)
    assert len(read_io.buf.getvalue()) == 100
    assert read_io.buf.getvalue(), buf[100:200]

    await plugin.close()
