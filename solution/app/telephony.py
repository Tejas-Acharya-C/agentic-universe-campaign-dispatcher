"""Async HTTP client for interacting with the mock telephony provider."""

from typing import Any, Dict, Optional, Tuple
import httpx


class TelephonyClient:
    """HTTP client wrapper for the mock carrier telephony API."""

    def __init__(self, provider_url: str, timeout: float = 10.0):
        self.provider_url = provider_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.provider_url, timeout=timeout)

    async def get_clock(self) -> Dict[str, Any]:
        """Fetch current clock state from GET /v1/clock."""
        resp = await self.client.get("/v1/clock")
        resp.raise_for_status()
        return resp.json()

    async def place_call(
        self, payload: Dict[str, Any]
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        Place an outbound call via POST /v1/calls.
        
        Returns (status_code, response_data).
        Treats network timeouts and transport errors as HTTP 504.
        """
        try:
            resp = await self.client.post("/v1/calls", json=payload)
            try:
                data = resp.json()
            except Exception:
                data = None
            return resp.status_code, data
        except (httpx.TimeoutException, httpx.TransportError):
            return 504, None

    async def get_call_by_request_id(
        self, request_id: str
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Query call details by request_id idempotency key via GET /v1/calls?request_id=..."""
        try:
            resp = await self.client.get(
                "/v1/calls", params={"request_id": request_id}
            )
            if resp.status_code == 200:
                try:
                    return 200, resp.json()
                except Exception:
                    return 200, None
            return resp.status_code, None
        except (httpx.TimeoutException, httpx.TransportError):
            return 504, None

    async def get_call_by_sid(
        self, call_sid: str
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Query call details by call_sid via GET /v1/calls/{call_sid}."""
        try:
            resp = await self.client.get(f"/v1/calls/{call_sid}")
            if resp.status_code == 200:
                try:
                    return 200, resp.json()
                except Exception:
                    return 200, None
            return resp.status_code, None
        except (httpx.TimeoutException, httpx.TransportError):
            return 504, None

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        await self.client.aclose()

    async def __aenter__(self) -> "TelephonyClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

