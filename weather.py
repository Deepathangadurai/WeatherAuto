#!/usr/bin/env python3
"""
Seoul Weather Email Automation (Python 3.10+)

Flow: workflow.yaml -> OpenWeather API -> format email (HTML + plain text)
      -> Gmail SMTP.

Credentials: read from environment variables when present (GitHub Actions Secrets),
             otherwise fall back to the hardcoded local values below.
Workflow configuration (location, units, subject) lives in workflow.yaml.
"""

import html
import logging
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
import yaml

# ============================================================================
# 1. CREDENTIALS
# ============================================================================
# Priority: environment variable (GitHub Actions Secret) > hardcoded local value.
# When running locally, the hardcoded values are used automatically.
# When running via GitHub Actions, the secrets injected as env vars take over.
# WARNING: Keep this file private. Do NOT share or commit real credentials publicly.

_LOCAL_API_KEY  = "73a14138aeaa4e4c3f84d915fbcf1d64"   # OpenWeather API key
_LOCAL_USERNAME = "deepathangadurai923@gmail.com"      # Gmail sender address
_LOCAL_PASSWORD = "yhon lsce gnkc iott"                # Gmail App Password
_LOCAL_EMAIL_TO = "deepathangadurai923@gmail.com"      # Recipient address

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", _LOCAL_API_KEY)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

SMTP_USERNAME = os.environ.get("SMTP_USERNAME", _LOCAL_USERNAME)
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", _LOCAL_PASSWORD)

EMAIL_TO = os.environ.get("EMAIL_TO", _LOCAL_EMAIL_TO)

# ============================================================================
# 2. WORKFLOW FILE
# ============================================================================
# Path to the external YAML file that lives next to this script.
WORKFLOW_FILE = Path(__file__).with_name("workflow.yaml")

# ============================================================================
# Constants
# ============================================================================
REQUEST_TIMEOUT = 10   # seconds, OpenWeather request
SMTP_TIMEOUT = 30      # seconds, Gmail connection

# Display units for each OpenWeather "units" value: (temperature, wind speed)
UNIT_LABELS = {
    "metric": ("°C", "m/s"),
    "imperial": ("°F", "mph"),
    "standard": ("K", "m/s"),
}

# ============================================================================
# Logging (never logs credentials)
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)
# Keep HTTP libraries quiet: their debug output could contain request URLs.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger("weather_email")


class WorkflowError(Exception):
    """Expected, user-facing error. The message is safe to display."""


# ============================================================================
# Helpers
# ============================================================================
def _clean_app_password() -> str:
    """Gmail shows app passwords in groups with spaces; remove all whitespace."""
    return "".join(SMTP_PASSWORD.split())


def _redact(text: str) -> str:
    """Mask secrets in any text that may end up in an error message."""
    secrets = {OPENWEATHER_API_KEY.strip(), SMTP_PASSWORD, _clean_app_password()}
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _is_missing(value: str, placeholder: str) -> bool:
    return not value or not value.strip() or value.strip() == placeholder


def _dig(data: Any, *path: Any, default: Any = None) -> Any:
    """Safely read a nested value, e.g. _dig(data, "main", "temp")."""
    current = data
    for key in path:
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError):
            return default
    return current


def _require(config: Any, *path: str) -> Any:
    """Return a required setting from the parsed YAML or raise a clear error."""
    current = config
    for key in path:
        if not isinstance(current, dict) or current.get(key) in (None, ""):
            raise WorkflowError(
                "ERROR: Workflow YAML is missing required setting '"
                + ".".join(path)
                + "'."
            )
        current = current[key]
    return current


def _fmt_number(value: Any, suffix: str = "", decimals: int = 1) -> str:
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "N/A"


def validate_credentials() -> None:
    """Make sure the placeholders were replaced before making any request."""
    if _is_missing(OPENWEATHER_API_KEY, "YOUR_OPENWEATHER_API_KEY"):
        raise WorkflowError("ERROR: OpenWeather API key is missing.")
    if _is_missing(SMTP_USERNAME, "yourgmail@gmail.com"):
        raise WorkflowError("ERROR: Gmail address (SMTP_USERNAME) is missing.")
    if _is_missing(SMTP_PASSWORD, "YOUR_GMAIL_APP_PASSWORD"):
        raise WorkflowError("ERROR: Gmail App Password (SMTP_PASSWORD) is missing.")
    if _is_missing(EMAIL_TO, "recipient@gmail.com"):
        raise WorkflowError("ERROR: Recipient address (EMAIL_TO) is missing.")


