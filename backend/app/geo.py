"""Rough "where in the world / which time zone" for a job, worked out from the location text on the post.

This is a best guess: posts often give no location, or a vague one. A job with no clear place gets None and shows under
"Location not stated". `classify` is pure and cheap, so the whole table can be rebuilt at any time (admin: rebuild regions)."""
from __future__ import annotations

import re
import unicodedata

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
            "arkansas", "louisiana", "wisconsin", "illinois", "mississippi", "alabama", "tennessee", "kentucky",
            "los angeles", "san francisco", "san diego", "seattle", "portland", "las vegas", "phoenix", "denver", "salt lake", "austin",
            "dallas", "houston", "san antonio", "chicago", "minneapolis", "st. louis", "kansas city", "nashville", "silicon valley", "spokane", "tacoma", "bellevue", "olympia", "bay area", "sf bay", "west coast", "midwest", "pacific northwest",
            "pacific time", "mountain time", "central time", "alberta", "british columbia", "saskatchewan", "manitoba", "pst", "pdt", "mst", "mdt", "cst", "cdt",
            "vancouver", "calgary", "edmonton", "winnipeg", "mexico", "guadalajara", "monterrey", "costa rica", "guatemala", "el salvador", "honduras", "nicaragua", "honolulu", "memphis", "new orleans", "sacramento")
_US_WEST_ABBR = ("ca", "or", "wa", "nv", "az", "ut", "co", "id", "mt", "wy", "nm", "ak", "hi", "tx", "ok", "ks", "ne", "nd", "sd", "mn", "ia",
                 "mo", "ar", "la", "wi", "il", "ms", "al", "tn", "ky", "bc", "ab", "sk", "mb")
_AM_EAST = ("new york", "nyc", "new york city", "florida", "indiana", "savannah", "east coast", "mid-atlantic", "eastern time", "est", "edt",
            "ontario", "new brunswick", "newfoundland", "prince edward island", "d.c.", "washington d.c.", "washington dc", "north carolina", "south carolina", "virginia", "west virginia", "maryland", "delaware",
            "pennsylvania", "new jersey", "connecticut", "rhode island", "massachusetts", "vermont", "new hampshire", "maine", "ohio",
            "michigan", "district of columbia", "washington, d.c.", "boston", "miami", "atlanta", "philadelphia", "orlando",
            "tampa", "charlotte", "raleigh", "detroit", "cleveland", "pittsburgh", "toronto", "montreal", "ottawa", "quebec", "nova scotia",
            "colombia", "peru", "ecuador", "panama", "cuba", "jamaica", "dominican", "puerto rico", "venezuela", "bolivia", "chile", "santiago",
            "brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro", "argentina", "buenos aires", "uruguay", "paraguay", "bogota", "bogotá", "lima")
_AM_EAST_ABBR = ("ny", "fl", "ga", "nc", "sc", "va", "wv", "md", "de", "pa", "nj", "ct", "ri", "ma", "vt", "nh", "me", "oh", "mi", "dc", "on", "qc", "ns", "nb", "in")
_EUROPE_WEST = ("united kingdom", "uk", "england", "scotland", "wales", "ireland", "london", "manchester", "edinburgh", "dublin", "portugal",
                "lisbon", "spain", "madrid", "barcelona", "france", "paris", "germany", "berlin", "munich", "netherlands", "amsterdam", "belgium",
                "brussels", "luxembourg", "switzerland", "zurich", "austria", "vienna", "italy", "rome", "milan", "denmark", "copenhagen",
                "norway", "oslo", "sweden", "stockholm", "iceland", "morocco", "ghana", "senegal", "ivory coast", "nigeria", "lagos", "europe", "eu",
                "great britain", "britain", "scandinavia", "nordic", "nordics", "dach", "leeds", "glasgow", "hamburg", "frankfurt", "deutschland",
                "munchen", "cologne", "koln", "belfast", "bristol", "birmingham", "liverpool", "cardiff", "geneva", "benelux")
