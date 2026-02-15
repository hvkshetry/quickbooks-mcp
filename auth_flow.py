#!/usr/bin/env python3
"""One-time OAuth flow for QuickBooks Online.

Starts a temporary HTTP server on port 8010, opens the browser to Intuit's
authorization URL, receives the callback, exchanges the code for tokens,
and writes QBO_REFRESH_TOKEN and QBO_REALM_ID to .env.

Usage:
    uv run python auth_flow.py

Prerequisites:
    1. Create an app at https://developer.intuit.com
    2. Add http://localhost:8010/callback as a redirect URI
    3. Copy Client ID and Secret into .env (see .env.example)
"""

import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes

load_dotenv()

PORT = 8010
REDIRECT_URI = os.getenv("QBO_REDIRECT_URI", f"http://localhost:{PORT}/callback")


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle the OAuth callback from Intuit."""

    auth_code = None
    realm_id = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)

        if "error" in params:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            error = params["error"][0]
            self.wfile.write(f"<h1>Authorization failed</h1><p>{error}</p>".encode())
            return

        CallbackHandler.auth_code = params.get("code", [None])[0]
        CallbackHandler.realm_id = params.get("realmId", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h1>Authorization successful!</h1>"
            b"<p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, format, *args):
        """Suppress default HTTP log noise."""
        pass


def main():
    client_id = os.getenv("QBO_CLIENT_ID")
    client_secret = os.getenv("QBO_CLIENT_SECRET")
    environment = os.getenv("QBO_ENVIRONMENT", "sandbox")

    if not client_id or not client_secret:
        print("ERROR: QBO_CLIENT_ID and QBO_CLIENT_SECRET must be set in .env")
        print("  1. Go to https://developer.intuit.com")
        print("  2. Create/open your app")
        print("  3. Copy Client ID and Client Secret into .env")
        sys.exit(1)

    auth_client = AuthClient(
        client_id=client_id,
        client_secret=client_secret,
        environment=environment,
        redirect_uri=REDIRECT_URI,
    )

    # Generate authorization URL
    scopes = [Scopes.ACCOUNTING]
    auth_url = auth_client.get_authorization_url(scopes)

    print(f"\nQuickBooks Online OAuth Flow")
    print(f"{'=' * 50}")
    print(f"Environment: {environment}")
    print(f"Redirect URI: {REDIRECT_URI}")
    print(f"\nOpening browser for authorization...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")

    webbrowser.open(auth_url)

    # Start temporary server to receive callback
    print(f"Waiting for callback on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), CallbackHandler)
    server.handle_request()  # Handle exactly one request

    auth_code = CallbackHandler.auth_code
    realm_id = CallbackHandler.realm_id

    if not auth_code:
        print("ERROR: No authorization code received")
        sys.exit(1)

    print(f"\nReceived auth code, exchanging for tokens...")
    print(f"Realm ID: {realm_id}")

    # Exchange auth code for tokens
    auth_client.get_bearer_token(auth_code, realm_id=realm_id)

    refresh_token = auth_client.refresh_token
    access_token = auth_client.access_token

    if not refresh_token:
        print("ERROR: No refresh token received")
        sys.exit(1)

    # Persist to .env
    from client import _persist_token
    _persist_token(refresh_token, realm_id)

    # Also update in-memory for immediate use
    os.environ["QBO_REFRESH_TOKEN"] = refresh_token
    os.environ["QBO_REALM_ID"] = realm_id

    print(f"\nSuccess!")
    print(f"  Refresh token: {refresh_token[:20]}...")
    print(f"  Realm ID: {realm_id}")
    print(f"  Written to .env")
    print(f"\nYou can now start the MCP server:")
    print(f"  uv run python server.py")


if __name__ == "__main__":
    main()
