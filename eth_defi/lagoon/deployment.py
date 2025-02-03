"""Deploy new Lagoon vaults.

Lagoon automatised vault consists of

- Safe multisig - we use 1.4.1 here
- Vault module
- Lagoon protocol smart contracts
- TradingStrategyModuleV0 module enabling guarded automated trade executor for the Safe
- Support deployments with Forge and Etherscan verification

Any Safe must be deployed as 1-of-1 deployer address multisig and multisig holders changed after the deployment.
"""

import logging
import os
import time
from dataclasses import dataclass, asdict
from io import StringIO
from pathlib import Path
from pprint import pformat
from typing import Callable

from eth_account.signers.local import LocalAccount
from eth_typing import HexAddress, BlockNumber
from hexbytes import HexBytes
from safe_eth.safe.safe import Safe
from safe_eth.safe.api.transaction_service_api.transaction_service_api import TransactionServiceApi

from web3 import Web3
from web3.contract import Contract
from web3.contract.contract import ContractFunction

from eth_defi.abi import get_contract
from eth_defi.deploy import deploy_contract
from eth_defi.foundry.forge import deploy_contract_with_forge
from eth_defi.hotwallet import HotWallet
from eth_defi.lagoon.beacon_proxy import deploy_beacon_proxy
from eth_defi.lagoon.vault import LagoonVault
from eth_defi.middleware import construct_sign_and_send_raw_middleware_anvil
from eth_defi.provider.anvil import is_anvil
from eth_defi.safe.deployment import deploy_safe, add_new_safe_owners
from eth_defi.token import get_wrapped_native_token_address, fetch_erc20_details
from eth_defi.trace import assert_transaction_success_with_explanation
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment
from eth_defi.uniswap_v3.deployment import UniswapV3Deployment
from eth_defi.vault.base import VaultSpec

logger = logging.getLogger(__name__)

#  https://github.com/hopperlabsxyz/lagoon-v0
LAGOON_BEACONS = {
    # Base
    8453: "0xD69BC314bdaa329EB18F36E4897D96A3A48C3eeF",
}


# https://github.com/hopperlabsxyz/lagoon-v0
LAGOON_FEE_REGISTRIES = {
    # Base
    8453: "0x6dA4D1859bA1d02D095D2246142CdAd52233e27C",
}

DEFAULT_RATE_UPDATE_COOLDOWN = 86400

DEFAULT_MANAGEMENT_RATE = 200

DEFAULT_PERFORMANCE_RATE = 2000

CONTRACTS_ROOT = Path(os.path.dirname(__file__)) / ".." / ".." / "contracts"


@dataclass(slots=True)
class LagoonDeploymentParameters:
    """Capture core parameters needed to deploy a Lagoon vault"""

    underlying: HexAddress
    name: str
    symbol: str
    safe: str | None = None
    whitelistManager: str | None = None
    valuationManager: str | None = None
    admin: str = None
    feeReceiver: str = None
    feeRegistry: str = None

    #: Management fee in BPS
    managementRate: int = DEFAULT_MANAGEMENT_RATE  # Assuming these are integers, adjust type if different

    #: Performance fee in BPS
    performanceRate: int = DEFAULT_PERFORMANCE_RATE
    enableWhitelist: bool = False

    #: Max rate update frequency, seconds
    rateUpdateCooldown: int = DEFAULT_RATE_UPDATE_COOLDOWN

    #: If set None, then autoresolve
    wrappedNativeToken: HexAddress | None = None

    def as_solidity_struct(self) -> dict:
        # Return Vault.InitStruct to be passed to the constructor
        return asdict(self)


