"""Module for spoofing SIM card and geo-data for different countries."""

from typing import Dict

# A dictionary containing SIM card profiles for different countries.
# Each profile includes locale, timezone, numeric code, operator alpha tag, and ISO country code.
SIM_PROFILES: Dict[str, Dict[str, str]] = {
    "ID": {
        "locale": "id-ID",
        "timezone": "Asia/Jakarta",
        "numeric": "51010",
        "alpha": "Telkomsel",
        "iso": "id",
    },
    "GB": {
        "locale": "en-GB",
        "timezone": "Europe/London",
        "numeric": "23415",
        "alpha": "Vodafone",
        "iso": "gb",
    },
    "US": {
        "locale": "en-US",
        "timezone": "America/New_York",
        "numeric": "310260",
        "alpha": "T-Mobile",
        "iso": "us",
    },
    "PL": {
        "locale": "pl-PL",
        "timezone": "Europe/Warsaw",
        "numeric": "26001",
        "alpha": "Plus",
        "iso": "pl",
    },
}

DEFAULT_PROFILE = SIM_PROFILES["US"]


def get_sim_env_for_country(country_code: str) -> Dict[str, str]:
    """
    Retrieves a dictionary of environment variables for SIM and geo-spoofing
    based on the given two-letter country code.

    Args:
        country_code: The two-letter ISO country code (e.g., 'ID', 'GB').

    Returns:
        A dictionary formatted for use as environment variables in Docker,
        containing spoofed SIM and system settings. If the country is not found,
        a default profile (US) is returned.
    """
    code = country_code.upper()
    profile = SIM_PROFILES.get(code, DEFAULT_PROFILE)

    return {
        "SYS_LOCALE": profile["locale"],
        "SYS_TIMEZONE": profile["timezone"],
        "SIM_NUMERIC": profile["numeric"],
        "SIM_ALPHA": profile["alpha"],
        "SIM_ISO": profile["iso"],
    }


# Operator registry: maps normalised operator name → (numeric, alpha, iso)
# numeric = MCC+MNC as a 6-char string
_OPERATOR_MAP: Dict[str, Dict[str, str]] = {
    # USA virtual / MVNO operators
    "textnow":   {"numeric": "310260", "alpha": "TextNow",  "iso": "us"},
    "tmobile":   {"numeric": "310260", "alpha": "T-Mobile", "iso": "us"},
    "t-mobile":  {"numeric": "310260", "alpha": "T-Mobile", "iso": "us"},
    "verizon":   {"numeric": "311480", "alpha": "Verizon",  "iso": "us"},
    "att":       {"numeric": "310410", "alpha": "AT&T",     "iso": "us"},
    "at&t":      {"numeric": "310410", "alpha": "AT&T",     "iso": "us"},
    "sprint":    {"numeric": "310120", "alpha": "Sprint",   "iso": "us"},
    "lycamobile": {"numeric": "310260", "alpha": "Lycamobile", "iso": "us"},
    # UK
    "vodafone":  {"numeric": "23415",  "alpha": "Vodafone", "iso": "gb"},
    "o2":        {"numeric": "23410",  "alpha": "O2",       "iso": "gb"},
    "ee":        {"numeric": "23430",  "alpha": "EE",       "iso": "gb"},
    # Indonesia
    "telkomsel": {"numeric": "51010",  "alpha": "Telkomsel","iso": "id"},
}

# Country-level defaults (fallback when operator is unknown)
_COUNTRY_DEFAULTS: Dict[str, Dict[str, str]] = {
    "US":  {"numeric": "310260", "alpha": "T-Mobile", "iso": "us"},
    "GB":  {"numeric": "23415",  "alpha": "Vodafone", "iso": "gb"},
    "ID":  {"numeric": "51010",  "alpha": "Telkomsel","iso": "id"},
    "PL":  {"numeric": "26001",  "alpha": "Plus",     "iso": "pl"},
}


def get_sim_props_by_operator(operator_name: str, country_code: str) -> Dict[str, str]:
    """Return Docker env vars (SIM_NUMERIC, SIM_ALPHA, SIM_ISO) derived from the
    operator name exactly as returned by the SMS API.

    Priority: exact operator match → country default → hardcoded US T-Mobile.

    Args:
        operator_name: Operator string from SMS API (e.g. 'textnow', 'verizon').
        country_code:  Two-letter ISO country code (e.g. 'US', 'GB').

    Returns:
        Dict with keys SIM_NUMERIC, SIM_ALPHA, SIM_ISO.
    """
    op = operator_name.lower().strip()
    country = country_code.upper().strip()

    props = (
        _OPERATOR_MAP.get(op)
        or _COUNTRY_DEFAULTS.get(country)
        or {"numeric": "310260", "alpha": "T-Mobile", "iso": "us"}
    )

    return {
        "SIM_NUMERIC": props["numeric"],
        "SIM_ALPHA":   props["alpha"],
        "SIM_ISO":     props["iso"],
    }


def get_operator_env(country_code: str, operator_name: str) -> dict[str, str]:
    """Legacy helper — kept for backwards compatibility.

    Prefer get_sim_props_by_operator() for new call-sites: it returns
    Docker-style SIM_* keys that align with docker-compose.yml variables.
    """
    return get_sim_props_by_operator(operator_name, country_code)