_EUROPE_EAST_MEA = ("poland", "warsaw", "czech", "prague", "slovakia", "hungary", "budapest", "romania", "bucharest", "bulgaria", "greece", "athens",
                    "finland", "helsinki", "estonia", "latvia", "lithuania", "ukraine", "kyiv", "belarus", "serbia", "croatia", "slovenia", "turkey",
                    "istanbul", "russia", "moscow", "israel", "tel aviv", "egypt", "cairo", "saudi", "riyadh", "uae", "united arab emirates", "dubai",
                    "abu dhabi", "qatar", "doha", "kuwait", "jordan", "lebanon", "iraq", "south africa", "johannesburg", "cape town", "kenya", "nairobi",
                    "ethiopia", "tanzania", "uganda", "zimbabwe", "zambia", "africa", "middle east", "tbilisi", "georgia (country)", "country of georgia", "republic of georgia", "armenia", "azerbaijan", "oman", "bahrain", "cyprus", "malta",
                    "czechia", "turkiye", "krakow", "baltics", "baltic", "wroclaw", "gdansk", "belgrade", "sofia", "vilnius", "riga", "tallinn", "bratislava", "zagreb")
_SOUTH_ASIA = ("india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "chennai", "pune", "kolkata", "pakistan", "karachi", "lahore",
               "bangladesh", "dhaka", "sri lanka", "colombo", "nepal", "kathmandu", "bhutan", "maldives", "uzbekistan", "kazakhstan")
_ASIA_PACIFIC = ("china", "beijing", "shanghai", "shenzhen", "hong kong", "taiwan", "taipei", "japan", "tokyo", "osaka", "korea", "seoul", "singapore",
                 "malaysia", "kuala lumpur", "indonesia", "jakarta", "bali", "thailand", "bangkok", "vietnam", "hanoi", "ho chi minh", "philippines",
                 "manila", "cambodia", "myanmar", "laos", "mongolia", "asia", "apac")
