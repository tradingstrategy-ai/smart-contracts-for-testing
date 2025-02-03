"""Vault adapter for Lagoon Finance protocol."""

import logging
from dataclasses import asdict
from decimal import Decimal
from functools import cached_property

from eth.typing import BlockRange
from eth_typing import HexAddress, BlockIdentifier, ChecksumAddress


from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract
from web3.contract.contract import ContractFunction

from eth_defi.balances import fetch_erc20_balances_fallback
from eth_defi.vault.base import VaultBase, VaultSpec, VaultInfo, TradingUniverse, VaultPortfolio, VaultFlowManager

from safe_eth.safe import Safe

from ..abi import get_deployed_contract, encode_function_call, present_solidity_args, get_function_selector
from ..safe.safe_compat import create_safe_ethereum_client
from ..token import TokenDetails, fetch_erc20_details
from ..trace import assert_transaction_success_with_explanation

logger = logging.getLogger(__name__)

#: How much gas we use for valuation post
DEFAULT_LAGOON_POST_VALUATION_GAS = 500_000

#: How much gas we use for valuation post
DEFAULT_LAGOON_SETTLE_GAS = 500_000


class LagoonVaultInfo(VaultInfo):
    """Capture information about Lagoon vault deployment."""

    #: The ERC-20 token that nominates the vault assets
    asset: HexAddress

    #: Lagoon vault deployment info
    safe: HexAddress
    #: Lagoon vault deployment info
    whitelistManager: HexAddress  # Can be 0x0000000000000000000000000000000000000000
    #: Lagoon vault deployment info
    feeReceiver: HexAddress
    #: Lagoon vault deployment info
    feeRegistry: HexAddress
    #: Lagoon vault deployment info
    valuationManager: HexAddress

    #: Safe multisig core info
    address: ChecksumAddress
    #: Safe multisig core info
    fallback_handler: ChecksumAddress
    #: Safe multisig core info
    guard: ChecksumAddress
    #: Safe multisig core info
    master_copy: ChecksumAddress
    #: Safe multisig core info
    modules: list[ChecksumAddress]
    #: Safe multisig core info
    nonce: int
    #: Safe multisig core info
    owners: list[ChecksumAddress]
    #: Safe multisig core info
    threshold: int
    #: Safe multisig core info
    version: str