@dataclass(slots=True, frozen=True)
class LagoonAutomatedDeployment:
    """Capture information of the lagoon automated deployment.

    - Have the deployment report for the users for diagnostics
    """

    chain_id: int
    vault: LagoonVault
    trading_strategy_module: Contract
    asset_manager: HexAddress
    multisig_owners: list[HexAddress]
    deployer: HexAddress
    block_number: BlockNumber

    def is_asset_manager(self, address: HexAddress) -> bool:
        return self.trading_strategy_module.functions.isAllowedSender(address).call()

    def get_deployment_data(self) -> dict:
        """Get JSON data describing the deployment.

        Store all addresses etc.
        """
        vault = self.vault
        safe = vault.safe
        fields = {
            "Deployer": self.deployer,
            "Safe": safe.address,
            "Vault": vault.address,
            "Trading strategy module": self.trading_strategy_module.address,
            "Asset manager": self.asset_manager,
            "Underlying token": self.vault.underlying_token.address,
            "Underlying symbol": self.vault.underlying_token.symbol,
            "Share token": self.vault.share_token.address,
            "Share token symbol": self.vault.share_token.symbol,
            "Multisig owners": ", ".join(self.multisig_owners),
            "Block number": f"{self.block_number:,}",
        }
        return fields

    def pformat(self) -> str:
        """Return pretty print of deployment info."""
        fields = self.get_deployment_data()
        # https://stackoverflow.com/a/17330263/315168
        io = StringIO()
        print("{:<30} {:30}".format("Key", "Label"), file=io)
        for k, v in fields.items():
            print("{:<30} {:<30}".format(k, v), file=io)

        return io.getvalue()


def deploy_lagoon(
    web3: Web3,
    deployer: LocalAccount,
    safe: Safe,
    asset_manager: HexAddress,
    parameters: LagoonDeploymentParameters,
    owner: HexAddress | None,
    gas=2_000_000,
    etherscan_api_key: str = None,
    use_forge=False,
    beacon_proxy=True,
    beacon_address="0x652716FaD571f04D26a3c8fFd9E593F17123Ab20",
) -> Contract:
    """Deploy a new Lagoon vault.

    - Create a new Safe

    - Create a new Lagoon vault

    - Set guard policies

    - Set owership

    For Foundry recipe see https://github.com/hopperlabsxyz/lagoon-v0/blob/main/script/deploy_vault.s.sol

    :param deployer:
        The initial account used to deploy smart contracts

    :param owner:
        All transfership is transferred to this user after.

        Usually defaults to newly deployed Safe the vault is associated with.

    :param asset_manager:
        Able to perform trades, valuations

    :param beacon_address:
        Vault beacon on base.

    :param use_forge:
        Deploy a new vault contract from source with Forge and Etherscan verification.

        TODO: Not implemented, contract not yet open source.

    :param etherscan_api_key:
        For Forge.

    :return:
        Vault contract.

        Is a proxy contract.
    """

    assert isinstance(safe, Safe)
    assert isinstance(deployer, LocalAccount)

    chain_id = web3.eth.chain_id

    logger.info(
        "Deploying Lagoon vault on chain %d, deployer is %s",
        chain_id,
        deployer,
    )

    if owner is None:
        owner = safe.address

    # Autoresolve some parameters based on our deployment structure

    if parameters.wrappedNativeToken is None:
        parameters.wrappedNativeToken = get_wrapped_native_token_address(chain_id)

    if parameters.feeReceiver is None:
        parameters.feeReceiver = owner

    if parameters.whitelistManager is None:
        parameters.whitelistManager = owner

    if parameters.valuationManager is None:
        parameters.valuationManager = asset_manager

    if parameters.feeRegistry is None:
        parameters.feeRegistry = LAGOON_FEE_REGISTRIES[chain_id]

    if parameters.admin is None:
        parameters.admin = owner

    init_struct = parameters.as_solidity_struct()

    logger.info("Parameters are:\n%s", pformat(init_struct))

    # TODO: Beacon proxy deployment does not work

    if use_forge:
        logger.warning("lagoon/Vault.sol yet not open source - cannot do source verified deploy")

    if beacon_proxy:
        vault = deploy_beacon_proxy(
            web3,
            deployer=deployer,
            beacon_address=beacon_address,
            implementation_contract_abi="lagoon/Vault.json",
        )

    else:
        vault = deploy_contract(
            web3,
            "lagoon/Vault.json",
            deployer,
            False,
        )

    tx_params = vault.functions.initialize(init_struct).build_transaction(
        {
            "gas": 2_000_000,
            "chainId": chain_id,
            "nonce": web3.eth.get_transaction_count(deployer.address),
        }
    )
    signed_tx = deployer.sign_transaction(tx_params)
    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    assert_transaction_success_with_explanation(web3, tx_hash)

    return vault

    # VaultContract = get_contract(
    #     web3,
    #     "lagoon/Vault.json",
    # )
    #

    # payable(Upgrades.deployBeaconProxy(beacon, abi.encodeWithSelector(Vault.initialize.selector, init)))
    # E           Could not identify the intended function with name `initialize`, positional arguments with type(s) `address,str,str,address,address,address,address,address,address,int,int,bool,int,address` and keyword arguments with type(s) `{}`.
    #
    # abi_packed_init_args = encode_function_call(
    #     VaultContract.functions.initialize,
    #     [init_struct],  # Solidity struct encoding is a headache
    # )
    #
    # tx_hash = deploy_contract(
    #     web3,
    #     "lagoon/BeaconProxy.json",
    #     deployer,
    #     owner,
    #     abi_packed_init_args,
    #     gas=gas,
    #     confirm=False,
    # )
    # tx_receipt = assert_transaction_success_with_explanation(web3, tx_hash)
    #
    # beacon_address = tx_receipt["contractAddress"]
    # vault_proxy = get_deployed_contract(
    #     web3,
    #     "lagoon/Vault.json",
    #     beacon_address,
    # )
    # return vault_proxy


