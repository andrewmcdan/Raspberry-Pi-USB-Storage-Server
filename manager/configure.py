"""Generate a local Compose environment without printing credentials."""
import argparse
import getpass
import secrets
from pathlib import Path
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.env')
    parser.add_argument('--url', default='https://piusb.example.internal')
    parser.add_argument('--test', action='store_true', help='Generate a random administrator password for tests')
    args = parser.parse_args()
    password = secrets.token_urlsafe(32) if args.test else getpass.getpass('Manager administrator password: ')
    if len(password) < 12:
        raise SystemExit('Use at least 12 characters')
    with Path(args.output).open('x') as stream:
        # Single quoting prevents Compose from interpolating password-hash dollar signs.
        stream.write(f"POSTGRES_PASSWORD={secrets.token_hex(24)}\nSECRET_KEY={secrets.token_hex(32)}\n"
                     f"ADMIN_PASSWORD_HASH='{generate_password_hash(password)}'\nPUBLIC_URL={args.url}\n")
    print('Created ' + args.output + '; keep it private and back it up securely.')


if __name__ == '__main__':
    main()