_OCEANIA = ("australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "new zealand", "auckland", "wellington", "fiji", "oceania", "anz", "nz")
_US_CANADA_GENERIC = ("united states", "usa", "u.s.", "u.s.a", "canada", "north america", "americas", "latam", "latin america")


# "Eastern Europe" is its own band; plain "europe" would otherwise file it under the western one.
_EAST_EUROPE = re.compile(r"(?<![a-z0-9])(?:central\s+and\s+)?(?:eastern|east)\s+europe(?:an)?(?![a-z0-9])")
# "(excluding US)", "except United States", "not US", "no US applicants": the clause names what is left OUT, so it is dropped before matching.
_EXCLUDE = re.compile(r"\b(?:excluding|excludes?|except(?:ing)?|outside(?:\s+of)?|other\s+than|without|not|no)\b[^,;()|/\n]*", re.I)


def _plain(text: str) -> str:
    """Accents folded to ASCII ("Zürich" -> "Zurich", "Kraków" -> "Krakow"); the word lists are ASCII."""
    return unicodedata.normalize("NFKD", text.replace("ß", "ss").replace("ł", "l")).encode("ascii", "ignore").decode()


def _rx(words: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


# Two-letter state/province codes are matched on the ORIGINAL text. Codes that are also ordinary English words ("in", "or", "me", "hi",
# "ok", "on", ...) only count when written in capitals, so "Remote, in the EU" or "Remote, or hybrid" do not become Indiana / Oregon.
_ENGLISH_ABBR = frozenset({"in", "or", "me", "hi", "ok", "on", "la", "al", "ar", "id", "ne", "co", "ma", "de", "pa", "il", "ca", "mo", "mt", "ms", "nb", "ns"})
# Codes that are also country codes (Berlin, DE / Bangalore, IN / Bogota, CO). They count only when no place is named.
_COUNTRY_CODE_ABBR = frozenset({"de", "ca", "in", "co", "il", "id", "pa", "ma", "ar", "al", "mt", "sk", "az", "mn"})


def _abbr_rx(abbrs: tuple[str, ...], codes: frozenset[str]) -> re.Pattern:
    pick = [a for a in abbrs if a in codes]
    safe = "|".join(a for a in pick if a not in _ENGLISH_ABBR)
    caps = "|".join(a.upper() for a in pick if a in _ENGLISH_ABBR)
    parts = ([f"(?i:{safe})"] if safe else []) + ([caps] if caps else [])
    return re.compile(rf",\s*(?:{'|'.join(parts)})(?![A-Za-z0-9-])")


_WW = _rx(_WORLDWIDE + ("the world",))
_ABBR_W, _ABBR_E = _abbr_rx(_US_WEST_ABBR, frozenset(_US_WEST_ABBR)), _abbr_rx(_AM_EAST_ABBR, frozenset(_AM_EAST_ABBR))
_ABBR_W_SAFE, _ABBR_E_SAFE = (_abbr_rx(a, frozenset(a) - _COUNTRY_CODE_ABBR) for a in (_US_WEST_ABBR, _AM_EAST_ABBR))
_ORDER = [("americas_east", _rx(_AM_EAST)), ("americas_west", _rx(_US_WEST)), ("europe_west", _rx(_EUROPE_WEST)),
          ("europe_east_mea", _rx(_EUROPE_EAST_MEA)), ("south_asia", _rx(_SOUTH_ASIA)), ("asia_pacific", _rx(_ASIA_PACIFIC)), ("oceania", _rx(_OCEANIA))]
_GENERIC = _rx(_US_CANADA_GENERIC)
_EMEA = _rx(("emea",))
_US_SHORT = _rx(("usa", "u.s", "america", "american"))
# "US" counts only in capitals ("Join us" / "Contact us" are not a place), or when it is the whole text ("us", "remote us").
_US_CAPS = re.compile(r"(?<![A-Za-z0-9$])US(?![A-Za-z0-9$])")
_US_WHOLE = re.compile(r"^(?:remote|hybrid|onsite|on-site)?\W*(?:in\s+)?(?:the\s+)?us\W*(?:only|based)?$")
# "Anywhere in the US" / "Anywhere, USA" limit the place; they do not mean worldwide.
_ANYWHERE_IN = re.compile(r"anywhere\s*(?:in|within|across|throughout|from)\s+(?!(?:the\s+)?world)(?:the\s+)?|anywhere\s*,\s*(?=usa|us\b|u\.s|canada|europe)")
# A US town named after a foreign city: "Paris, TX" is Texas, not France.
_NAMESAKE = re.compile(r"\b(?:paris|london|dublin|manchester|sydney|melbourne|perth|rome|athens|lima|moscow|warsaw|cairo|lisbon|madrid|vienna|berlin|portland)\b(?=\s*,\s*"
                       r"(?:(?:" + "|".join(a.upper() for a in _US_WEST_ABBR + _AM_EAST_ABBR if a not in _COUNTRY_CODE_ABBR) + r")(?![A-Za-z0-9])|"
                       r"(?i:texas|ohio|maine|georgia|florida|ontario|new hampshire|nova scotia|virginia|illinois|idaho|indiana|iowa|kentucky|tennessee)\b))", re.I)


def classify(location: str | None) -> str | None:
    """Every region the text names, as a space-separated string of BANDS keys in west-to-east order, or None when it names no place.
    "Austin, TX" -> "americas_west"; "United States" -> both American bands; "US or Europe" -> both American bands + europe_west."""
    if not location:
        return None
    orig = _NAMESAKE.sub(" ", _EXCLUDE.sub(" ", _plain(location.strip())))
    t = " " + orig.lower() + " "
    found: set[str] = set()
    if _EAST_EUROPE.search(t):
        found.add("europe_east_mea")
        t = _EAST_EUROPE.sub(" ", t)
    for key, rx in _ORDER:
        if rx.search(t):
            found.add(key)
    named = bool(found)
    w, e = (_ABBR_W_SAFE, _ABBR_E_SAFE) if named else (_ABBR_W, _ABBR_E)       # "Berlin, DE" is Germany, "Boulder, CO" is Colorado
    if w.search(orig):
        found.add("americas_west")
    if e.search(orig):
        found.add("americas_east")
    if _EMEA.search(t):
        found.update(("europe_west", "europe_east_mea"))
    if _GENERIC.search(t) or _US_CAPS.search(orig) or _US_WHOLE.match(t.strip()) or (len(t) < 60 and _US_SHORT.search(t)):
        found.update(("americas_west", "americas_east"))        # a whole country spans several zones: show it under each
    if _WW.search(_ANYWHERE_IN.sub(" ", t)):
        found.add("worldwide")
    if not found:
        return None
    return " ".join(k for k in BANDS if k in found)


def split(region: str | None) -> list[str]:
    return region.split() if region else []
