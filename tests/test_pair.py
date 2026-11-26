from pathlib import Path
import os

import pytest

from deploy.pair import prepare_workspace


def test_default_workspace_outside_checkout_and_fresh_inbox(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config = tmp_path / "private.yaml"
    roots, inbox = prepare_workspace([], config, [])
    assert roots == [home / "Documents/Fleet-Work"]
    assert inbox.is_dir() and inbox.parent == roots[0]
    inbox.joinpath("original.txt").write_text("private", encoding="utf8")
    more, fresh = prepare_workspace([], config, [])
    assert more[0] != roots[0] and fresh.is_dir() and not list(fresh.iterdir())
    assert inbox.joinpath("original.txt").is_file()


def test_default_rejects_checkout_and_protected_workspace(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".git").mkdir()
    (home / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(ValueError, match="source checkout"):
        prepare_workspace([], tmp_path / "private.yaml", [])
    with pytest.raises(ValueError, match="protected"):
        prepare_workspace(
            [Path(__file__).resolve().parents[1]], tmp_path / "private.yaml", []
        )
    assert not (home / "Documents").exists()


def test_rejects_workspace_reparse_point(tmp_path):
    destination = tmp_path / "private"
    destination.mkdir()
    link = tmp_path / "redirect"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="protected or contains a filesystem link"):
        prepare_workspace([link], tmp_path / "config.yaml", [])
    assert not list(destination.iterdir())
