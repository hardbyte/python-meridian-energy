"""Constants for the Meridian Energy API client.

Auth endpoints, the Firebase project key, and GraphQL URL were taken from
Meridian's public app.meridianenergy.nz web bundle. There is no published
API documentation.
"""

from __future__ import annotations

BRAND = "meridian"

# Meridian's own Firebase Web API key (project meridian-retail-ciam). Not a
# secret: Firebase Web API keys only identify the project to Google's client
# SDKs; access is controlled by Firebase Auth/Security Rules, and Meridian's
# own apps ship this same key to every user.
FIREBASE_API_KEY = "AIzaSyCYCKXQhGmo7haJxAAyO_7mIPrV7jtxsK8"

AUTH_BASE_URL = "https://auth.meridianenergy.nz"
IDENTITY_TOOLKIT_URL = "https://identitytoolkit.googleapis.com/v1"
SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
GRAPHQL_URL = "https://api.meridianenergy.nz/v1/graphql/"

# Required by the OTP email-connector endpoint (empty string is rejected).
DEFAULT_REDIRECT_URL = "https://app.meridianenergy.nz"

DEFAULT_TIMEZONE = "Pacific/Auckland"

CLIENT_HEADERS = {"X-Client-Platform": "web"}
