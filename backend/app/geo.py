"""Rough "where in the world / which time zone" for a job, worked out from the location text on the post.

This is a best guess: posts often give no location, or a vague one. A job with no clear place gets None and shows under
"Location not stated". `classify` is pure and cheap, so the whole table can be rebuilt at any time (admin: rebuild regions)."""
from __future__ import annotations

import re

# key -> (label, UTC offsets). Order matters for the UI: west to east.
BANDS: dict[str, tuple[str, str]] = {
    "worldwide": ("Anywhere / worldwide", "any time zone"),
    "americas_west": ("Western Americas", "UTC−8 to −6"),
    "americas_east": ("Eastern Americas", "UTC−5 to −3"),
    "europe_west": ("UK, Ireland & Western Europe", "UTC+0 to +1"),
    "europe_east_mea": ("Eastern Europe, Middle East & Africa", "UTC+2 to +4"),
    "south_asia": ("South Asia", "UTC+5 to +6"),
    "asia_pacific": ("East & Southeast Asia", "UTC+7 to +9"),
    "oceania": ("Australia & New Zealand", "UTC+10 to +13"),
}
REGION_KEYS = frozenset(BANDS) | {"unspecified"}

_WORLDWIDE = ("worldwide", "world wide", "anywhere", "global", "globally", "international", "any location", "all locations", "work from anywhere", "earth")

_US_WEST = ("california", "oregon", "washington state", "nevada", "arizona", "utah", "colorado", "idaho", "montana", "wyoming", "new mexico",
            "alaska", "hawaii", "texas", "oklahoma", "kansas", "nebraska", "north dakota", "south dakota", "minnesota", "iowa", "missouri",
            "arkansas", "louisiana", "wisconsin", "illinois", "mississippi", "alabama", "tennessee", "kentucky", "indiana",
            "los angeles", "san francisco", "san diego", "seattle", "portland", "las vegas", "phoenix", "denver", "salt lake", "austin",
            "dallas", "houston", "san antonio", "chicago", "minneapolis", "st. louis", "kansas city", "nashville", "silicon valley",
            "vancouver", "calgary", "edmonton", "winnipeg", "mexico", "guadalajara", "monterrey", "costa rica", "guatemala", "el salvador", "honduras", "nicaragua")
_US_WEST_ABBR = ("ca", "or", "wa", "nv", "az", "ut", "co", "id", "mt", "wy", "nm", "ak", "hi", "tx", "ok", "ks", "ne", "nd", "sd", "mn", "ia",
                 "mo", "ar", "la", "wi", "il", "ms", "al", "tn", "ky", "in", "bc", "ab", "sk", "mb")
_AM_EAST = ("new york", "florida", "georgia", "north carolina", "south carolina", "virginia", "west virginia", "maryland", "delaware",
            "pennsylvania", "new jersey", "connecticut", "rhode island", "massachusetts", "vermont", "new hampshire", "maine", "ohio",
            "michigan", "district of columbia", "washington dc", "washington, d.c.", "boston", "miami", "atlanta", "philadelphia", "orlando",
            "tampa", "charlotte", "raleigh", "detroit", "cleveland", "pittsburgh", "toronto", "montreal", "ottawa", "quebec", "nova scotia",
            "colombia", "peru", "ecuador", "panama", "cuba", "jamaica", "dominican", "puerto rico", "venezuela", "bolivia", "chile", "santiago",
            "brazil", "são paulo", "sao paulo", "rio de janeiro", "argentina", "buenos aires", "uruguay", "paraguay", "bogota", "bogotá", "lima")
_AM_EAST_ABBR = ("ny", "fl", "ga", "nc", "sc", "va", "wv", "md", "de", "pa", "nj", "ct", "ri", "ma", "vt", "nh", "me", "oh", "mi", "dc", "on", "qc", "ns", "nb")
_EUROPE_WEST = ("united kingdom", "uk", "england", "scotland", "wales", "ireland", "london", "manchester", "edinburgh", "dublin", "portugal",
                "lisbon", "spain", "madrid", "barcelona", "france", "paris", "germany", "berlin", "munich", "netherlands", "amsterdam", "belgium",
                "brussels", "luxembourg", "switzerland", "zurich", "austria", "vienna", "italy", "rome", "milan", "denmark", "copenhagen",
                "norway", "oslo", "sweden", "stockholm", "iceland", "morocco", "ghana", "senegal", "ivory coast", "nigeria", "lagos", "europe", "emea", "eu")
