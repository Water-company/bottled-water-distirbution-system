import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError


ADDIS_ABABA_VIEWBOX = "38.6485,9.0840,38.8472,8.8780"


def search_addis_locations(query, limit=6):
    normalized_query = (query or "").strip()
    if not normalized_query:
        return []

    search_params = {
        "q": normalized_query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "et",
        "viewbox": settings.NOMINATIM_VIEWBOX or ADDIS_ABABA_VIEWBOX,
        "bounded": 1,
        "dedupe": 1,
        "email": settings.NOMINATIM_CONTACT_EMAIL,
    }
    payload = nominatim_request("search", search_params)
    if not payload:
        fallback_params = {
            **search_params,
            "q": f"{normalized_query}, Addis Ababa, Ethiopia",
            "bounded": 0,
        }
        payload = nominatim_request("search", fallback_params)
    return [
        {
            "display_name": item.get("display_name", ""),
            "latitude": float(item["lat"]),
            "longitude": float(item["lon"]),
        }
        for item in payload
        if item.get("lat") and item.get("lon")
    ]


def reverse_geocode_coordinate(latitude, longitude):
    payload = nominatim_request(
        "reverse",
        {
            "lat": latitude,
            "lon": longitude,
            "format": "jsonv2",
            "addressdetails": 1,
            "layer": "address",
            "zoom": 18,
            "email": settings.NOMINATIM_CONTACT_EMAIL,
        },
    )
    return {
        "display_name": payload.get("display_name", ""),
        "latitude": float(payload.get("lat") or latitude),
        "longitude": float(payload.get("lon") or longitude),
        "address": payload.get("address", {}),
    }


def nominatim_request(endpoint, params):
    base_url = settings.NOMINATIM_BASE_URL.rstrip("/")
    cleaned_params = {
        key: value
        for key, value in params.items()
        if value not in ("", None)
    }
    url = f"{base_url}/{endpoint}?{urlencode(cleaned_params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": settings.NOMINATIM_USER_AGENT,
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise ValidationError(error_body or "Unable to decode that location right now.") from exc
    except (URLError, TimeoutError) as exc:
        raise ValidationError("Unable to reach the location service right now.") from exc
