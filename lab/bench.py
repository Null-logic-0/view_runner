"""Start the whole lab with one command.

    python -m lab.bench                       target + 3 proxies
    python -m lab.bench --proxies 5 --verbose
    python -m lab.bench --write-proxy-file proxies.local.txt

Then, in another shell::

    automation-lab run --url http://127.0.0.1:8000/ -n 10 -d 5 -j 3 \
        --proxy-file proxies.local.txt

The target runs in a thread (its handlers block on purpose, which is how
/slow works) and the proxies run on one asyncio event loop, which is all they
need since they are pure I/O.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from lab.proxy_server import ProxyServer
from lab.target_server import TargetServer


async def _run(args: argparse.Namespace) -> None:
    target = TargetServer(args.host, args.port, access_log=args.access_log)
    proxies = [
        ProxyServer(
            args.host,
            args.proxy_base_port + index,
            delay_ms=args.delay_ms,
            fail_every=args.fail_every,
            verbose=args.verbose,
        )
        for index in range(args.proxies)
    ]

    with target:
        for proxy in proxies:
            await proxy.start()
        try:
            addresses = [proxy.address for proxy in proxies]
            if args.write_proxy_file is not None:
                args.write_proxy_file.write_text("\n".join(addresses) + "\n", encoding="utf-8")

            print(f"target   {target.base_url}", file=sys.stderr)
            print(
                "         /  /slow?ms=N  /status?code=N  /heavy?n=N  /flaky?fail=N"
                "  /__headers  /__stats",
                file=sys.stderr,
            )
            for address in addresses:
                print(f"proxy    {address}", file=sys.stderr)
            if args.write_proxy_file is not None:
                print(f"written  {args.write_proxy_file}", file=sys.stderr)
            print("\nCtrl-C to stop.", file=sys.stderr)

            await asyncio.Event().wait()
        finally:
            for proxy in proxies:
                await proxy.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab.bench", description="Run the lab bench.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="target port")
    parser.add_argument("--proxies", type=int, default=3, help="how many proxies to start")
    parser.add_argument("--proxy-base-port", type=int, default=3128)
    parser.add_argument("--delay-ms", type=int, default=0, help="latency added by every proxy")
    parser.add_argument("--fail-every", type=int, default=0, help="each proxy fails every Nth")
    parser.add_argument("--access-log", type=Path, help="target access log, one JSON per line")
    parser.add_argument(
        "--write-proxy-file", type=Path, help="write the proxy addresses in proxies.txt format"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