def deploy_safe_trading_strategy_module(
    web3,
    deployer: LocalAccount,
    safe: Safe,
    use_forge=False,
    etherscan_api_key: str = None,
) -> Contract:
    """Deploy TradingStrategyModuleV0 for Safe and Lagoon.

    :param use_forge:
        Deploy Etherscan verified build with Forge

    :return:
        TradingStrategyModuleV0 instance
    """

    logger.info("Deploying TradingStrategyModuleV0")

    owner = deployer.address

    # Deploy guard module
    if use_forge:
        module, tx_hash = deploy_contract_with_forge(
            web3,
            CONTRACTS_ROOT / "safe-integration",
            "TradingStrategyModuleV0.sol",
            "TradingStrategyModuleV0",
            deployer,
            [owner, safe.address],
            etherscan_api_key=etherscan_api_key,
        )
    else:
        module = deploy_contract(
            web3,
            "safe-integration/TradingStrategyModuleV0.json",
            deployer,
            owner,
            safe.address,
        )

    # Enable TradingStrategyModuleV0 as Safe module
    # Multisig owners can enable the module
    tx = safe.contract.functions.enableModule(module.address).build_transaction({"from": deployer.address, "gas": 0, "gasPrice": 0})
    safe_tx = safe.build_multisig_tx(safe.address, 0, tx["data"])
    safe_tx.sign(deployer._private_key.hex())
    tx_hash, tx = safe_tx.execute(
        tx_sender_private_key=deployer._private_key.hex(),
    )
    assert_transaction_success_with_explanation(web3, tx_hash)

    return module


def setup_guard(
    web3: Web3,
    safe: Safe,
    deployer: HotWallet,
    owner: HexAddress,
    asset_manager: HexAddress,
    vault: Contract,
    module: Contract,
    broadcast_func: Callable[[ContractFunction], HexBytes],
    any_asset: bool = False,
    uniswap_v2: UniswapV2Deployment = None,
    uniswap_v3: UniswapV3Deployment = None,
):

    assert isinstance(deployer, HotWallet), f"Got: {deployer}"
    assert type(owner) == str
    assert isinstance(module, Contract)
    assert isinstance(vault, Contract)
    assert any_asset, f"Only wildcard trading supported at the moment"
    assert callable(broadcast_func), "Must have a broadcast function for txs"

    _broadcast = broadcast_func

    logger.info("Setting up TradingStrategyModuleV0 guard")

    # Enable asset_manager as the whitelisted trade-executor
    logger.info("Whitelisting trade-executor as sender")
    tx_hash = _broadcast(module.functions.allowSender(asset_manager, "Whitelist trade-executor"))
    assert_transaction_success_with_explanation(web3, tx_hash)

    # Enable safe as the receiver of tokens
    logger.info("Whitelist Safe as trade receiver")
    tx_hash = _broadcast(module.functions.allowReceiver(safe.address, "Whitelist Safe as trade receiver"))
    assert_transaction_success_with_explanation(web3, tx_hash)

    # Whitelist Uniswap v2
    if uniswap_v2:
        logger.info("Whitelisting Uniswap v2 router: %s", uniswap_v2.router.address)
        tx_hash = _broadcast(module.functions.whitelistUniswapV2Router(uniswap_v2.router.address, "Allow Uniswap v2"))
        assert_transaction_success_with_explanation(web3, tx_hash)

    # Whitelist Uniswap v3
    if uniswap_v3:
        logger.info("Whitelisting Uniswap v3 router: %s", uniswap_v3.swap_router.address)
        tx_hash = _broadcast(module.functions.whitelistUniswapV3Router(uniswap_v3.swap_router.address, "Allow Uniswap v3"))
        assert_transaction_success_with_explanation(web3, tx_hash)

    # Whitelist all assets
    if any_asset:
        logger.info("Allow any asset whitelist")
        tx_hash = _broadcast(module.functions.setAnyAssetAllowed(True, "Allow any asset"))
        assert_transaction_success_with_explanation(web3, tx_hash)
    else:
        logger.info("Using only whitelisted assets")

    # Whitelist vault settle
    logger.info("Whitelist vault settlement")
    tx_hash = _broadcast(module.functions.whitelistLagoon(vault.address, "Whitelist vault settlement"))
    assert_transaction_success_with_explanation(web3, tx_hash)


