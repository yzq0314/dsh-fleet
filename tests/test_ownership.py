import pytest
from fleet_hub.store import Store


async def test_second_hub_refused_until_owner_closes(tmp_path):
    config = {"database": str(tmp_path / "fleet.sqlite3")}
    first, second = Store(config), Store(config)
    await first.open()
    try:
        with pytest.raises(RuntimeError, match="ownership"):
            await second.open()
        assert second.db is None
    finally:
        await first.close()
    await second.open()
    try:
        assert await second.row("SELECT value FROM metadata WHERE key='settings'")
    finally:
        await second.close()


async def test_failed_hub_initialization_releases_lock(tmp_path):
    config = {"database": str(tmp_path / "fleet.sqlite3")}
    broken = Store({**config, "nodes": [{}]})
    with pytest.raises(ValueError):
        await broken.open()
    assert broken.db is None and broken.owner is None
    repaired = Store(config)
    await repaired.open()
    await repaired.close()
