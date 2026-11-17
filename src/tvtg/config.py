"""Lazily created AWS clients and SSM-backed secrets, cached per Lambda container."""

import os
import time
from typing import Any

import boto3

_clients: dict[str, Any] = {}
_params: dict[str, tuple[str, float]] = {}


def client(service: str) -> Any:
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def param(env_var: str, ttl_s: float | None = None) -> str:
    """Return the value of the SSM parameter whose *name* is stored in ``env_var``.

    Values are cached for the life of the container, or for ``ttl_s`` seconds when given.
    """
    name = os.environ[env_var]
    hit = _params.get(name)
    if hit is not None and (ttl_s is None or time.monotonic() - hit[1] < ttl_s):
        return hit[0]
    value = client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    _params[name] = (value, time.monotonic())
    return value


def reset() -> None:
    """Forget cached clients and parameters (tests, and simulating a cold start)."""
    _clients.clear()
    _params.clear()
