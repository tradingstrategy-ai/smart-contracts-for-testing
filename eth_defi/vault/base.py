"""Generic Vault adapter base classes.

- Create unified interface across different vault protocols and their investment flows

- Helps to create automated trading agents against any vault easily

- Handle both trading (asset management role) and investor management (deposits/redemptions)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import TypedDict

from eth.typing import BlockRange
from eth_typing import BlockIdentifier, HexAddress
from web3 import Web3

from eth_defi.token import TokenAddress, fetch_erc20_details, TokenDetails


@dataclass(slots=True, frozen=True)
class VaultSpec:
    """Unique id for a vault"""

    #: Ethereum chain id
    chain_id: int

    #: Vault smart contract address or whatever is the primary address for unravelling a vault deployment for a vault protocol
    vault_address: HexAddress

    def __post_init__(self):
        assert isinstance(self.chain_id, int)
        assert isinstance(self.vault_address, str)
        assert self.vault_address.startswith("0x")


class VaultInfo(TypedDict):
    """Vault-protocol specific intormation about the vault.

    - A dictionary of data we gathered about the vault deployment,
      like various smart contracts associated with the vault

    - Not standardised yet
    """


class VaultDeploymentParameters(TypedDict):
    """Input needed to deploy a vault."""


@dataclass
class TradingUniverse:
    """Describe assets vault can manage.

    - Because of brainrotten and awful ERC-20 token standard, the vault does not know what tokens it owns
      and this needs to be specific offchain
    """

    spot_token_addresses: set[TokenAddress]


@dataclass
class VaultPortfolio:
    """Get the vault asset balances.

    - Takes :py:class:`TradingUniverse` as an input and resolves all relevant balances the vault holds for this trading universe

    - Because of brainrotten and awful ERC-20 token standard, the vault does not know what tokens it owns
      and this needs to be specific offchain

    - See :py:meth:`VaultBase.fetch_portfolio`
    """

    spot_erc20: dict[HexAddress, Decimal]

    def __post_init__(self):
        for token, value in self.spot_erc20.items():
            assert type(token) == str
            assert isinstance(value, Decimal)

    @property
    def tokens(self) -> set[HexAddress]:
        """Get list of tokens held in this portfolio"""
        return set(self.spot_erc20.keys())

    def is_spot_only(self) -> bool:
        """Do we have only ERC-20 hold positions in this portfolio"""
        return True  # Other positiosn not supported yet

    def get_position_count(self):
        return len(self.spot_erc20)

    def get_raw_spot_balances(self, web3: Web3) -> dict[HexAddress, int]:
        """Convert spot balances to raw token balances"""
        chain_id = web3.eth.chain_id
        return {addr: fetch_erc20_details(web3, addr, chain_id=chain_id).convert_to_raw(value) for addr, value in self.spot_erc20.items()}



class VaultFlowManager(ABC):
    """Manage deposit/redemption events

    - Create a replay of flow events that happened for a vault within a specific block range

    - Not implemented yet
    """

    @abstractmethod
    def fetch_pending_deposits(
        self,
        range: BlockRange,
    ) -> None:
        """Read incoming pending deposits."""

    @abstractmethod
    def fetch_pending_redemptions(
        self,
        range: BlockRange,
    ) -> None:
        """Read outgoing pending withdraws."""

    @abstractmethod
    def fetch_processed_deposits(
        self,
        range: BlockRange,
    ) -> None:
        """Read incoming pending deposits."""

    @abstractmethod
    def fetch_processed_redemptions(
        self,
        vault: VaultSpec,
        range: BlockRange,
    ) -> None:
        """Read outgoing pending withdraws."""


class VaultBase(ABC):
    """Base class for vault protocol adapters.

    Allows automated interaction with different `vault protocols <https://tradingstrategy.ai/glossary/vault>`__.

    Supported protocols include

    - Velvet Capital :py:class:`eth_defi.velvet.vault.VelvetVault`

    - Lagoon Finance :py:class:`eth_defi.lagoon.vault.LagoonVault`

    Code exists, but does not confirm the interface yet:

    - Enzyme Finance :py:class:`eth_defi.lagoon.enzyme.vault.Vault`

    What this wrapper class does:

    - Takes :py:class:`VaultSpec` as a constructor argument and builds a proxy class
      for accessing the vault based on this

    Vault functionality that needs to be supported

    - Fetching the current balances, deposits or redemptions

        - Either using naive polling approach with :py:method:`fetch_portfolio`
        - Listen to vault events for deposits and redemptions using :py:meth:`get_flow_manager`

    - Get vault information with :py:method:`fetch_info`
        - No standardised data structures or functions yet

    - Build a swap through a vault
        - No standardised data structure yet

    - Update vault position valuations
        - No standardised data structure yet

    For code examples see `tests/lagoon` and `tests/velvet`.

    Integration check list

    - [ ] read vault core info
    - [ ] read vault investors
    - [ ] read vault share price
    - [ ] read vault share token
    - [ ] read all positions
    - [ ] read NAV
    - [ ] read pending redemptions to know how much USDC we will need for the next settlement cycles
    - [ ] deposit integration test
    - [ ] redemption integration
    - [ ] swap integration test
    - [ ] re-valuation integration test
    - [ ] only asset manager allowed to swap negative test
    - [ ] only valuation commitee allowed to update vault valuations (if applicable)
    - [ ] can redeem if enough USDC to settle
    - [ ] cannot redeem not enough USDC to settle
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Vault name."""
        pass

    @property
    @abstractmethod
    def symbol(self) -> str:
        """Vault share token symbol"""
        pass

    @abstractmethod
    def has_block_range_event_support(self) -> bool:
        """Can we query delta changes by block ranges."""

    @abstractmethod
    def fetch_portfolio(
        self,
        universe: TradingUniverse,
        block_identifier: BlockIdentifier | None = None,
    ) -> VaultPortfolio:
        """Read the current token balances of a vault.

        - SHould be supported by all implementations
        """

    @abstractmethod
    def fetch_info(self) -> VaultInfo:
        """Read vault parameters from the chain.

        Use :py:meth:`info` property for cached access.
        """

    @abstractmethod
    def get_flow_manager(self) -> VaultFlowManager:
        """Get flow manager to read individial events.

        - Only supported if :py:meth:`has_block_range_event_support` is True
        """

    @abstractmethod
    def fetch_denomination_token(self) -> TokenDetails:
        """Use :py:method:`denomination_token` to access"""

    @abstractmethod
    def fetch_nav(self) -> Decimal:
        """Fetch the most recent onchain NAV value.

        :return:
            Vault NAV, denominated in :py:meth:`denomination_token`
        """

    @cached_property
    def denomination_token(self) -> TokenDetails:
        return self.fetch_denomination_token()

    @abstractmethod
    def fetch_share_token(self) -> TokenDetails:
        """Use :py:method:`share_token` to access"""

    @cached_property
    def share_token(self) -> TokenDetails:
        """ERC-20 that presents vault shares."""
        return self.fetch_share_token()

    @cached_property
    def info(self) -> VaultInfo:
        """Get info dictionary related to this deployment."""
        return self.fetch_info()
