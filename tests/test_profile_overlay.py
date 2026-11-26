import pytest
import yaml

from fleet_node.provider import resolve_route, selected_profile_route


def set_patches(home, selection, overlay):
    profile = home / "profiles" / "desktop"
    profile.mkdir(parents=True)
    rows = [
        {
            "id": "agent-default-model",
            "config": {"provider": selection, "model": "fixture"},
        },
        {"id": "llm-deepseek", "config": {"apiKeyEnv": "FIXTURE_API_KEY"}},
        {
            "id": "llm-pi-ai",
            "config": {
                "providers": {
                    "fixture": {
                        "api": "openai-completions",
                        "apiKeyEnv": "FIXTURE_API_KEY",
                        "baseURL": "https://example.invalid/v1",
                    }
                }
            },
        },
    ]
    (profile / "cordis.patch.yml").write_text(yaml.safe_dump(rows), encoding="utf-8")
    (home / "cordis.patch.yml").write_text(yaml.safe_dump(overlay), encoding="utf-8")


@pytest.mark.parametrize(
    ("provider", "disabled"),
    [
        ("deepseek-official", "agent-default-model"),
        ("deepseek-official", "llm-deepseek"),
        ("fixture", "llm-pi-ai"),
    ],
)
def test_disabled_selection_does_not_use_ambient_fallback(
    tmp_path, monkeypatch, provider, disabled
):
    set_patches(
        tmp_path,
        provider,
        [{"id": disabled, "disabled": True}, {"id": disabled, "config": {}}],
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    with pytest.raises(ValueError, match="disabled"):
        resolve_route({"dsh_home": tmp_path})


def test_only_explicit_enable_restores_provider_and_unrelated_disable_is_ignored(
    tmp_path,
):
    set_patches(
        tmp_path,
        "fixture",
        [
            {"id": "llm-pi-ai", "disabled": True},
            {"id": "llm-deepseek", "disabled": True},
            {"id": "llm-pi-ai", "disabled": False},
        ],
    )
    route = selected_profile_route(tmp_path, "desktop", {})
    assert route["type"] == "openai-compatible"
    assert route["api_key_env"] == "FIXTURE_API_KEY"
