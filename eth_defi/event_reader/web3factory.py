"""Web3 connection factory."""

# For typing.Protocol see https://stackoverflow.com/questions/68472236/type-hint-for-callable-that-takes-kwargs
from typing import Protocol

import requests
from web3 import Web3, HTTPProvider

from eth_defi.event_reader.fast_json_rpc import patch_web3
from eth_defi.event_reader.logresult import LogContext


class Web3Factory(Protocol):
    """Create a new Web3 connection.

    When each worker is initialised, the factory is called to get JSON-RPC connection.
    """

    def __call__(self,
                 context: LogContext,
                 ) -> Web3:
        pass


class TunedWeb3Factory(Web3Factory):
    """Create a connection"""

    def __init__(self, json_rpc_url):
        self.json_rpc_url = json_rpc_url

    def __call__(self, context: LogContext) -> Web3:
        """Create a new Web3 connection.

        - Get rid of middleware

        - Patch for ujson
        """

        # Reuse HTTPS session for HTTP 1.1 keep-alive
        session = requests.Session()

        web3 = Web3(HTTPProvider(self.json_rpc_url, session=session))

        # Enable faster ujson reads
        patch_web3(web3)

        web3.middleware_onion.clear()

        return web3
