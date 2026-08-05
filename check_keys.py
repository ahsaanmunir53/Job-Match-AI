"""
Test your API keys against the live services.

Run:  python check_keys.py
      python check_keys.py "python developer"     (custom query)

Prints one line per source: whether the key works, how many jobs came back,
how long it took, and a sample listing. Costs one request per configured
source, so don't run it in a loop on the JSearch free tier.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import sources

BASE_DIR = Path(__file__).parent


def load_keys() -> dict:
    import os
    keys = {}
    path = BASE_DIR / "data" / "api_keys.json"
    if path.exists():
        for k, v in json.loads(path.read_text()).items():
            if isinstance(v, str) and v.strip() and not v.startswith("PASTE_") \
                    and not k.startswith("_"):
                keys[k.upper()] = v.strip()
    needed = {k for s in sources.KEYED_SOURCES.values() for k in s["keys"]}
    for name in os.environ:
        if name.upper() in needed:
            keys[name.upper()] = os.environ[name]
    return keys


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "python developer"
    keys = load_keys()

    print(f"\nQuery: {query!r}   Scope: pakistan\n" + "-" * 68)

    any_active = False
    async with sources.make_client() as client:
        for name, spec in sources.KEYED_SOURCES.items():
            missing = [k for k in spec["keys"] if k not in keys]
            if missing:
                print(f"  --  {spec['label']:<26} not configured "
                      f"(missing {', '.join(missing)})")
                continue

            any_active = True
            started = time.time()
            status, jobs = await sources.run_keyed(client, name, keys,
                                                   query, "pakistan")
            elapsed = time.time() - started

            if status["ok"]:
                print(f"  OK  {spec['label']:<26} {len(jobs):>3} jobs   "
                      f"{elapsed:>5.1f}s")
                for j in jobs[:2]:
                    print(f"        · {j['title']} @ {j['company']} "
                          f"— {j['location']}")
            else:
                print(f"  X   {spec['label']:<26} {status['error']}   "
                      f"{elapsed:>5.1f}s")

    print("-" * 68)
    if not any_active:
        print("No keys configured. Add them to data/api_keys.json.\n")
    else:
        print("Anything marked X will simply be skipped during a search —\n"
              "the other sources still run.\n")


if __name__ == "__main__":
    asyncio.run(main())
