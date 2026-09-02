"""ANAF API Client — sole contact point with ANAF APIs."""
from __future__ import annotations
import asyncio
from typing import Any
import httpx
from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)


class ANAFAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ANAFClient:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=Config.REQUEST_TIMEOUT,
                headers={"User-Agent": "ANAFIntelPlatform/2.0", "Accept": "application/json"},
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_company_info(self, cuis: list[int], query_date: str) -> dict[str, Any]:
        payload = [{"cui": c, "data": query_date} for c in cuis]
        for attempt in range(1, Config.MAX_RETRIES + 2):
            try:
                cl = await self._get_client()
                r = await cl.post(
                    Config.ANAF_COMPANY_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if r.status_code == 404:
                    return {"found": [], "notFound": cuis}
                if r.status_code != 200:
                    raise ANAFAPIError(f"HTTP {r.status_code}", r.status_code)
                return r.json()
            except ANAFAPIError:
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as ex:
                if attempt <= Config.MAX_RETRIES:
                    await asyncio.sleep(Config.RETRY_DELAY)
                else:
                    raise ANAFAPIError(f"Request failed after {attempt} attempts: {ex}")
            except Exception as ex:
                raise ANAFAPIError(f"Unexpected error: {ex}")

    async def fetch_financials(self, cui: int, year: int) -> dict[str, Any]:
        for attempt in range(1, Config.MAX_RETRIES + 2):
            try:
                cl = await self._get_client()
                r = await cl.get(Config.ANAF_FINANCIALS_URL, params={"an": year, "cui": cui})
                if r.status_code != 200:
                    raise ANAFAPIError(f"HTTP {r.status_code}", r.status_code)
                return r.json()
            except ANAFAPIError:
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as ex:
                if attempt <= Config.MAX_RETRIES:
                    await asyncio.sleep(Config.RETRY_DELAY)
                else:
                    raise ANAFAPIError(f"Financials failed after {attempt} attempts: {ex}")
            except Exception as ex:
                raise ANAFAPIError(f"Unexpected error: {ex}")


_client: ANAFClient | None = None


def get_anaf_client() -> ANAFClient:
    global _client
    if _client is None:
        _client = ANAFClient()
    return _client
