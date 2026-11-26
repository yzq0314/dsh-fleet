"""Expose only validated hardware readings written by a separate collector."""
import json
import time
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError


class GPU(BaseModel):
    utilization: float = Field(ge=0, le=100)
    used_mib: int = Field(ge=0)
    total_mib: int = Field(ge=0)


class Sample(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    timestamp: int = Field(ge=0)
    cpu: float | None = Field(ge=0, le=100)
    ram: float = Field(ge=0, le=100)
    disk: float = Field(ge=0, le=100)
    free_gib: float = Field(ge=0, allow_inf_nan=False)
    partitions: dict[Annotated[str, Field(max_length=256)], Annotated[float, Field(ge=0, le=100)]] = Field(default_factory=dict, max_length=128)
    gpu: list[GPU] = Field(max_length=64)


def hardware_monitors(directory):
    if not directory:
        return []
    out = []
    for path in sorted(Path(directory).glob('*.json')):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                continue
            sample = Sample.model_validate(json.loads(raw))
        except (OSError, ValueError, ValidationError):
            continue
        out.append({**sample.model_dump(), 'fresh': 0 <= time.time()-sample.timestamp <= 60})
    return out
