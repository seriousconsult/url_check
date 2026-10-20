"""Check whether requests are reaching the internet through the Tor network."""

from __future__ import annotations

import requests


def is_using_tor(
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> bool:
    """Return whether the request's public IP is identified as a Tor exit node.

    The supplied session's proxy configuration is used. If no session is
    supplied, requests' normal environment proxy settings are used.

    Raises:
        requests.RequestException: If the diagnostic endpoint cannot be reached.
        ValueError: If the endpoint returns an unexpected response.
    """
    if session is None:
        response = requests.get("https://check.torproject.org/api/ip", timeout=timeout)
    else:
        response = session.get("https://check.torproject.org/api/ip", timeout=timeout)
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as error:
        raise ValueError("Tor check endpoint returned invalid JSON") from error

    if not isinstance(result, dict) or not isinstance(result.get("IsTor"), bool):
        raise ValueError("Tor check endpoint returned an unexpected response")

    return result["IsTor"]
