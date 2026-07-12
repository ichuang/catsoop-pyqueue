"""Run the queue server:  python -m pyqueue  (from the repo root).

Options:
    --host HOST     bind address (overrides config)
    --port PORT     bind port (overrides config)
    --dev           dev mode: no CAT-SOOP needed; authentication trusts
                    the client-supplied username/role (for local testing
                    with the bundled demo page)
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import params as config_params  # noqa: E402
from pyqueue.queue_server import QueueApp  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pyqueue',
                                     description='CAT-SOOP queue server')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--dev', action='store_true',
                        help='trust client-supplied auth (no CAT-SOOP)')
    args = parser.parse_args(argv)

    overrides = {'SERVER': {}}
    if args.host:
        overrides['SERVER']['HOST'] = args.host
    if args.port is not None:
        overrides['SERVER']['PORT'] = args.port
    if args.dev:
        overrides['CATSOOP'] = {'API_ROOT': None}

    params = config_params.load(overrides)
    app = QueueApp(params)
    try:
        asyncio.run(app.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
