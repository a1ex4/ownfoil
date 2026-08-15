#!/usr/bin/env python3
"""Record real client traffic into tests/captures/, one scenario at a time.

    python tests/capture/run_capture.py --client tinfoil
    python tests/capture/run_capture.py --client sphaira --scenario download
    python tests/capture/run_capture.py --list

Point the client at the printed url, follow the instruction for each scenario, press Enter,
and the exchange lands in tests/captures/<client>/<scenario>.json with identifying data
already replaced. Scenarios are independent: any one can be recaptured on its own.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WORKDIR = os.path.join(HERE, ".workdir")
CAPTURES = os.path.join(REPO, "tests", "captures")

SEP = "═" * 78


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", choices=("tinfoil", "cyberfoil", "sphaira"))
    parser.add_argument("--scenario", action="append",
                        help="capture only this scenario; repeatable")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8466)
    parser.add_argument("--fresh", action="store_true",
                        help="discard the capture workdir (db, settings, pseudonym map)")
    return parser.parse_args()


def main():
    args = parse_args()
    # nsz's Print module parses sys.argv when settings imports it; hide our flags from it.
    sys.argv = sys.argv[:1]

    sys.path.insert(0, os.path.join(REPO, "app"))
    sys.path.insert(0, REPO)

    # Before the first app import: constants.py freezes CONFIG_DIR at import time, and
    # scenarios -> fixture -> constants. Getting this wrong points the capture shop at the
    # real install and lets it seed fixture accounts into it.
    if args.fresh and os.path.exists(WORKDIR):
        shutil.rmtree(WORKDIR)
    os.environ["OWNFOIL_CONFIG_DIR"] = os.path.join(WORKDIR, "config")

    import scenarios

    if args.list:
        for scenario in scenarios.SCENARIOS:
            print(f"  {scenario.name:24} {scenario.title}")
            print(f"  {'':24} clients: {', '.join(scenario.clients)}")
        return 0

    if not args.client:
        print("--client is required (or use --list)")
        return 2

    selected = scenarios.for_client(args.client)
    if args.scenario:
        wanted = {name for arg in args.scenario for name in arg.split(",")}
        unknown = wanted - set(scenarios.BY_NAME)
        if unknown:
            print(f"unknown scenario(s): {', '.join(sorted(unknown))}")
            return 2
        selected = [s for s in selected if s.name in wanted]
    if not selected:
        print(f"no scenarios apply to {args.client}")
        return 2

    from server import CaptureServer  # after OWNFOIL_CONFIG_DIR is set

    server = CaptureServer(WORKDIR)
    server.serve(args.host, args.port)
    url = shop_url(args.port)

    print(f"\n{SEP}\nCapturing {args.client}: {len(selected)} scenario(s)")
    print(f"Shop url   {url}")
    print(f"Captures   {os.path.join(CAPTURES, args.client)}")
    print(SEP)

    written = []
    for index, scenario in enumerate(selected, 1):
        server.apply(scenario, args.client)
        print(f"\n{SEP}\n[{index}/{len(selected)}] {scenario.name} - {scenario.title}")
        if scenario.credentials:
            user, password = scenario.credentials
            print(f"  credentials  user '{user}'  password '{password}'")
        else:
            print("  credentials  none")
        print(f"  url          {shop_path(url, scenario.path)}")
        print(f"  do           {scenario.instruction}")
        print(f"  expect       {scenario.expect}")
        try:
            input("\n  press Enter when the client is done (Ctrl-C to skip) ... ")
        except KeyboardInterrupt:
            print("\n  skipped")
            continue
        path, count = server.write(scenario, args.client, CAPTURES)
        print(f"  -> {count} exchange(s) written to {os.path.relpath(path, REPO)}")
        written.append((scenario.name, count))

    print(f"\n{SEP}")
    for name, count in written:
        flag = "" if count else "   <- nothing captured"
        print(f"  {name:24} {count} exchange(s){flag}")
    return 0


def shop_url(port):
    from utils import get_lan_ip

    return f"http://{get_lan_ip() or '127.0.0.1'}:{port}"


def shop_path(url, path):
    return f"{url}/{path}".rstrip("/")


if __name__ == "__main__":
    sys.exit(main())
