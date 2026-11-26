import json
import time

from fleet_hub.monitors import hardware_monitors


def test_monitor_whitelist_and_staleness(tmp_path):
    file = tmp_path / 'sample.json'
    sample = dict(name='test server', timestamp=int(time.time()), cpu=7, ram=3, disk=98,
                  free_gib=2.4, gpu=[dict(utilization=10, used_mib=512, total_mib=1024)], secret='private')
    file.write_text(json.dumps(sample))
    (row,) = hardware_monitors(tmp_path)
    assert row['fresh'] and row['gpu'][0]['used_mib'] == 512 and 'secret' not in row
    sample['timestamp'] -= 120
    file.write_text(json.dumps(sample))
    assert hardware_monitors(tmp_path)[0]['fresh'] is False
    file.write_text('{')
    assert hardware_monitors(tmp_path) == []
    assert hardware_monitors(None) == []
