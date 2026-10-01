"""One-time Google Calendar OAuth: creates token.json and prints it for CALENDAR_CREDS_JSON.

Prerequisite: an OAuth client (Desktop app) downloaded as credentials.json from
https://console.cloud.google.com/apis/credentials with the Calendar API enabled.
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main() -> None:
    secrets = Path(sys.argv[1] if len(sys.argv) > 1 else "credentials.json")
    if not secrets.exists():
        sys.exit(f"{secrets} not found. Download an OAuth 'Desktop app' client JSON from the Google Cloud console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    Path("token.json").write_text(creds.to_json())
    print("\nSaved token.json. For Cloud Run, store this single line as the CALENDAR_CREDS_JSON secret:\n")
    print(json.dumps(json.loads(creds.to_json())))


if __name__ == "__main__":
    main()
