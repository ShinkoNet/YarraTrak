import hashlib
import hmac
import httpx
from urllib.parse import urlencode
try:
    from .config import PTV_DEV_ID, PTV_API_KEY
except ImportError:
    from config import PTV_DEV_ID, PTV_API_KEY


class PTVClient:
    BASE_URL = "https://timetableapi.ptv.vic.gov.au"

    def __init__(self):
        self.dev_id = PTV_DEV_ID
        self.api_key = PTV_API_KEY
        if not self.dev_id or not self.api_key:
            raise ValueError("PTV_DEV_ID and PTV_API_KEY must be set")

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
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def get_directions(self, route_id: int):
        """
        View directions that a route travels in.
        """
        endpoint = f"/v3/directions/route/{route_id}"
        url = self._sign_request(endpoint)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def get_run(self, route_type: int, run_ref: int, expand: list = None):
        """
        Get run details for a specific run_ref.
        """
        endpoint = f"/v3/runs/{run_ref}/route_type/{route_type}"
        params = {}
        if expand:
            params["expand"] = expand

        url = self._sign_request(endpoint, params)

        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def search(self, term: str):
        """
        Search for stops or routes.
        """
        from urllib.parse import quote
        # Encode the term (e.g. "narre warren" -> "narre%20warren")
        term = quote(term)
        endpoint = f"/v3/search/{term}"
        url = self._sign_request(endpoint)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
