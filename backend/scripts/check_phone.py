"""Look up one real phone number with the phone services configured in .env and
score it as if it were a trade reference.

    make phone NUMBER="+1 415 555 1234" COMPANY="Northwind Foods"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vouch.agents import scoring  # noqa: E402
from vouch.agents import verification as ver  # noqa: E402
from vouch.config import get_settings  # noqa: E402
from vouch.policy import load_policy  # noqa: E402
from vouch.providers.phone import SOURCE_LABEL, PhoneProvider  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("number")
    parser.add_argument("company", nargs="?", default="")
    parser.add_argument("--applicant", default="", help="applicant name, to catch a number that belongs to them")
    parser.add_argument("--json", action="store_true", help="print the raw lookup")
    args = parser.parse_args()

    settings = get_settings()
    provider = PhoneProvider(settings)
    if not provider.live_sources:
        print("No phone services configured. Add IPQS_API_KEY, GOOGLE_MAPS_API_KEY, or Twilio credentials to .env.")
        return
    print(f"Checking {args.number} with {', '.join(SOURCE_LABEL[s] for s in provider.live_sources)}...")
    intel = (await provider.lookup(args.number)).to_dict()
    if args.json:
        print(json.dumps(intel, indent=2))
    for err in intel["errors"]:
        print(f"  ! {err}")

    policy = load_policy()
    ref = ver.PartyInput("ref", args.company or "the reference", intel["number"], None, None, None, None, None)
    checks = {"registry": {"found": False, "source": "sandbox", "officers": []}, "domain": None,
              "website": {"checked": False, "phones": []}, "phone": intel}
    sigs = scoring.phone_signals(ref, intel, [args.applicant] if args.applicant else [], policy)
    out = scoring.score_reference(ref, checks, sigs, [], [], policy)
    print()
    print(scoring.phone_summary(ref, intel) or "Nothing came back for this number.")
    print(f"\nPhone-only authenticity score: {out['score']}/100 ({out['band'].replace('_', ' ')})")
    for f in out["factors"]:
        print(f"  {'+' if f['weight'] > 0 else '-'} {f['label']}  [{f['source']}]")
    print("\nIn the app the score also uses state records, the website, domain age, and Vouch's history.")


if __name__ == "__main__":
    asyncio.run(main())
