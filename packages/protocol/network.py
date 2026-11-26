"""Bind an explicit interface and authorize the actual transport peer."""

import ipaddress
import socket

import psutil


def allowed_peer(peer: str, cidrs: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(peer.split("%")[0])
        address = getattr(address, "ipv4_mapped", None) or address
        return any(
            address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs
        )
    except (ValueError, AttributeError):
        return False


def resolve_bind(config: dict, unsafe_bind_all=False) -> str:
    mode = config.get("bind_mode", "ip")
    if mode == "ip":
        value = str(ipaddress.ip_address(config.get("bind_ip", "127.0.0.1")))
    elif mode in {"cidr_match", "interface"}:
        candidates = []
        if mode == "cidr_match":
            network = ipaddress.ip_network(config["cidr"], strict=False)
        for name, interfaces in psutil.net_if_addrs().items():
            if (
                mode == "interface"
                and name.casefold() != config["interface"].casefold()
            ):
                continue
            for entry in interfaces:
                if entry.family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                address = ipaddress.ip_address(entry.address.split("%")[0])
                if not address.is_unspecified and (
                    mode == "interface" or address in network
                ):
                    candidates.append((address.version, name, entry.address))
        if not candidates:
            raise ValueError("No local interface matches the binding configuration")
        value = min(candidates)[2]
    else:
        raise ValueError("Unknown bind_mode")
    address = ipaddress.ip_address(value.split("%")[0])
    effective = getattr(address, "ipv4_mapped", None) or address
    if effective.is_unspecified and not unsafe_bind_all:
        raise ValueError("Wildcard binding requires --unsafe-bind-all")
    return value
