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
