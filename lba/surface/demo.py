"""Log in to the fake bank and print what the surface sees.

Start the bank first (in another terminal):   lba bank
Then run this:                                python -m lba.surface.demo [--headed]
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from lba.surface import BrowserSurface, Target


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    load_dotenv()
    base = os.environ.get("BANK_URL", "http://127.0.0.1:5000")
    user, password = os.environ.get("BANK_USER"), os.environ.get("BANK_PASSWORD")
    if not user or not password:
        raise SystemExit("Set BANK_USER and BANK_PASSWORD in .env (same values the bank uses).")

    with BrowserSurface(headless=not args.headed) as surface:
        surface.goto(f"{base}/login")
        surface.type(Target(css="input[name=user]"), user)
        surface.type(Target(css="input[name=pw]"), password)  # never printed
        surface.click(Target(role="button", name="Sign On"))
        surface.wait_for(Target(role="link", name="Sign Off"))
        # popup=0 switches the maintenance-popup fault off for this visit, so the
        # demo looks the same even if BANK_POPUP=1 is set in .env.
        surface.goto(f"{base}/home?popup=0")

        observation = surface.observe()
        print(f"URL:   {observation.url}")
        print(f"Title: {observation.title}")
        print(f"Screenshot: {len(observation.screenshot)} bytes (PNG)")
        print("Accessibility tree (note the search form from the iframe):")
        print(observation.tree)

        folder = Path("evidence/screenshots")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "observe_demo.png").write_bytes(observation.screenshot)
        print(f"Saved screenshot to {folder / 'observe_demo.png'}")

        print("\nFallback Targets for the two search-form elements:")
        for title, target in (
            ("Member ID box", Target(css="input[name=mid]")),
            ("Search button", Target(role="button", name="Search")),
        ):
            print(f"  {title}:")
            for candidate in surface.describe(target):
                print(f"    {candidate}")


if __name__ == "__main__":
    main()