class LagoonVault(VaultBase):
    """Python interface for interacting with Lagoon Finance vaults.

    For information see :py:class:`~eth_defi.vault.base.VaultBase` base class documentation.

    Notes

    - Vault contract knows about Safe, Safe does not know about the Vault

    - Ok so for settlement you dont have to worry about this metric, the only thing you have to value is the assets inside the safe (what you currently have under management) and update the NAV of the vault by calling updateNewTotalAssets (ex: if you have 1M inside the vault and 500K pending deposit you only need to call updateTotalAssets with the 1M that are currently inside the safe). Then, to settle you just call settleDeposit and the vault calculate everything for you.

    - To monitor the pending deposits it's a bit more complicated. You have to check the balanceOf the pendingSilo contract (0xAD1241Ba37ab07fFc5d38e006747F8b92BB217D5) in term of underlying (here USDC) for pending deposit and in term of shares (so the vault itself) for pending withdraw requests

    Lagoon tokens can be in
    - Safe: Tradeable assets
    - Silo: pending deposits (USDC)
    - Vault: pending redemptions (USDC)
    - User wallets: after `deposit()` have been called share tokens are moved to the user wallet
    """

    def __init__(
        self,
        web3: Web3,
        spec: VaultSpec,
        trading_strategy_module_address: HexAddress | None = None,
    ):
        """
        :param spec:
            Address must be Lagoon vault  address (not Safe address)

        :param trading_strategy_module_address:
            TradingStrategyModuleV0 enabled on Safe for automated trading.

            If not given, not known.
        """
        assert isinstance(web3, Web3)
        assert isinstance(spec, VaultSpec)
        self.web3 = web3
        self.spec = spec
        self.trading_strategy_module_address = trading_strategy_module_address

    def __repr__(self):
        return f"<Lagoon vault:{self.vault_contract.address} safe:{self.safe_address}>"

    def has_block_range_event_support(self):
        return True

    def has_deposit_distribution_to_all_positions(self):
        return False

    def get_flow_manager(self) -> "LagoonFlowManager":
        return LagoonFlowManager(self)

    def fetch_safe(self, address) -> Safe:
        """Use :py:meth:`safe` property for cached access"""
        client = create_safe_ethereum_client(self.web3)
        return Safe(
            address,
            client,
        )

    @property
    def chain_id(self) -> int:
        return self.spec.chain_id

    @cached_property
    def vault_address(self) -> HexAddress:
        return Web3.to_checksum_address(self.spec.vault_address)

    @property
    def name(self) -> str:
        return self.share_token.name

    @property
    def symbol(self) -> str:
        return self.share_token.symbol

    @cached_property
    def vault_contract(self) -> Contract:
        """Underlying Vault smart contract."""
        return get_deployed_contract(
            self.web3,
            "lagoon/Vault.json",
            self.spec.vault_address,
        )

    @cached_property
    def trading_strategy_module(self) -> Contract:
        assert self.trading_strategy_module_address, "TradingStrategyModuleV0 address must be separately given in the configuration"
        return get_deployed_contract(
            self.web3,
            "safe-integration/TradingStrategyModuleV0.json",
            self.trading_strategy_module_address,
        )

    def fetch_vault_info(self) -> dict:
        """Get all information we can extract from the vault smart contracts."""
        vault = self.vault_contract
        roles_tuple = vault.functions.getRolesStorage().call()
        whitelistManager, feeReceiver, safe, feeRegistry, valuationManager = roles_tuple
        asset = vault.functions.asset().call()
        return {
            "address": vault.address,
            "whitelistManager": whitelistManager,
            "feeReceiver": feeReceiver,
            "feeRegistry": feeRegistry,
            "valuationManager": valuationManager,
            "safe": safe,
            "asset": asset,
            "tradingStrategyModuleAddress": self.trading_strategy_module_address,
        }

    def fetch_denomination_token(self) -> TokenDetails:
        token_address = self.info["asset"]
        return fetch_erc20_details(self.web3, token_address, chain_id=self.spec.chain_id)

    def fetch_share_token(self) -> TokenDetails:
        token_address = self.info["address"]
        return fetch_erc20_details(self.web3, token_address, chain_id=self.spec.chain_id)

    def fetch_info(self) -> LagoonVaultInfo:
        """Use :py:meth:`info` property for cached access.

        :return:
            See :py:class:`LagoonVaultInfo`
        """
        vault_info = self.fetch_vault_info()
        safe = self.fetch_safe(vault_info["safe"])
        safe_info_dict = asdict(safe.retrieve_all_info())
        del safe_info_dict["address"]  # Key conflict
        return vault_info | safe_info_dict

    def fetch_nav(self, block_identifier=None) -> Decimal:
        """Fetch the most recent onchain NAV value.

        - In the case of Lagoon, this is the last value written in the contract with
          `updateNewTotalAssets()` and ` settleDeposit()`

        - TODO: `updateNewTotalAssets()` there is no way to read pending asset update on chain

        :return:
            Vault NAV, denominated in :py:meth:`denomination_token`
        """
        token = self.denomination_token
        raw_amount = self.vault_contract.functions.totalAssets().call(block_identifier=block_identifier)
        return token.convert_to_decimals(raw_amount)

    def fetch_total_assets(self, block_identifier: BlockIdentifier) -> Decimal:
        """What is the total NAV of the vault.

        :return:
            The vault value in underlyinh token
        """
        raw_amount = self.vault_contract.functions.totalAssets().call(block_identifier=block_identifier)
        return self.underlying_token.convert_to_decimals(raw_amount)

    def fetch_total_supply(self, block_identifier: BlockIdentifier) -> Decimal:
        """What is the current outstanding shares.

        :return:
            The vault value in underlyinh token
        """
        raw_amount = self.vault_contract.functions.totalSupply().call(block_identifier=block_identifier)
        return self.share_token.convert_to_decimals(raw_amount)

    def fetch_share_price(self, block_identifier: BlockIdentifier) -> Decimal:
        """Get the current share price.

        :return:
            The share price in underlying token.

            If supply is zero return zero.
        """

        #     function _convertToAssets(
        #         uint256 shares,
        #         uint40 requestId,
        #         Math.Rounding rounding
        #     ) internal view returns (uint256) {
        #         ERC7540Storage storage $ = _getERC7540Storage();
        #
        #         // cache
        #         uint40 settleId = $.epochs[requestId].settleId;
        #
        #         uint256 _totalAssets = $.settles[settleId].totalAssets + 1;
        #         uint256 _totalSupply = $.settles[settleId].totalSupply + 10 ** _decimalsOffset();
        #
        #         return shares.mulDiv(_totalAssets, _totalSupply, rounding);
        #     }
        total_assets = self.fetch_total_assets(block_identifier)
        total_supply = self.fetch_total_supply(block_identifier)
        if total_supply == 0:
            return Decimal(0)
        return total_assets / self.fetch_total_supply(block_identifier)

    @property
    def address(self) -> HexAddress:
        """Get the vault smart contract address."""
        return self.spec.vault_address

    @property
    def safe_address(self) -> HexAddress:
        """Get Safe multisig contract address"""
        return self.info["safe"]

    @cached_property
    def safe(self) -> Safe:
        """Get the underlying Safe object used as an API from safe-eth-py library.

        - Warps Safe Contract using Gnosis's in-house library
        """
        return self.fetch_safe(self.info["safe"])

    @cached_property
    def safe_contract(self) -> Contract:
        """Safe multisig as a contract.

        - Interact with Safe multisig ABI
        """
        return self.safe.contract

    @property
    def valuation_manager(self) -> HexAddress:
        """Valuation manager role on the vault."""
        return self.info["valuationManager"]

    @cached_property
    def silo_address(self) -> HexAddress:
        """Pending Silo contract address"""
        vault_contract = self.vault_contract
        silo_address = vault_contract.functions.pendingSilo().call()
        return silo_address

    @cached_property
    def silo_contract(self) -> Contract:
        """Pending Silo contract.

        - This contract does not have any functionality, but stores deposits (pending USDC) and redemptions (pending share token)
        """
        return get_deployed_contract(self.web3, "lagoon/Silo.json", self.silo_address)

    @property
    def underlying_token(self) -> TokenDetails:
        """Alias for :py:meth:`denomination_token`"""
        return self.denomination_token

    def fetch_portfolio(
        self,
        universe: TradingUniverse,
        block_identifier: BlockIdentifier | None = None,
    ) -> VaultPortfolio:
        erc20_balances = fetch_erc20_balances_fallback(
            self.web3,
            self.safe_address,
            universe.spot_token_addresses,
            block_identifier=block_identifier,
            decimalise=True,
        )
        return VaultPortfolio(
            spot_erc20=erc20_balances,
        )

    def transact_via_exec_module(
        self,
        func_call: ContractFunction,
        value: int = 0,
        operation=0,
    ) -> ContractFunction:
        """Create a multisig transaction using a module.

        - Calls `execTransactionFromModule` on Gnosis Safe contract

        - Executes a transaction as a multisig

        - Mostly used for testing w/whitelist ignore

        .. warning ::

            A special gas fix is needed, because `eth_estimateGas` seems to fail for these Gnosis Safe transactions.

        Example:

        .. code-block:: python

            # Then settle the valuation as the vault owner (Safe multisig) in this case
            settle_call = vault.settle()
            moduled_tx = vault.transact_through_module(settle_call)
            tx_data = moduled_tx.build_transaction({
                "from": asset_manager,
            })
            # Normal estimate_gas does not give enough gas for
            # Safe execTransactionFromModule() transaction for some reason
            gnosis_gas_fix = 1_000_000
            tx_data["gas"] = web3.eth.estimate_gas(tx_data) + gnosis_gas_fix
            tx_hash = web3.eth.send_transaction(tx_data)
            assert_execute_module_success(web3, tx_hash)

        :param func_call:
            Bound smart contract function call

        :param value:
            ETH attached to the transaction

        :param operation:
            Gnosis enum.

            .. code-block:: text
                library Enum {
                    enum Operation {
                        Call,
                        DelegateCall
                    }
                }
        """
        contract_address = func_call.address
        data_payload = encode_function_call(func_call, func_call.arguments)
        contract = self.safe_contract
        bound_func = contract.functions.execTransactionFromModule(
            contract_address,
            value,
            data_payload,
            operation,
        )
        return bound_func

    def transact_via_trading_strategy_module(
        self,
        func_call: ContractFunction,
    ) -> ContractFunction:
        """Create a Safe multisig transaction using TradingStrategyModuleV0.

        :param module:
            Deployed TradingStrategyModuleV0 contract that is enabled on Safe.

        :param func_call:
            Bound smart contract function call

        :return:
            Bound Solidity functionc all you need to turn to a transaction

        """
        contract_address = func_call.address
        data_payload = encode_function_call(func_call, func_call.arguments)
        logger.info(
            "Lagoon: Wrapping call to TradingStrategyModuleV0. Target: %s, function: %s (0x%s), args: %s, payload is %d bytes",
            contract_address,
            func_call.fn_name,
            get_function_selector(func_call).hex(),
            present_solidity_args(func_call.arguments),
            len(data_payload),
        )
        bound_func = self.trading_strategy_module.functions.performCall(
            contract_address,
            data_payload,
        )
        return bound_func

    def post_new_valuation(
        self,
        total_valuation: Decimal,
    ) -> ContractFunction:
        """Update the valuations of this vault.

        - Lagoon vault does not currently track individual positions, but takes a "total value" number

        - Updating this number also allows deposits and redemptions to proceed

        Notes:

            How can I post a valuation commitee update 1. as the valuationManager, call the function updateNewTotalAssets(_newTotalAssets) _newTotalAssets being expressed in underlying in its smallest unit for usdc, it would  with its 6 decimals. Do not take into account requestDeposit and requestRedeem in your valuation

            2. as the safe, call the function settleDeposit()

        :param total_valuation:
            The vault value nominated in :py:meth:`denomination_token`.

        :return:
            Bound contract function that can be turned to a transaction
        """
        logger.info("Updating vault %s valuation to %s %s", self.address, total_valuation, self.denomination_token.symbol)
        raw_amount = self.denomination_token.convert_to_raw(total_valuation)
        bound_func = self.vault_contract.functions.updateNewTotalAssets(raw_amount)
        return bound_func

    def settle_via_trading_strategy_module(self) -> ContractFunction:
        """Settle the new valuation and deposits.

        - settleDeposit will also settle the redeems request if possible. If there are enough assets in the safe it will settleRedeem
          It there are not enough assets, it will only settleDeposit.

        - if there is nothing to settle: no deposit and redeem requests you can still call settleDeposit/settleRedeem to validate the new nav

        - If there is not enough USDC to redeem, the transaction will revert
        """
        assert self.trading_strategy_module_address, "TradingStrategyModuleV0 not configured"
        block = self.web3.eth.block_number
        pending = self.get_flow_manager().fetch_pending_deposit(block)
        logger.info("Settling deposits for the block %d, we have %s %s deposits pending", block, pending, self.underlying_token.symbol)
        bound_func = self.vault_contract.functions.settleDeposit()
        return self.transact_via_trading_strategy_module(bound_func)

    def post_valuation_and_settle(
        self,
        valuation: Decimal,
        asset_manager: HexAddress,
        gas=1_000_000,
    ) -> HexBytes:
        """Do both new valuation and settle.

        - Quickhand method for asset_manager code

        - Only after this we can read back

        - Broadcasts two transactions and waits for the confirmation

        - If there is not enough USDC to redeem, the second transaction will fail with revert

        :return:
            The transaction hash of the settlement transaction
        """

        assert isinstance(valuation, Decimal)

        bound_func = self.post_new_valuation(valuation)
        tx_hash = bound_func.transact({"from": asset_manager, "gas": gas})
        assert_transaction_success_with_explanation(self.web3, tx_hash)

        bound_func = self.settle_via_trading_strategy_module()
        tx_hash = bound_func.transact({"from": asset_manager, "gas": gas})
        assert_transaction_success_with_explanation(self.web3, tx_hash)

        return tx_hash

    def request_deposit(self, depositor: HexAddress, raw_amount: int) -> ContractFunction:
        """Build a deposit transction.

        - Phase 1 of deposit before settlement
        - Used for testing
        - Must be approved() first
        - Uses the vault underlying token (USDC)

        :param raw_amount:
            Raw amount in underlying token
        """
        underlying = self.underlying_token
        existing_balance = underlying.fetch_raw_balance_of(depositor)
        assert existing_balance >= raw_amount, f"Cannot deposit {underlying.symbol} by {depositor}. Have: {existing_balance}, asked to deposit: {amount}"
        existing_allowance = underlying.contract.functions.allowance(depositor, self.vault_address).call()
        assert existing_allowance >= raw_amount, f"Cannot deposit {underlying.symbol} by {depositor}. Allowance: {existing_allowance}, asked to deposit: {amount}"
        return self.vault_contract.functions.requestDeposit(
            raw_amount,
            depositor,
            depositor,
        )

    def finalise_deposit(self, depositor: HexAddress, raw_amount: int | None = None) -> ContractFunction:
        """Move shares we received to the user wallet.

        - Phase 2 of deposit after settlement
        """

        if raw_amount is None:
            raw_amount = self.vault_contract.functions.maxDeposit(depositor).call()

        return self.vault_contract.functions.deposit(raw_amount, depositor)

    def request_redeem(self, depositor: HexAddress, raw_amount: int) -> ContractFunction:
        """Build a redeem transction.

        - Phase 1 of redemption, before settlement
        - Used for testing
        - Sets up a redemption request for X shares

        :param raw_amount:
            Raw amount in share token
        """
        assert type(raw_amount) == int, f"Got {raw_amount} {type(raw_amount)}"
        shares = self.share_token
        block_number = self.web3.eth.block_number

        # Check we have shares
        owned_raw_amount = shares.fetch_raw_balance_of(depositor, block_number)
        assert owned_raw_amount >= raw_amount, f"Cannot redeem, has only {owned_raw_amount} shares when {raw_amount} needed"

        human_amount = shares.convert_to_decimals(raw_amount)
        total_shares = self.fetch_total_supply(block_number)
        logger.info("Setting up redemption for %s %s shares out of %s, for %s", human_amount, shares.symbol, total_shares, depositor)
        return self.vault_contract.functions.requestRedeem(
            raw_amount,
            depositor,
            depositor,
        )

    def finalise_redeem(self, depositor: HexAddress, raw_amount: int | None = None) -> ContractFunction:
        """Move redeemed assets to the user wallet.

        - Phase 2 of the redemption
        """

        assert type(depositor) == str, f"Got {depositor} {type(depositor)}"

        if raw_amount is None:
            raw_amount = self.vault_contract.functions.maxRedeem(depositor).call()

        return self.vault_contract.functions.redeem(raw_amount, depositor, depositor)


