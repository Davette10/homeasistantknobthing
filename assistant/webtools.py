"""Web search and weather, both keyless.

Search uses a self-hosted SearxNG if SEARXNG_URL is set, otherwise DuckDuckGo's HTML
page. Weather uses Open-Meteo (free, no API key). Results are trimmed hard because
the model's context window is small.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_RESULTS = 5
PAGE_CHARS = 1500


class WebError(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_ddg_html(page: str) -> List[SearchResult]:
    """Pull results out of html.duckduckgo.com's markup."""
    results = []
    parts = re.split(r'<div[^>]+class="([^"]*\bresult\b[^"]*)"', page)
    for classes, block in zip(parts[1::2], parts[2::2]):
        if "result--ad" in classes:
            continue
        link = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        if "uddg=" in href:  # DDG wraps links in a redirect
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        snippet = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', block, re.S)
        results.append(SearchResult(_clean(link.group(2)), href, _clean(snippet.group(1)) if snippet else ""))
        if len(results) >= MAX_RESULTS:
            break
    return results


def page_text(page: str, limit: int = PAGE_CHARS) -> str:
    """Rough readable text from an HTML page."""
    page = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|header|form)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if len(ln) > 30)  # drop menu crumbs
    return text[:limit]


class Web:
    def __init__(self, searxng_url: str = ""):
        self.searxng_url = searxng_url
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=6), follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    async def search(self, query: str) -> List[SearchResult]:
        try:
            if self.searxng_url:
                r = await self._http.get(f"{self.searxng_url}/search", params={"q": query, "format": "json"})
                r.raise_for_status()
                return [
                    SearchResult(i.get("title", ""), i.get("url", ""), i.get("content", ""))
                    for i in r.json().get("results", [])[:MAX_RESULTS]
                ]
            r = await self._http.post("https://html.duckduckgo.com/html/", data={"q": query})
            r.raise_for_status()
            return parse_ddg_html(r.text)
        except httpx.HTTPError as e:
            raise WebError(f"search failed ({e.__class__.__name__})") from e

    async def fetch_text(self, url: str) -> str:
        try:
            r = await self._http.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise WebError(f"couldn't open {url}") from e
        if "html" not in r.headers.get("content-type", "html"):
            return ""
        return page_text(r.text)

    async def search_summary(self, query: str) -> str:
        """What the model sees: top results plus a bit of the first readable page."""
        results = await self.search(query)
        if not results:
            return f"No results for {query!r}."
        lines = [f"Search results for {query!r}:"]
        for i, res in enumerate(results, 1):
            lines.append(f"{i}. {res.title} - {res.url}\n   {res.snippet[:240]}")
        for res in results[:2]:
            try:
                text = await self.fetch_text(res.url)
            except WebError:
                continue
            if text:
                lines.append(f"\nFrom {res.url}:\n{text}")
                break
        return "\n".join(lines)

    # --- weather (Open-Meteo) -----------------------------------------------

    async def geocode(self, place: str) -> Tuple[float, float, str]:
        name = place.split(",")[0].strip()
        try:
            r = await self._http.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": name, "count": 5, "language": "en", "format": "json"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise WebError("weather lookup failed") from e
        results = r.json().get("results") or []
        if not results:
            raise WebError(f"couldn't find a place called {place!r}")
        best = pick_place(results, place)
        label = ", ".join(x for x in (best.get("name"), best.get("admin1")) if x)
        return best["latitude"], best["longitude"], label

    async def weather(self, place: str) -> str:
        lat, lon, label = await self.geocode(place)
        try:
            r = await self._http.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
                    "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise WebError("weather lookup failed") from e
        return format_weather(r.json(), label)

    async def close(self) -> None:
        await self._http.aclose()


def pick_place(results: List[dict], query: str) -> dict:
    """Prefer a result whose state/country matches what the user typed after the comma."""
    hint = query.split(",", 1)[1].strip().lower() if "," in query else ""
    if hint:
        for r in results:
            region = " ".join(str(r.get(k, "")) for k in ("admin1", "country", "country_code")).lower()
            if hint in region or (len(hint) == 2 and US_STATES.get(hint.upper(), "").lower() in region):
                return r
    return results[0]


WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def format_weather(data: dict, label: str) -> str:
    cur, daily = data.get("current", {}), data.get("daily", {})
    out = [f"Weather for {label}:"]
    if cur:
        out.append(
            f"Now: {round(cur['temperature_2m'])}°F (feels {round(cur['apparent_temperature'])}°F), "
            f"{WMO.get(cur.get('weather_code'), 'mixed')}, wind {round(cur.get('wind_speed_10m', 0))} mph"
        )
    names = ["Today", "Tomorrow", "Day after"]
    for i, day in enumerate(daily.get("time", [])[:3]):
        rain = daily.get("precipitation_probability_max", [None] * 3)[i]
        out.append(
            f"{names[i]}: {WMO.get(daily['weather_code'][i], 'mixed')}, high {round(daily['temperature_2m_max'][i])}°F, "
            f"low {round(daily['temperature_2m_min'][i])}°F" + (f", {rain}% chance of rain" if rain is not None else "")
        )
    return "\n".join(out)


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def weather_place(settings_location: str, asked: Optional[str]) -> str:
    place = (asked or "").strip() or settings_location.strip()
    if not place:
        raise WebError("no location given and WEATHER_LOCATION isn't set - ask the user which city")
    return place
