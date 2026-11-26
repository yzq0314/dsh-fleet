import re
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def uid():
    return uuid4().hex


def http_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("请填写不含凭据的 HTTP(S) 地址")
    _ = parts.port
    return value


def node_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    parts = urlsplit(http_url(value))
    if parts.path.strip("/") or parts.query or parts.fragment:
        raise ValueError("节点地址只包含协议、主机和端口")
    return value.rstrip("/")


_PRIVATE_NETWORKS = tuple(map(ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
)))
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def dsh_web_entry(node: dict) -> str | None:
    """Build a public bookmark from a paired host and a port, with no launch secrets."""
    port = node.get("dsh_web_port")
    scheme = node.get("dsh_web_scheme", "https")
    if type(port) is not int or not 1 <= port <= 65535 or scheme not in ("http", "https"):
        return None
    endpoint = urlsplit(node["url"])
    if port == (endpoint.port or (443 if endpoint.scheme == "https" else 80)):
        return None
    host = endpoint.hostname or ""
    try:
        address = ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
            return None
        if len(labels) == 1:
            # Browsers resolve all-numeric and hexadecimal hosts as IPv4 literals.
            if host == "localhost" or re.fullmatch(r"\d+|0x[0-9a-f]*", host):
                return None
        elif not host.endswith((".ts.net", ".local", ".lan", ".internal", ".home.arpa")):
            return None
    else:
        if "%" in host or not any(address in subnet for subnet in _PRIVATE_NETWORKS):
            return None
        host = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
    return f"{scheme}://{host}:{port}/"


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=32768)


class NodeEdit(Fields):
    alias: str | None = Field(None, min_length=1, max_length=80)
    url: str | None = None
    dsh_web_port: int | None = Field(None, ge=1, le=65535, strict=True)
    dsh_web_scheme: Literal["http", "https"] = "https"
    tags: list[str] | None = Field(None, max_length=64)
    icon: str | None = Field(None, max_length=48)
    mac: str | None = Field(
        None, pattern=r"^(?:$|(?:[A-Fa-f0-9]{2}[:-]){5}[A-Fa-f0-9]{2})$"
    )
    broadcast: IPv4Address | None = None
    wake_proxy: str | None = None
    enabled: bool | None = None

    _url = field_validator("url")(node_endpoint)


class Enroll(Fields):
    token: str
    alias: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=64)
    url: str
    _url = field_validator("url")(node_endpoint)


class Context(Fields):
    volume: str = Field(min_length=1, max_length=80)
    path: str


class Dispatch(Fields):
    prompt: str = Field(min_length=1)
    workspace: str = ""
    mode: Literal["single", "auto", "fanout"] = "auto"
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list)
    agent: str = Field(default="dsh", pattern=r"^[a-zA-Z0-9_.-]{1,48}$")
    context: Context | None = None
    desktop_handoff: bool = True


class Answer(Fields):
    decision: Literal["approve_once", "approve_session_dir", "deny"]


class Macro(Fields):
    name: str = Field(min_length=1, max_length=80)
    template: str = Field(min_length=1)
    position: int = 0
    target_tags: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("target_tags")
    @classmethod
    def normalize_tags(cls, tags):
        cleaned = [tag.strip() for tag in tags]
        if any(not tag or len(tag) > 64 for tag in cleaned):
            raise ValueError("标签长度应为 1–64")
        return list(dict.fromkeys(cleaned))


class Service(Fields):
    name: str = Field(min_length=1, max_length=80)
    url: str
    node_id: str | None = None
    volume: str | None = None
    _url = field_validator("url")(http_url)


class Preferences(Fields):
    theme: str = Field(default="industrial", max_length=40)
    accent: str = Field(default="mint", max_length=40)
    language: str = Field(default="zh-CN", max_length=16)
    default_route: Literal["single", "auto", "fanout"] = "auto"