# ============================================================================
# 3. YAML PARSER
# ============================================================================
def load_workflow() -> dict[str, Any]:
    """Read workflow.yaml from disk, parse and validate it; return the settings the program uses."""
    if not WORKFLOW_FILE.is_file():
        raise WorkflowError(
            f"ERROR: Workflow configuration file not found: {WORKFLOW_FILE}\n"
            "Make sure 'workflow.yaml' is in the same directory as this script."
        )
    try:
        config = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkflowError(
            f"ERROR: Could not read workflow file '{WORKFLOW_FILE}': {exc}"
        ) from None
    except yaml.YAMLError as exc:
        raise WorkflowError(
            f"ERROR: Workflow YAML is invalid and could not be parsed: {exc}"
        ) from None

    if not isinstance(config, dict):
        raise WorkflowError("ERROR: Workflow YAML must be a mapping with a 'workflow' key.")

    workflow = {
        "name": str(_require(config, "workflow", "name")),
        "version": str(_dig(config, "workflow", "version", default="n/a")),
        "weather_provider": str(_require(config, "workflow", "weather", "provider")),
        "endpoint": str(_require(config, "workflow", "weather", "endpoint")),
        "city": str(_require(config, "workflow", "weather", "location", "city")),
        "country_code": str(_require(config, "workflow", "weather", "location", "country_code")),
        "units": str(_require(config, "workflow", "weather", "units")).lower(),
        "email_provider": str(_require(config, "workflow", "email", "provider")),
        "email_subject": str(_require(config, "workflow", "email", "subject")),
    }

    if workflow["weather_provider"] != "openweather":
        raise WorkflowError("ERROR: Unsupported weather provider in YAML (expected 'openweather').")
    if workflow["email_provider"] != "gmail_smtp":
        raise WorkflowError("ERROR: Unsupported email provider in YAML (expected 'gmail_smtp').")
    if workflow["units"] not in UNIT_LABELS:
        raise WorkflowError(
            "ERROR: Invalid 'units' in YAML. Use one of: " + ", ".join(UNIT_LABELS) + "."
        )
    if not workflow["endpoint"].startswith("https://"):
        raise WorkflowError("ERROR: Weather endpoint in YAML must start with https://")

    logger.info("Loaded workflow '%s' (version %s)", workflow["name"], workflow["version"])
    return workflow


