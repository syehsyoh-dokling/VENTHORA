# ruff: noqa: E501
"""Where VENTHORA itself may connect when a person types the address (a connector, a self-hosted model, a discovery).

A typed address must not reach what only the server can reach. Always refused: link-local addresses (the cloud
metadata service at 169.254.169.254 is one) and anything that is not http or https. On a multi-company deployment
(VENTHORA_DEPLOYMENT=saas) loopback and private networks are refused too: there the server's own network is not the
customer's. A self-hosted VENTHORA may reach its own network (a model on localhost, an ERP on the LAN): that is the
point of hosting it yourself.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class Refused(ValueError):
    pass


def check_url(url: str, *, saas: bool | None = None) -> str:
    """The url when VENTHORA may connect to it; Refused (with the reason) otherwise."""
    if saas is None:
        from . import access

        saas = access.deployment() == "saas"
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("the address must be an http or https URL")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise Refused(f"{parts.hostname} cannot be resolved ({exc.__class__.__name__})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise Refused(f"{parts.hostname} is a link-local or special address ({ip}): VENTHORA does not connect there")
        if ip.is_loopback and parts.path.startswith("/demo/legacy-erp"):
            continue  # VENTHORA's own stand-in ERP (the demo connector), served by this same process: fictional data only
        if saas and (ip.is_private or ip.is_loopback or ip.is_reserved):
            raise Refused(f"{parts.hostname} is on a private network ({ip}): on this deployment VENTHORA connects to public addresses only")
    return url