def deploy_automated_lagoon_vault(
    web3: Web3,
    deployer: LocalAccount | HotWallet,
    asset_manager: HexAddress,
    parameters: LagoonDeploymentParameters,
    safe_owners: list[HexAddress | str],
    safe_threshold: int,
    uniswap_v2: UniswapV2Deployment | None,
    uniswap_v3: UniswapV3Deployment | None,
    any_asset: bool = False,
    etherscan_api_key: str = None,
    use_forge=False,
    between_contracts_delay_seconds=10.0,
) -> LagoonAutomatedDeployment:
    """Deploy a full Lagoon setup with a guard.

    Lagoon automatised vault consists of

    - Safe multisig - we use 1.4.1 here
    - Vault module
    - Lagoon protocol smart contracts
    - TradingStrategyModuleV0 module enabling guarded automated trade executor for the Safe

    For roles
    - Asset manager (Trading Straegy) and Valuation Manager (Lagoon) are the same role
    - Any Safe must be deployed as 1-of-1 deployer address multisig and multisig holders changed after the deployment.

    .. warning::

        Because we need to mix Forge, Safe lib and Web3.py transaction nonce management becomes a madness.

    .. note ::

        Deployer account must be manually removed from the Safe by new owners.
    """

    assert len(safe_owners) >= 1, "Multisig owners empty"

    chain_id = web3.eth.chain_id

    if isinstance(deployer, HotWallet):
        # Production nonce hack
        deployer_local_account = deployer.account
    else:
        deployer_local_account = deployer

    # Hack together a nonce management helper
    def _broadcast(bound_func: ContractFunction):
        assert isinstance(bound_func, ContractFunction)
        assert bound_func.args is not None
        if isinstance(deployer, HotWallet):
            # Path must be taken with prod deployers
            return deployer.transact_and_broadcast_with_contract(bound_func)
        elif isinstance(deployer, LocalAccount):
            # Only for Anvil
            # Will cause nonce sync errors in proc
            return bound_func.transact({"from": deployer.address})
        else:
            raise NotImplementedError(f"No idea about: {deployer}")

    safe = deploy_safe(
        web3,
        deployer_local_account,
        owners=[deployer.address],
        threshold=1,
    )

    parameters.safe = safe.address

    if not is_anvil(web3):
        logger.info("Between contracts deployment delay: Sleeping %s for new nonce to propagade", between_contracts_delay_seconds)
        time.sleep(between_contracts_delay_seconds)

    vault_contract = deploy_lagoon(
        web3=web3,
        deployer=deployer_local_account,
        safe=safe,
        asset_manager=asset_manager,
        parameters=parameters,
        owner=safe.address,
        etherscan_api_key=etherscan_api_key,
        use_forge=use_forge,
    )

    if not is_anvil(web3):
        logger.info("Between contracts deployment delay: Sleeping %s for new nonce to propagade", between_contracts_delay_seconds)
        time.sleep(between_contracts_delay_seconds)

    module = deploy_safe_trading_strategy_module(
        web3=web3,
        deployer=deployer_local_account,
        safe=safe,
        etherscan_api_key=etherscan_api_key,
        use_forge=use_forge,
    )

    if not is_anvil(web3):
        logger.info("Between contracts deployment delay: Sleeping %s for new nonce to propagade", between_contracts_delay_seconds)
        time.sleep(between_contracts_delay_seconds)

    if isinstance(deployer, HotWallet):
        deployer.sync_nonce(web3)

    setup_guard(
        web3=web3,
        safe=safe,
        vault=vault_contract,
        deployer=deployer,
        owner=safe.address,
        asset_manager=asset_manager,
        module=module,
        uniswap_v2=uniswap_v2,
        uniswap_v3=uniswap_v3,
        any_asset=any_asset,
        broadcast_func=_broadcast,
    )

    # After everything is deployed, fix ownership
    # 1. Transfer TradingStrategyModuleV0 module ownership to Gnosis
    # 2. Approve redemptions for Safe. USDC must be transferable to Vault (not Silo).
    # 3. Set Gnosis to a true multisig

    # 1. Transfer guard ownership to Gnosis
    assert module.functions.owner().call() == deployer.address
    tx_hash = _broadcast(module.functions.transferOwnership(safe.address))
    assert_transaction_success_with_explanation(web3, tx_hash)

    # 2. USDC.approve() for redemptions on Safe
    underlying = fetch_erc20_details(web3, parameters.underlying, chain_id=chain_id)
    tx_data = underlying.contract.functions.approve(vault_contract.address, 2**256 - 1).build_transaction(
        {
            "from": deployer.address,
            "gas": 0,
            "gasPrice": 0,
        }
    )
    safe_tx = safe.build_multisig_tx(underlying.address, 0, tx_data["data"])
    safe_tx.sign(deployer_local_account._private_key.hex())
    tx_hash, tx = safe_tx.execute(
        tx_sender_private_key=deployer_local_account._private_key.hex(),
    )
    assert_transaction_success_with_explanation(web3, tx_hash)

    # 3. Set Gnosis to a true multisig
    # DOES NOT REMOVE DEPLOYER
    add_new_safe_owners(
        web3,
        safe,
        deployer_local_account,
        owners=safe_owners,
        threshold=safe_threshold,
    )

    vault = LagoonVault(
        web3,
        VaultSpec(chain_id, vault_contract.address),
        trading_strategy_module_address=module.address,
    )

    return LagoonAutomatedDeployment(
        chain_id=chain_id,
        vault=vault,
        trading_strategy_module=module,
        asset_manager=asset_manager,
        multisig_owners=safe_owners,
        block_number=web3.eth.block_number,
        deployer=deployer.address,
    )