class LagoonFlowManager(VaultFlowManager):
    """Manage deposit/redemption queue for Lagoon.

    - Lagoon uses `ERC-7540 <https://eips.ethereum.org/EIPS/eip-7540>`__ Asynchronous ERC-4626 Tokenized Vaults for
      deposits and redemptions flow

    On the Lagoon flow:

        Ok so for settlement you dont have to worry about this metric, the only thing you have to value is the assets inside the safe (what you currently have under management) and update the NAV of the vault by calling updateNewTotalAssets (ex: if you have 1M inside the vault and 500K pending deposit you only need to call updateTotalAssets with the 1M that are currently inside the safe). Then, to settle you just call settleDeposit and the vault calculate everything for you.

        To monitor the pending deposits it's a bit more complicated. You have to check the balanceOf the pendingSilo contract (0xAD1241Ba37ab07fFc5d38e006747F8b92BB217D5) in term of underlying (here USDC) for pending deposit and in term of shares (so the vault itself) for pending withdraw requests

    """

    def __init__(self, vault: LagoonVault) -> None:
        self.vault = vault

    def fetch_pending_redemption(self, block_identifier: BlockIdentifier) -> Decimal:
        silo = self.vault.silo_contract
        return self.vault.share_token.fetch_balance_of(silo.address, block_identifier)

    def fetch_pending_deposit(self, block_identifier: BlockIdentifier) -> Decimal:
        silo = self.vault.silo_contract
        return self.vault.underlying_token.fetch_balance_of(silo.address, block_identifier)

    def fetch_pending_deposit_events(self, range: BlockRange) -> None:
        raise NotImplementedError()

    def fetch_pending_redemption_event(self, range: BlockRange) -> None:
        raise NotImplementedError()

    def fetch_processed_deposit_event(self, range: BlockRange) -> None:
        pass

    def fetch_processed_redemption_event(self, vault: VaultSpec, range: BlockRange) -> None:
        raise NotImplementedError()

    def calculate_underlying_neeeded_for_redemptions(self, block_identifier: BlockIdentifier) -> Decimal:
        """How much underlying token (USDC) we are going to need on the next redemption cycle.

        :return:
            Raw token amount
        """
        # How many shares we have pending for the redemption
        shares_pending = self.fetch_pending_redemption(block_identifier)
        share_price = self.vault.fetch_share_price(block_identifier)
        return shares_pending * share_price
