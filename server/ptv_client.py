import asyncio
from collections import defaultdict
import hashlib
import hmac
import httpx
from urllib.parse import urlencode
try:
    from .config import (
        HTTP_CLIENT_MAX_CONNECTIONS,
        HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
        HTTP_CLIENT_TIMEOUT_SECONDS,
        PTV_DEV_ID,
        PTV_API_KEY,
    )
except ImportError:
    from config import (
        HTTP_CLIENT_MAX_CONNECTIONS,
        HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
        HTTP_CLIENT_TIMEOUT_SECONDS,
        PTV_DEV_ID,
        PTV_API_KEY,
    )


class PTVClient:
    BASE_URL = "https://timetableapi.ptv.vic.gov.au"

    def __init__(self):
        self.dev_id = PTV_DEV_ID
        self.api_key = PTV_API_KEY
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._request_counts: dict[str, int] = defaultdict(int)
        if not self.dev_id or not self.api_key:
            raise ValueError("PTV_DEV_ID and PTV_API_KEY must be set")

    async def startup(self):
        if self._client is not None:
            return
        async with self._client_lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_CLIENT_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=HTTP_CLIENT_MAX_CONNECTIONS,
                    max_keepalive_connections=HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
                ),
            )

    async def shutdown(self):
        async with self._client_lock:
            if self._client is None:
                return
            await self._client.aclose()
            self._client = None

    def snapshot_and_reset_metrics(self) -> dict[str, int]:
        snapshot = dict(self._request_counts)
        self._request_counts = defaultdict(int)
        return snapshot

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.startup()
        assert self._client is not None
        return self._client

    async def _request_json(self, url: str, metric_name: str) -> dict:
        client = await self._get_client()
        self._request_counts[metric_name] += 1
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    def _sign_request(self, endpoint: str, params: dict = None) -> str:
        if params is None:
            params = {}
        
        # Add devid to params
        params['devid'] = self.dev_id
        
        # Construct the query string
        # Note: PTV API expects the query string to be part of the signature
        query_string = urlencode(params)
        
        # The path with query to sign
        # Ensure endpoint starts with /
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
            
        path_with_query = f"{endpoint}?{query_string}" if query_string else endpoint
        
        # Calculate signature
        raw = path_with_query.encode('utf-8')
        key = self.api_key.encode('utf-8')
        hashed = hmac.new(key, raw, hashlib.sha1)
        signature = hashed.hexdigest().upper()
        
        # Return full URL with signature
        separator = "&" if "?" in path_with_query else "?"
        return f"{self.BASE_URL}{path_with_query}{separator}signature={signature}"

    async def get_departures(self, route_type: int, stop_id: int, route_id: str = None, max_results: int = 3, expand: list = None):
        """
        Get departures for a specific stop.
        route_type: 0=Train, 1=Tram, 2=Bus, 3=VLine, 4=Night Bus
        """
        endpoint = f"/v3/departures/route_type/{route_type}/stop/{stop_id}"
        params = {"max_results": max_results}
        if route_id:
            params["route_id"] = route_id
        if expand:
            params["expand"] = expand
            
        url = self._sign_request(endpoint, params)

        return await self._request_json(url, "departures")

    async def get_directions(self, route_id: int):
        """
        View directions that a route travels in.
        """
        endpoint = f"/v3/directions/route/{route_id}"
        url = self._sign_request(endpoint)

        return await self._request_json(url, "directions")

    async def get_run(self, route_type: int, run_ref: int, expand: list = None):
        """
        Get run details for a specific run_ref.
        """
        endpoint = f"/v3/runs/{run_ref}/route_type/{route_type}"
        params = {}
        if expand:
            params["expand"] = expand

        url = self._sign_request(endpoint, params)

        return await self._request_json(url, "runs")

    async def search(self, term: str):
        """
        Search for stops or routes.
        """
        from urllib.parse import quote
        # Encode the term (e.g. "narre warren" -> "narre%20warren")
        term = quote(term)
        endpoint = f"/v3/search/{term}"
        url = self._sign_request(endpoint)

        return await self._request_json(url, "search")