_EUROPE_EAST_MEA = ("poland", "warsaw", "czech", "prague", "slovakia", "hungary", "budapest", "romania", "bucharest", "bulgaria", "greece", "athens",
                    "finland", "helsinki", "estonia", "latvia", "lithuania", "ukraine", "kyiv", "belarus", "serbia", "croatia", "slovenia", "turkey",
                    "istanbul", "russia", "moscow", "israel", "tel aviv", "egypt", "cairo", "saudi", "riyadh", "uae", "united arab emirates", "dubai",
                    "abu dhabi", "qatar", "doha", "kuwait", "jordan", "lebanon", "iraq", "south africa", "johannesburg", "cape town", "kenya", "nairobi",
                    "ethiopia", "tanzania", "uganda", "zimbabwe", "zambia", "africa", "middle east", "georgia (country)", "armenia", "azerbaijan", "oman", "bahrain", "cyprus", "malta")
_SOUTH_ASIA = ("india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "chennai", "pune", "kolkata", "pakistan", "karachi", "lahore",
               "bangladesh", "dhaka", "sri lanka", "colombo", "nepal", "kathmandu", "bhutan", "maldives", "uzbekistan", "kazakhstan")
_ASIA_PACIFIC = ("china", "beijing", "shanghai", "shenzhen", "hong kong", "taiwan", "taipei", "japan", "tokyo", "osaka", "korea", "seoul", "singapore",
                 "malaysia", "kuala lumpur", "indonesia", "jakarta", "bali", "thailand", "bangkok", "vietnam", "hanoi", "ho chi minh", "philippines",
                 "manila", "cambodia", "myanmar", "laos", "mongolia", "asia", "apac")
_OCEANIA = ("australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "new zealand", "auckland", "wellington", "fiji", "oceania", "anz")
_US_CANADA_GENERIC = ("united states", "usa", "u.s.", "u.s.a", "canada", "north america", "americas", "latam", "latin america")


def _rx(words: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


_WW = _rx(_WORLDWIDE)
_ABBR_W, _ABBR_E = (re.compile(rf",\s*(?:{'|'.join(a)})(?![a-z0-9])") for a in (_US_WEST_ABBR, _AM_EAST_ABBR))
_ORDER = [("americas_east", _rx(_AM_EAST)), ("americas_west", _rx(_US_WEST)), ("europe_west", _rx(_EUROPE_WEST)),
          ("europe_east_mea", _rx(_EUROPE_EAST_MEA)), ("south_asia", _rx(_SOUTH_ASIA)), ("asia_pacific", _rx(_ASIA_PACIFIC)), ("oceania", _rx(_OCEANIA))]
_GENERIC = _rx(_US_CANADA_GENERIC)
_US_SHORT = _rx(("us", "usa", "u.s", "america", "american"))


def classify(location: str | None) -> str | None:
    """Every region the text names, as a space-separated string of BANDS keys in west-to-east order, or None when it names no place.
    "Austin, TX" -> "americas_west"; "United States" -> both American bands; "US or Europe" -> both American bands + europe_west."""
    if not location:
        return None
    t = " " + location.lower().strip() + " "
    found: set[str] = set()
    for key, rx in _ORDER:
        if rx.search(t):
            found.add(key)
    if _ABBR_W.search(t):
        found.add("americas_west")
    if _ABBR_E.search(t):
        found.add("americas_east")
    if _GENERIC.search(t) or (len(t) < 60 and _US_SHORT.search(t)):
        found.update(("americas_west", "americas_east"))        # a whole country spans several zones: show it under each
    if _WW.search(t):
        found.add("worldwide")
    if not found:
        return None
    return " ".join(k for k in BANDS if k in found)


def split(region: str | None) -> list[str]:
    return region.split() if region else []
