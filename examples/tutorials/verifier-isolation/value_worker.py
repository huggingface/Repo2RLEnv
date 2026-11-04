"""Untrusted adapter: serialize public behavior, never report test verdicts."""

from __future__ import annotations

import json
import sys

import torch
from accelerate.utils.operations import concatenate


def decode(value):
    if isinstance(value, dict) and "$tensor" in value:
        return torch.tensor(value["$tensor"], dtype=torch.int64)
    if isinstance(value, dict) and "$tuple" in value:
        return tuple(decode(v) for v in value["$tuple"])
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def encode(value):
    if isinstance(value, torch.Tensor):
        return {"$tensor": value.tolist()}
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


request = json.load(sys.stdin)
try:
    if request.get("mode") == "loader":
        from accelerate import Accelerator
        from torch.utils.data import DataLoader, IterableDataset

        class Dataset(IterableDataset):
            def __iter__(self):
                return iter(torch.arange(request["length"]))

        def collate(rows):
            return {"tensor": torch.stack(rows), "metadata": request["metadata"]}

        result = [
            encode(v)
            for v in Accelerator(cpu=True).prepare_data_loader(
                DataLoader(Dataset(), batch_size=4, collate_fn=collate)
            )
        ]
    else:
        result = encode(concatenate(decode(request["batches"]), dim=request.get("dim", 0)))
    response = {"value": result}
except Exception as exc:
    response = {"error": type(exc).__name__}
print(json.dumps(response, allow_nan=False))