def update_lagoon_vault_fees(
    web3: Web3,
    *,
    deployer: LocalAccount | HotWallet,
    vault_spec: VaultSpec,
    management_rate: int,
    performance_rate: int,
) -> None:
    """
    Update the management and performance fees for the Lagoon vault.

    NOTE: this function only proposes a tx to the Safe, the tx must be confirmed by the Safe owners.

    :param deployer:
        The deployer account

    :param vault_spec:
        The vault specification

    :param management_rate:
        The management fee in BPS

    :param performance_rate:
        The performance fee in BPS
    """
    if isinstance(deployer, HotWallet):
        # Production nonce hack
        deployer_local_account = deployer.account
    else:
        deployer_local_account = deployer

    vault = LagoonVault(web3, vault_spec)
    safe = vault.safe

    tx_data = vault.vault_contract.functions.updateRates((management_rate, performance_rate)).build_transaction(
        {
            "from": deployer_local_account.address,
            "gas": 0,
            "gasPrice": 0,
        }
    )

    safe_tx = safe.build_multisig_tx(vault.vault_address, 0, tx_data["data"])
    safe_tx.sign(deployer_local_account._private_key.hex())

    # setup transaction service API and propose the tx to Safe
    api = TransactionServiceApi.from_ethereum_client(safe.ethereum_client)
    api.post_transaction(safe_tx)
