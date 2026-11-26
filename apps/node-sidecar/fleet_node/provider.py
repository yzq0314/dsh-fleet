"""Select one complete model route. Only the private SDK subprocess receives its credential."""

import asyncio
import hmac
import json
import secrets
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml


def origin(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in url
    ):
        raise ValueError("Invalid provider endpoint")
    port = parts.port
    if parts.scheme == "http" and parts.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ValueError("Model endpoints outside loopback require HTTPS")
    host = "[" + parts.hostname + "]" if ":" in parts.hostname else parts.hostname
    return (
        parts.scheme
        + "://"
        + host
        + (
            ":" + str(port)
            if port and port != (443 if parts.scheme == "https" else 80)
            else ""
        )
    )


def dotenv_fields(path, names):
    values = {}
    if path.is_file():
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                key, separator, value = (
                    line.strip().removeprefix("export ").partition("=")
                )
                if separator and key.strip() in names:
                    key, value = key.strip(), value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values[key] = value
    return values


@dataclass(repr=False)
class ModelRoute:
    provider: str
    model: str
    source: str
    environment: dict = field(repr=False)
    patches: list = field(repr=False)
    desktop_handoff: bool = False


def selected_profile_route(home, profile, bindings):
    """Interpret only literal official model settings. No profile JS or third-party plugin is evaluated."""
    if not re.fullmatch(r"[\w-]{1,80}", profile):
        raise ValueError("Invalid DSH profile name")

    class LiteralSettings(yaml.SafeLoader):
        pass

    unsupported = object()
    LiteralSettings.add_constructor("tag:yaml.org,2002:js", lambda *_: unsupported)
    rows = {}
    disabled = set()
    for path in (
        home / "profiles" / profile / "cordis.patch.yml",
        home / "cordis.patch.yml",
    ):
        if not path.is_file():
            continue
        try:
            parsed = yaml.load(
                path.read_text(encoding="utf-8-sig"), Loader=LiteralSettings
            )
        except yaml.YAMLError:
            raise ValueError(
                "Dynamic DSH profile settings require the Desktop plugin"
            ) from None
        if not isinstance(parsed, list):
            continue
        for row in parsed:
            if not isinstance(row, dict) or row.get("id") not in {
                "agent-default-model",
                "llm-pi-ai",
                "llm-deepseek",
            }:
                continue
            identifier = row["id"]
            if "disabled" in row:
                if row["disabled"] is False:
                    disabled.discard(identifier)
                else:
                    disabled.add(identifier)
            if isinstance(row.get("config"), dict):
                rows.setdefault(identifier, {}).update(row["config"])
    if "agent-default-model" in disabled:
        raise ValueError(
            "Default DSH model selection is disabled; check the Desktop profile"
        )
    selection = rows.get("agent-default-model", {})
    if not selection:
        return None
    name, model = selection.get("provider"), selection.get("model")
    if not isinstance(name, str) or not isinstance(model, str):
        raise ValueError("Dynamic DSH model selection requires the Desktop plugin")
    if name == "deepseek-account":
        raise ValueError(
            "DSH account login requires the Desktop plugin in the signed-in Host"
        )
    provider_id = "llm-deepseek" if name == "deepseek-official" else "llm-pi-ai"
    if provider_id in disabled:
        raise ValueError("Selected DSH provider is disabled; check the Desktop profile")
    if name == "deepseek-official":
        provider = rows.get("llm-deepseek", {})
        key = provider.get("apiKeyEnv", "DEEPSEEK_API_KEY")
        endpoint = provider.get("baseURL", "https://api.deepseek.com/anthropic")
        kind = "deepseek"
    else:
        catalog = rows.get("llm-pi-ai", {}).get("providers", {})
        provider = catalog.get(name) if isinstance(catalog, dict) else None
        if (
            not isinstance(provider, dict)
            or provider.get("api") != "openai-completions"
        ):
            raise ValueError("Selected DSH provider requires its native Desktop plugin")
        if set(provider) - {
            "api",
            "apiKeyEnv",
            "baseURL",
            "displayName",
            "models",
            "retryPolicy",
        }:
            raise ValueError("Provider-specific settings require the Desktop plugin")
        key, endpoint, kind = (
            provider.get("apiKeyEnv"),
            provider.get("baseURL"),
            "openai-compatible",
        )
    if not isinstance(key, str) or not isinstance(endpoint, str):
        raise ValueError("Dynamic credential or endpoint requires the Desktop plugin")
    return {
        "type": kind,
        "api_key_env": key,
        "base_url": endpoint,
        "model": model,
        "credential_origin": bindings.get(key),
    }


