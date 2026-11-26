"""Run on the monitoring VM. Read one fixed SSH target from private config."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def collect(config):
    target = config['target']
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.@-]*', target):
        raise ValueError('Invalid SSH destination')
    sampler = Path(__file__).with_name('read_linux_hardware.py').read_bytes()
    mounts = config.get("monitored_mounts", [])
    if not isinstance(mounts, list) or len(mounts) > 128 or any(
        not isinstance(m, str) or not m.startswith("/") or len(m) > 256 for m in mounts
    ):
        raise ValueError("Invalid monitored mount paths")
    sampler = ("MONITORED_MOUNTS = " + ascii(mounts) + "\n").encode("ascii") + sampler
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                             '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
                             '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                             target, 'python3 -'], input=sampler, capture_output=True, timeout=14, check=True)
    sample = json.loads(result.stdout)
    sample.update(name=config['name'], timestamp=int(time.time()))
    path = Path(config['output'])
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
    try:
        temp.write_text(json.dumps(sample, allow_nan=False), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    collect(json.loads(Path(sys.argv[1]).read_bytes()))
