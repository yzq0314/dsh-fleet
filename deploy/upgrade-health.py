"""Local authenticated preflight and post-upgrade checks for a production logon task."""
import sys
import time
from pathlib import Path

import httpx
import yaml

cfg = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
target = f"http://{cfg['bind_ip']}:{cfg['port']}"
ready = sys.argv[2] == "ready"
end = time.monotonic() + (40 if ready else 6)
with httpx.Client(base_url=target, headers={"Authorization":"Bearer "+cfg["secret"]}, timeout=4, trust_env=False) as client:
    while time.monotonic() < end:
        try:
            response = client.get("/health")
            response.raise_for_status()
            status = response.json()
            if ready:
                if status.get("pocket_status") in ("ready","unconfigured","stopped"):
                    sys.exit(0)
            elif status.get("active_tasks") == 0:
                sys.exit(0)
            else:
                sys.exit("Node has active tasks")
        except httpx.HTTPError:
            pass
        time.sleep(.5)
sys.exit("Authenticated health check did not pass")
