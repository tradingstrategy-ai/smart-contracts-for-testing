"""Configuring and managing multiple JSON-RPC provider connections."""

import logging
from typing import List

from urllib3.util import parse_url, Url
from web3 import Web3, HTTPProvider

from eth_defi.chain import install_chain_middleware
from eth_defi.event_reader.fast_json_rpc import patch_provider
from eth_defi.provider.fallback import FallbackProvider
from eth_defi.provider.mev_blocker import MEVBlockerProvider
from eth_defi.provider.named import NamedProvider, get_provider_name


logger = logging.getLogger(__name__)


class MultiProviderConfigurationError(Exception):
    """Could not parse URL soup for configuring web3"""


class MultiProviderWeb3(Web3):
    """A web3 instance that knows about multiple RPC endpoints it is using.

    - This is either using or not using :py:class:`eth_defi.provider.mev_blocker.MEVBlockerProvider`
      for making transactions to prevent frontrunning

    - There might be several providers for reading on-chain data
    """

    def get_transact_provider(self) -> NamedProvider:
        """Get active transact provider."""
        provider = self.provider
        if isinstance(provider, MEVBlockerProvider):
            return provider.transact_provider

        return self.get_call_provider().get_active_provider()

    def get_call_provider(self) -> NamedProvider:
        """Get active call provider."""
        provider = self.provider
        if isinstance(provider, MEVBlockerProvider):
            fallback_provider = provider.call_provider
            assert isinstance(fallback_provider, FallbackProvider), f"Got: {fallback_provider}"
            return fallback_provider.get_active_provider()
        else:
            assert isinstance(provider, FallbackProvider), f"Got: {provider}"
            return provider.get_active_provider()

    def get_fallback_provider(self) -> FallbackProvider:
        """Get the fallback provider multiplexer."""
        provider = self.provider
        if isinstance(provider, MEVBlockerProvider):
            fallback_provider = provider.call_provider
            assert isinstance(fallback_provider, FallbackProvider), f"Got: {fallback_provider}"
            return fallback_provider
        else:
            assert isinstance(provider, FallbackProvider), f"Got: {provider}"
            return provider

    def switch_to_next_call_provider(self):
        """Recycles to the next call provider (if available)."""
        self.get_fallback_provider().switch_provider()


def create_multi_provider_web3(
        configuration_line: str,
        fallback_sleep=0.1,
        fallback_backoff=1.1,
) -> MultiProviderWeb3:
    """Create a Web3 instance with multi-provider support.

    Create a complex Web3 connection manager that

    - Supports fail-overs to different providers

    - Can have a special execution endpoint
      for MEV protection

    - HTTP providers are monkey-patched for faster uJSON reading

    - HTTP providers have middleware cleared and chain middleware installed

    The configuration line is a whitespace separated list of URLs (spaces, newlines, etc.)
    using mini configuration language.

    - If any of the protocols have `mev+` prefix like `mev+https` then this
      endpoint is used for the execution.

    :param configuration_line:
        Configuration line from an environment variable, config file or similar.

    :param fallback_sleep:
        Seconds between JSON-RPC call retries.

    :param fallback_backoff:
        Sleep increase multiplier.

    :return:
        Configured Web3 instance with multiple providers
    """

    assert type(configuration_line) == str

    items = configuration_line.split()

    urls: List[Url] = []
    for parsable in items:

        parsable = parsable.strip()

        try:
            url = parse_url(parsable)
        except Exception as e:
            raise MultiProviderConfigurationError(f"Could not parse JSON-RPC configuration URL: {parsable}")

        if not url.scheme:
            raise MultiProviderConfigurationError(f"Bad URL: {parsable}")

        if url in urls:
            raise MultiProviderConfigurationError(f"Entry appears twice: {url}")

        urls.append(url)

    if len(urls) == 0:
        raise MultiProviderConfigurationError(f"No configured endpoints")

    transact_endpoints = [url.url.replace("mev+", "") for url in urls if url.scheme.startswith("mev+")]
    call_endpoints = [url.url for url in urls if not url.scheme.startswith("mev+")]

    if len(transact_endpoints) > 1:
        raise MultiProviderConfigurationError(f"Only one execution endpoint can be specified, got {transact_endpoints}")

    if len(call_endpoints) < 0:
        raise MultiProviderConfigurationError(f"At least one call endpoint must be specified, configuration was {configuration_line}")

    call_providers = [HTTPProvider(url) for url in call_endpoints]

    # Do uJSON patching
    for p in call_providers:
        _fix_provider(p)

    fallback_provider = FallbackProvider(call_providers, sleep=fallback_sleep, backoff=fallback_backoff)
    transact_provider = None
    if len(transact_endpoints) > 0:
        transact_endpoint = transact_endpoints[0]
        transact_provider = HTTPProvider(transact_endpoint)

        _fix_provider(transact_provider)

        provider = MEVBlockerProvider(
            call_provider=fallback_provider,
            transact_provider=transact_provider,
        )
    else:
        provider = fallback_provider

    logger.info("Configuring MultiProviderWeb3. Call providers: %s, transact providers %s",
                [get_provider_name(c) for c in call_providers],
                get_provider_name(transact_provider) if transact_provider else "-",
                )

    web3 = MultiProviderWeb3(provider)

    web3.middleware_onion.clear()

    # Note that this triggers the first RPC call here
    install_chain_middleware(web3)

    return web3


def _fix_provider(provider: HTTPProvider):
    provider.middlewares.clear()
    patch_provider(provider)