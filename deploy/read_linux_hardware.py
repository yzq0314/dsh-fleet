"""Read-only Linux hardware sampler. Can run from stdin over SSH."""
import json
import shutil
import subprocess
import time
from pathlib import Path


def cpu_ticks():
    fields = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def sample():
    total, idle = cpu_ticks()
    time.sleep(0.3)
    end, idle_end = cpu_ticks()
    memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    disk = shutil.disk_usage('/')
    partitions = {}
    for mount in globals().get("MONITORED_MOUNTS", []):
        try:
            part = shutil.disk_usage(mount)
            partitions[mount] = round(100 * part.used / max(1, part.used + part.free), 1)
        except OSError:
            continue
    gpu = []
    if shutil.which('nvidia-smi'):
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=4, check=True)
            for line in result.stdout.splitlines():
                utilization, used, capacity = (int(v.strip()) for v in line.split(','))
                gpu.append(dict(utilization=utilization, used_mib=used, total_mib=capacity))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return dict(cpu=round(100*(1-(idle_end-idle)/(end-total)),1) if end>total else None,
                ram=round(100*(1-memory['MemAvailable']/memory['MemTotal']),1),
                disk=round(100*disk.used/max(1,disk.used+disk.free),1), free_gib=round(disk.free/2**30,2), partitions=partitions, gpu=gpu)


if __name__ == '__main__':
    print(json.dumps(sample(), allow_nan=False))