# ============================================================================
# 4. OPENWEATHER REQUEST + JSON PROCESSING
# ============================================================================
def parse_weather(data: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    """Extract the needed fields from the OpenWeather JSON, tolerating gaps."""
    if not isinstance(data, dict):
        raise WorkflowError("ERROR: Unexpected response format received from OpenWeather.")

    core_paths = {
        "location": ("name",),
        "temperature": ("main", "temp"),
        "feels_like": ("main", "feels_like"),
        "condition": ("weather", 0, "description"),
        "humidity": ("main", "humidity"),
        "wind_speed": ("wind", "speed"),
    }
    # Optional extras used only to make the email look nicer (theme, icon, time).
    extra_paths = {
        "condition_id": ("weather", 0, "id"),
        "icon": ("weather", 0, "icon"),
        "timestamp": ("dt",),
        "tz_offset": ("timezone",),
    }

    values: dict[str, Any] = {}
    missing: list[str] = []
    for key, path in core_paths.items():
        value = _dig(data, *path)
        values[key] = value
        if value is None:
            missing.append(key)
    for key, path in extra_paths.items():
        values[key] = _dig(data, *path)

    if len(missing) == len(core_paths):
        raise WorkflowError(
            "ERROR: OpenWeather response did not contain any expected weather fields."
        )
    if missing:
        logger.warning("OpenWeather response is missing field(s): %s", ", ".join(missing))

    if not values["location"]:
        values["location"] = workflow["city"]  # fall back to the YAML city

    return values


def get_weather(workflow: dict[str, Any]) -> dict[str, Any]:
    """Call the OpenWeather API and return the extracted weather values."""
    params = {
        "q": f"{workflow['city']},{workflow['country_code']}",
        "appid": OPENWEATHER_API_KEY.strip(),
        "units": workflow["units"],
    }

    try:
        response = requests.get(workflow["endpoint"], params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise WorkflowError(
            f"ERROR: The request to OpenWeather timed out after {REQUEST_TIMEOUT} seconds. "
            "Check your internet connection and try again."
        ) from None
    except requests.exceptions.ConnectionError as exc:
        raise WorkflowError(
            "ERROR: Could not connect to OpenWeather. Check your internet connection. "
            f"({type(exc).__name__})"
        ) from None
    except requests.exceptions.RequestException as exc:
        # Messages from requests can contain the full URL (with the API key), so redact.
        raise WorkflowError(
            f"ERROR: Request to OpenWeather failed: {type(exc).__name__}: {_redact(str(exc))}"
        ) from None

    status = response.status_code
    if status == 401:
        raise WorkflowError(
            "ERROR: OpenWeather rejected the API key (HTTP 401). Check OPENWEATHER_API_KEY; "
            "newly created keys can take a while to activate."
        )
    if status == 404:
        raise WorkflowError(
            f"ERROR: Location '{params['q']}' was not found by OpenWeather (HTTP 404). "
            "Check the city and country_code in the YAML."
        )
    if status == 429:
        raise WorkflowError(
            "ERROR: OpenWeather rate limit exceeded (HTTP 429). Wait a while and try again."
        )

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        raise WorkflowError(f"ERROR: OpenWeather returned an unexpected HTTP status {status}.") from None

    try:
        data = response.json()
    except ValueError:
        raise WorkflowError("ERROR: OpenWeather returned a response that is not valid JSON.") from None

    return parse_weather(data, workflow)


# ============================================================================
# 5. EMAIL FORMATTING (plain-text fallback + responsive HTML template)
# ============================================================================
def _theme_for(condition_id: Any, icon: Any) -> tuple[str, str, str]:
    """Pick (emoji, gradient start, gradient end) based on the weather condition."""
    is_night = str(icon or "").endswith("n")
    try:
        cid = int(condition_id)
    except (TypeError, ValueError):
        cid = 800 if icon is None else -1

    if 200 <= cid < 300:
        return "⛈️", "#41295a", "#2f0743"        # thunderstorm
    if 300 <= cid < 400:
        return "🌦️", "#4b6a88", "#7fa6c9"        # drizzle
    if 500 <= cid < 600:
        return "🌧️", "#314755", "#26a0da"        # rain
    if 600 <= cid < 700:
        return "❄️", "#5c7fa3", "#8fb3d9"        # snow
    if 700 <= cid < 800:
        return "🌫️", "#6b7b8c", "#9aa9b8"        # mist / fog / haze
    if cid == 800:
        return ("🌙", "#141e30", "#243b55") if is_night else ("☀️", "#2193b0", "#6dd5ed")
    if cid == 801 or cid == 802:
        return ("☁️", "#2c3e60", "#4a6fa5") if is_night else ("⛅", "#3a7bd5", "#6aa7e8")
    if 803 <= cid < 900:
        return "☁️", "#606c88", "#3f4c6b"        # broken / overcast clouds
    return "🌡️", "#2193b0", "#6dd5ed"            # default


def _local_time(weather: dict[str, Any]) -> tuple[datetime, str]:
    """Observation time in the city's own timezone (uses OpenWeather's UTC offset)."""
    try:
        offset = int(weather.get("tz_offset") or 0)
        tz = timezone(timedelta(seconds=offset))
        ts = weather.get("timestamp")
        moment = datetime.fromtimestamp(int(ts), tz) if ts is not None else datetime.now(tz)
    except (TypeError, ValueError, OverflowError, OSError):
        offset = 0
        moment = datetime.now(timezone.utc)
    return moment, f"UTC{offset / 3600:+g}"


def _stat_card(icon: str, label: str, value: str) -> str:
    """One small rounded stat tile (table-cell based for email-client compatibility)."""
    return (
        '<td align="center" width="33%" valign="top" '
        'style="background-color:#f4f7fb;border-radius:12px;padding:16px 8px;">'
        f'<div style="font-size:22px;line-height:1;">{icon}</div>'
        f'<div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;'
        f'color:#7b8794;margin-top:8px;">{html.escape(label)}</div>'
        f'<div style="font-size:18px;font-weight:700;color:#1f2933;margin-top:4px;">'
        f'{html.escape(value)}</div>'
        "</td>"
    )


def format_email(weather: dict[str, Any], workflow: dict[str, Any]) -> EmailMessage:
    """Build the EmailMessage (plain text + HTML) from the weather values and YAML settings."""
    temp_unit, wind_unit = UNIT_LABELS[workflow["units"]]
    esc = html.escape

    location = str(weather["location"])
    condition = weather["condition"]
    condition_text = str(condition).title() if condition else "N/A"

    temp = _fmt_number(weather["temperature"], f" {temp_unit}")
    feels = _fmt_number(weather["feels_like"], f" {temp_unit}")
    humidity = _fmt_number(weather["humidity"], "%", decimals=0)
    wind = _fmt_number(weather["wind_speed"], f" {wind_unit}")

    moment, tz_label = _local_time(weather)
    time_text = f"{moment:%A, %d %B %Y, %H:%M} ({tz_label})"

    emoji, color_a, color_b = _theme_for(weather.get("condition_id"), weather.get("icon"))
    footer_note = "This weather information was retrieved automatically from OpenWeather."

    # ---- Plain-text version (fallback for clients that do not show HTML) ----
    text_body = "\n".join(
        [
            "🌤️ Weather Update",
            "",
            f"Location: {location}",
            "",
            f"Temperature: {temp}",
            f"Feels Like: {feels}",
            f"Condition: {condition_text}",
            f"Humidity: {humidity}",
            f"Wind Speed: {wind}",
            "",
            f"Observed: {time_text}",
            "",
            footer_note,
        ]
    )

    # ---- HTML version (inline CSS + tables so Gmail/Outlook render it well) ----
    temp_number = _fmt_number(weather["temperature"])
    temp_big = (
        f'{esc(temp_number)}<span style="font-size:24px;font-weight:500;"> {esc(temp_unit)}</span>'
        if temp_number != "N/A"
        else "N/A"
    )
    preheader = f"{location}: {temp}, {condition_text}"
    cards = (
        _stat_card("🌡️", "Feels like", feels)
        + _stat_card("💧", "Humidity", humidity)
        + _stat_card("💨", "Wind", wind)
    )
    font = "-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{esc(workflow['email_subject'])}</title>
</head>
<body style="margin:0;padding:0;background-color:#eef2f7;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#eef2f7;">{esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#eef2f7;">
  <tr>
    <td align="center" style="padding:28px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;max-width:600px;background-color:#ffffff;border-radius:18px;overflow:hidden;font-family:{font};">

        <!-- Header banner -->
        <tr>
          <td align="center"
              style="background-color:{color_a};background-image:linear-gradient(135deg,{color_a},{color_b});padding:36px 24px 32px;color:#ffffff;">
            <div style="font-size:12px;letter-spacing:3px;text-transform:uppercase;opacity:0.85;">Weather Update</div>
            <div style="font-size:24px;font-weight:600;margin-top:8px;">📍 {esc(location)}</div>
            <div style="font-size:68px;line-height:1;margin:22px 0 10px;">{emoji}</div>
            <div style="font-size:56px;font-weight:700;line-height:1.1;">{temp_big}</div>
            <div style="font-size:19px;margin-top:8px;opacity:0.95;">{esc(condition_text)}</div>
          </td>
        </tr>

        <!-- Stat tiles -->
        <tr>
          <td style="padding:24px 20px 8px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="border-collapse:separate;border-spacing:8px 0;">
              <tr>{cards}</tr>
            </table>
          </td>
        </tr>

        <!-- Observation time -->
        <tr>
          <td align="center" style="padding:16px 24px 24px;color:#52606d;font-size:14px;">
            🕒 Observed {esc(time_text)}
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td align="center" style="background-color:#f4f7fb;padding:18px 24px;color:#7b8794;font-size:12px;line-height:1.6;">
            {esc(footer_note)}<br>
            Sent by {esc(workflow['name'])}
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""

    message = EmailMessage()
    message["From"] = SMTP_USERNAME.strip()
    message["To"] = EMAIL_TO.strip()
    message["Subject"] = workflow["email_subject"]
    message.set_content(text_body, charset="utf-8")                 # plain-text part
    message.add_alternative(html_body, subtype="html", charset="utf-8")  # HTML part
    return message


# ============================================================================
# 6. GMAIL SMTP
# ============================================================================
def send_email(message: EmailMessage) -> None:
    """Send the message through Gmail using STARTTLS."""
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USERNAME.strip(), _clean_app_password())
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise WorkflowError(
            "ERROR: Gmail authentication failed. Use a Gmail *App Password* "
            "(requires 2-Step Verification), not your normal password, and make sure "
            "SMTP_USERNAME is correct."
        ) from None
    except smtplib.SMTPRecipientsRefused:
        raise WorkflowError("ERROR: The recipient address was refused by the mail server.") from None
    except smtplib.SMTPException as exc:
        raise WorkflowError(f"ERROR: SMTP error while sending email ({type(exc).__name__}).") from None
    except (OSError, UnicodeError) as exc:
        # Covers DNS failures, timeouts, refused connections, TLS problems.
        raise WorkflowError(
            f"ERROR: Could not connect to or communicate with {SMTP_HOST}:{SMTP_PORT} "
            f"({type(exc).__name__})."
        ) from None


# ============================================================================
# 7. MAIN
# ============================================================================
def main() -> int:
    try:
        logger.info("Loading workflow configuration")
        workflow = load_workflow()

        validate_credentials()

        logger.info("Requesting weather data for %s", workflow["city"])
        weather = get_weather(workflow)
        logger.info("Weather data received successfully")

        logger.info("Preparing email")
        message = format_email(weather, workflow)

        logger.info("Connecting to Gmail SMTP")
        send_email(message)
        logger.info("Email sent successfully")
        return 0

    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # last-resort guard; never prints details that could hold secrets
        print(f"ERROR: Unexpected failure ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    if exit_code != 0:
        sys.exit(exit_code)