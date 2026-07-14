#!/usr/bin/env python3
"""
One-time helper: log in to Garmin interactively and print the token store.

Run this on your local machine (not on Fly) to handle MFA, then set the
output as the GARMINTOKENS Fly secret:

  python scripts/garmin_get_tokens.py | fly secrets set GARMINTOKENS=-
"""

import getpass
import sys


def main():
    try:
        from garminconnect import Garmin
    except ImportError:
        print("garminconnect not installed. Run: pip install garminconnect", file=sys.stderr)
        sys.exit(1)

    with open("/dev/tty") as tty:
        tty_out = open("/dev/tty", "w")
        tty_out.write("Garmin email: ")
        tty_out.flush()
        email = tty.readline().strip()
        password = getpass.getpass("Garmin password: ", stream=tty_out)
        tty_out.close()

    print("Logging in...", file=sys.stderr)
    client = Garmin(email=email, password=password)
    mfa_status, _ = client.login()

    if mfa_status == "needs_mfa":
        with open("/dev/tty") as tty:
            tty_out = open("/dev/tty", "w")
            tty_out.write("MFA code: ")
            tty_out.flush()
            mfa_code = tty.readline().strip()
            tty_out.close()
        client.resume_login(mfa_code)

    tokens = client.client.dumps()
    print(tokens)
    print("\nDone. Pipe this output to: fly secrets set GARMINTOKENS=-", file=sys.stderr)


if __name__ == "__main__":
    main()
