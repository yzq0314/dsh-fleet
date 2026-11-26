"""Build and verify the PWA against an isolated loopback Hub and three real Sidecar processes."""

import os
import json
import time
import hmac
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deploy.demo import Cluster


class BrowserCluster(Cluster):
    def spawn(self, name, module, config):
        if name == "hub":
            monitors = self.state / "hardware"
            monitors.mkdir(exist_ok=True)
            sample = dict(name="Compute Lab", timestamp=int(time.time()), cpu=80, ram=36, disk=98, free_gib=4,
                          gpu=[dict(utilization=i*10, used_mib=i*640, total_mib=8192) for i in range(10)])
            (monitors / "sample.json").write_text(json.dumps(sample), encoding="utf8")
            config = {**config, "web_root": str(self.state / "dist"), "hardware_monitor_dir":str(monitors)}
        super().spawn(name, module, config)


def main():
    ui = ROOT / "apps/web-pwa"
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise SystemExit("Install Node.js and npm before running the browser suite.")
    if not (ui / "node_modules/playwright").is_dir():
        subprocess.run([npm, "ci"], cwd=ui, check=True)
    with tempfile.TemporaryDirectory(prefix="fleet-ui-") as state:
        # A running Hub can serve the developer checkout's dist directory.
        # Build the browser fixture beside its own disposable database.
        binary = ui / "node_modules/.bin"
        ext = ".cmd" if os.name == "nt" else ""
        subprocess.run([binary / ("tsc" + ext), "--noEmit"], cwd=ui, check=True)
        subprocess.run([binary / ("vite" + ext), "build", "--outDir", Path(state) / "dist"], cwd=ui, check=True)
        subprocess.run(["node", ui / "scripts/offline.mjs"], cwd=state, check=True)
        cluster = BrowserCluster(state, port=0)
        try:
            cluster.start()
            env = {**os.environ, "FLEET_DEMO_JSON": str(Path(state) / "demo.json")}
            # A simulated desktop plugin authenticates with this disposable cluster only.
            node = yaml.safe_load((Path(state) / "demo-a.yaml").read_text(encoding="utf8"))
            env["FLEET_TEST_DESKTOP_KEY"] = hmac.digest(
                node["secret"].encode(), b"dsh-fleet/desktop/v1", "sha256"
            ).hex()
            subprocess.run(["node", ROOT / "tests/ui/languages.mjs"], cwd=ui, env=env, check=True)
            subprocess.run([npm, "run", "test:ui"], cwd=ui, env=env, check=True)
        finally:
            cluster.close()


if __name__ == "__main__":
    main()