async def refresh_desktop_route(route):
    if route.source != "desktop":
        return
    async with httpx.AsyncClient(
        trust_env=False, timeout=4, follow_redirects=False
    ) as client:
        try:
            nonce = secrets.token_hex(24)
            key = route.environment["FLEET_MODEL_CAPABILITY"].encode()
            proof = hmac.digest(key, ("request/" + nonce).encode(), "sha256").hex()
            response = await client.get(
                route.environment["FLEET_MODEL_BRIDGE"] + "/selection",
                headers={"X-Fleet-Nonce": nonce, "X-Fleet-Proof": proof},
            )
            response.raise_for_status()
            selected = response.json()
            if not all(
                isinstance(selected.get(key), str) and selected[key]
                for key in ("provider", "model")
            ):
                raise ValueError("Invalid Desktop model selection")
            payload = json.dumps(
                [nonce, selected["provider"], selected["model"]],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            expected = hmac.digest(
                key, ("response/" + payload).encode(), "sha256"
            ).hex()
            if not hmac.compare_digest(expected, str(selected.get("proof", ""))):
                raise ValueError("Invalid Desktop model bridge identity")
            route.desktop_handoff = response.headers.get("X-Fleet-Desktop-Handoff") == "1"
        except httpx.ConnectError:
            raise
        except (ValueError, httpx.HTTPError):
            raise RuntimeError(
                "Desktop model bridge is unavailable; restart the configured Desktop plugin"
            ) from None
    route.model = selected["model"]
    route.environment["FLEET_SELECTED_PROVIDER"] = selected["provider"]


async def select_live_route(config):
    route = await asyncio.to_thread(resolve_route, config)
    try:
        await refresh_desktop_route(route)
    except httpx.ConnectError:
        if config.get("dsh_provider") is not None:
            raise RuntimeError(
                "The explicitly selected Desktop model bridge is offline"
            ) from None
        return await asyncio.to_thread(resolve_route, config, use_desktop=False)
    return route


def resolve_route(config, *, use_desktop=True):
    home = Path(
        config.get("dsh_home") or os.environ.get("DSH_HOME") or Path.home() / ".dsh"
    )
    explicit = config.get("dsh_provider")
    if explicit is not None and not isinstance(explicit, dict):
        raise ValueError("dsh_provider must describe one model route")
    if use_desktop and (explicit is None or explicit.get("type") == "desktop"):
        descriptor = home / "fleet-desktop" / "bridge.json"
        if descriptor.is_file():
            # The descriptor is private to this OS user. Never forward its token off loopback.
            value = json.loads(descriptor.read_text(encoding="utf-8"))
            endpoint = value["url"]
            url = urlsplit(endpoint)
            if origin(endpoint) != "http://127.0.0.1:" + str(url.port) or url.path:
                raise ValueError(
                    "Desktop bridge must be an explicit IPv4 loopback port"
                )
            if not isinstance(value["token"], str) or len(value["token"]) < 32:
                raise ValueError("Invalid Desktop bridge capability")
            patch = {
                "insert": [
                    {
                        "id": "fleet-model-bridge",
                        "name": Path(__file__).with_name("model-bridge.mjs").as_uri(),
                    }
                ]
            }
            return ModelRoute(
                "fleet-desktop",
                value["model"],
                "desktop",
                {
                    "FLEET_MODEL_BRIDGE": endpoint,
                    "FLEET_MODEL_CAPABILITY": value["token"],
                    "FLEET_SELECTED_PROVIDER": value["provider"],
                },
                [patch],
            )
        if explicit:
            raise ValueError(
                "Desktop model bridge unavailable; start the configured Desktop plugin"
            )
    app_profile = None
    managed = None
    if explicit is None:
        app_profile = selected_profile_route(
            home,
            config.get("dsh_profile", "desktop"),
            config.get("dsh_credential_origins", {}),
        )
    if app_profile:
        explicit = app_profile
        vault = home / ".credentials.yaml"
        if vault.is_file():
            # Older DSH builds auto-migrate unversioned files. Use only a documented v1 record
            # and let the installed resolver look up the selected ref; never import account grants.
            with vault.open(encoding="utf-8-sig") as handle:
                if not any(
                    re.fullmatch(r"version:\s*1\s*(?:#.*)?", line.rstrip())
                    for line in handle
                ):
                    raise ValueError(
                        "Legacy credential layout: use the Desktop plugin; the source will not be migrated"
                    )
            managed = {
                "insert": [
                    {
                        "id": "fleet-credential-resolver",
                        "name": "@deepseek-ai/dsh-credentials-local",
                        "config": {"path": str(vault)},
                    }
                ]
            }
    if explicit:
        kind = explicit.get("type")
        if kind not in {"deepseek", "openai-compatible"}:
            raise ValueError("Unsupported dsh_provider.type")
        key_name = explicit.get("api_key_env", "")
        if (
            not isinstance(key_name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,110}_(?:KEY|TOKEN)", key_name)
            or key_name in {"FLEET_GUARD_KEY", "FLEET_SELECTED_API_KEY"}
        ):
            raise ValueError("Specify a credential environment reference")
        home_key = dotenv_fields(home / ".env", {key_name}).get(key_name)
        key = (
            (home_key or os.environ.get(key_name))
            if app_profile
            else (os.environ.get(key_name) or home_key)
        )
        endpoint, model = explicit["base_url"], explicit["model"]
        pin = explicit.get("credential_origin")
        if app_profile and not pin and (managed or home_key):
            # The existing profile pairs this endpoint with a named local credential.
            # A missing vault entry must never borrow a process key for another origin.
            pin = endpoint
            key = home_key
        source = "dsh-profile" if app_profile else "sidecar"
    else:
        names = {
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "DSH_FLEET_MODEL",
            "DSH_FLEET_CREDENTIAL_ORIGIN",
        }
        # Choose a whole credential-bearing layer; a lower-priority endpoint cannot redirect an inherited key.
        ambient = {name: os.environ[name] for name in names if name in os.environ}
        application = dotenv_fields(home / ".env", names)
        if any(application.get(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
            values, source = application, "dsh-home-env"
        else:
            values, source = ambient, "environment"
        if values.get("DEEPSEEK_API_KEY"):
            kind, key = "deepseek", values["DEEPSEEK_API_KEY"]
            endpoint = (
                values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/anthropic"
            )
            model = (
                config.get("dsh_model")
                or values.get("DSH_FLEET_MODEL")
                or "deepseek-v4-pro"
            )
        elif values.get("OPENAI_API_KEY"):
            kind, key = "openai-compatible", values["OPENAI_API_KEY"]
            endpoint = values.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            model = config.get("dsh_model") or values.get("OPENAI_MODEL")
        else:
            raise ValueError("No configured model credential or active Desktop bridge")
        pin = values.get("DSH_FLEET_CREDENTIAL_ORIGIN")
        if not pin and source == "dsh-home-env":
            # Both the credential and endpoint were chosen from this one private file.
            pin = endpoint
    default_origin = (
        "https://api.deepseek.com" if kind == "deepseek" else "https://api.openai.com"
    )
    if (not key and not managed) or not isinstance(model, str) or not model.strip():
        raise ValueError("Selected route needs its named credential and model")
    if origin(endpoint) != (origin(pin) if pin else default_origin):
        raise ValueError(
            "Credential origin does not match model endpoint; bind its origin or use the Desktop plugin"
        )
    if managed:
        ref = explicit["api_key_env"]
        credentials = {ref: key} if key else {}
        if kind == "deepseek":
            return ModelRoute(
                "deepseek-official",
                model,
                source,
                credentials,
                [
                    managed,
                    {
                        "id": "llm-deepseek",
                        "config": {"apiKeyEnv": ref, "baseURL": endpoint},
                    },
                ],
            )
    if kind == "deepseek":
        return ModelRoute(
            "deepseek-official",
            model,
            source,
            {"DEEPSEEK_API_KEY": key, "DEEPSEEK_BASE_URL": endpoint},
            [],
        )
    adapter = {
        "id": "fleet-pi-ai",
        "name": "@deepseek-ai/dsh-llm-pi-ai",
        "config": {
            "providers": {
                "fleet-openai": {
                    "api": "openai-completions",
                    "apiKeyEnv": ref if managed else "FLEET_SELECTED_API_KEY",
                    "baseURL": endpoint,
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "contextWindow": int(
                                (explicit or {}).get("context_window", 65536)
                            ),
                        }
                    ],
                    "retryPolicy": {"mode": "normal", "maxRetries": 0},
                }
            }
        },
    }
    return ModelRoute(
        "fleet-openai",
        model,
        source,
        credentials if managed else {"FLEET_SELECTED_API_KEY": key},
        ([managed] if managed else []) + [{"insert": [adapter]}],
    )
