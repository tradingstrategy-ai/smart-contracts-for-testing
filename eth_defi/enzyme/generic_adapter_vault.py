"""Safe deployment of Enzyme vaults with generic adapter. """
import logging
from pathlib import Path
from typing import Collection

from eth_account.signers.local import LocalAccount
from eth_typing import HexAddress
from web3.contract import Contract

from eth_defi.deploy import deploy_contract
from eth_defi.enzyme.deployment import EnzymeDeployment
from eth_defi.enzyme.policy import create_safe_default_policy_configuration_for_generic_adapter
from eth_defi.enzyme.vault import Vault
from eth_defi.foundry.forge import deploy_contract_with_forge
from eth_defi.token import TokenDetails, fetch_erc20_details
from eth_defi.trace import assert_transaction_success_with_explanation
from eth_defi.uniswap_v2.utils import ZERO_ADDRESS

logger = logging.getLogger(__name__)


CONTRACTS_ROOT = Path(os.path.dirname(__file__)) / ".." / ".." / "contracts"


def deploy_vault_with_generic_adapter(
    deployment: EnzymeDeployment,
    deployer: LocalAccount,
    asset_manager: HexAddress | str,
    owner: HexAddress | str,
    usdc: Contract,
    terms_of_service: Contract,
    fund_name="Example Fund",
    fund_symbol="EXAMPLE",
    whitelisted_assets: Collection[TokenDetails] | None = None,
) -> Vault:
    """Deploy an Enzyme vault and make it secure.

    Deploys an Enzyme vault in a specific way we want to have it deployed.

    - Because we want multiple deployed smart contracts to be verified on Etherscan,
      this deployed uses a Forge-based toolchain and thus the script
      can be only run from the git checkout where submodules are included.

    - Set up default policies

    - Assign a generic adapter

    - Assign a USDC payment forwarder with terms of service sign up

    - Assign asset manager role and transfer ownership

    - Whitelist USDC and the other given assets

    - Whitelist Uniswap v2 and v3 spot routers

    .. note ::

        The GuardV0 ownership is **not** transferred to the owner at the end of the deployment.
        You need to do it manually after configuring the guard.

    :param deployment:
        Enzyme deployment we use.

    :param deployer:
        Web3.py deployer account we use.

    :param asset_manager:
        Give trading access to this hot wallet address.

        Set to the deployer address to ignore.

    :param terms_of_service:
        Terms of service contract we use.

    :param owner:
        Nominated new owner.

        Immediately transfer vault ownership from a deployer to a multisig owner.
        Multisig needs to confirm this by calling `claimOwnership`.

        Set to the deployer address to ignore.

    :param whitelisted_assets:
        Whitelist these assets on Uniswap v2 and v3 spot market.

        USDC is always whitelisted.

    :param usdc:
        USDC token used as the vault denomination currency.

    :return:
        Freshly deployed vault
    """

    assert isinstance(deployer, LocalAccount)
    assert asset_manager.startswith("0x")
    assert owner.startswith("0x")

    assert CONTRACTS_ROOT.exists(), f"Cannot find contracts folder {CONTRACTS_ROOT.resolve()} - are you runnign from git checkout?"

    whitelisted_assets = whitelisted_assets or []
    for asset in whitelisted_assets:
        assert isinstance(asset, TokenDetails)

    logger.info(
        "Deploying Enzyme vault. Enzyme fund deployer is %s, Terms of service is %s, USDC is %s",
        deployment.contracts.fund_deployer.address,
        terms_of_service.address,
        usdc.address,
    )

    web3 = deployment.web3

    guard = deploy_contract_with_forge(
        web3,
        f"guard/GuardV0.json",
        deployer,
    )
    assert guard.functions.getInternalVersion().call() == 1

    generic_adapter = deploy_contract(
        web3,
        f"GuardedGenericAdapter.json",
        deployer,
        deployment.contracts.integration_manager.address,
        guard.address,
    )

    policy_configuration = create_safe_default_policy_configuration_for_generic_adapter(
        deployment,
        generic_adapter,
    )

    comptroller, vault = deployment.create_new_vault(
        deployer,
        usdc,
        policy_configuration=policy_configuration,
        fund_name=fund_name,
        fund_symbol=fund_symbol,
    )

    assert comptroller.functions.getDenominationAsset().call() == usdc.address
    assert vault.functions.getTrackedAssets().call() == [usdc.address]

    # asset manager role is the trade executor
    vault.functions.addAssetManagers([asset_manager]).transact({"from": deployer})

    payment_forwarder = deploy_contract_with_forge(
        web3,
        "TermedVaultUSDCPaymentForwarder.json",
        deployer,
        [usdc.address, comptroller.address, terms_of_service.address],
    )

    # When swap is performed, the tokens will land on the integration contract
    # and this contract must be listed as the receiver.
    # Enzyme will then internally move tokens to its vault from here.
    guard.functions.allowReceiver(generic_adapter.address, "").transact({"from": deployer})

    # Because Enzyme does not pass the asset manager address to through integration manager,
    # we set the vault address itself as asset manager for the guard
    tx_hash = guard.functions.allowSender(vault.address, "").transact({"from": deployer})
    assert_transaction_success_with_explanation(web3, tx_hash)

    # Give generic adapter back reference to the vault
    assert vault.functions.getCreator().call() != ZERO_ADDRESS, f"Bad vault creator {vault.functions.getCreator().call()}"
    tx_hash = generic_adapter.functions.bindVault(vault.address).transact({"from": deployer})
    assert_transaction_success_with_explanation(web3, tx_hash)

    receipt = web3.eth.get_transaction_receipt(tx_hash)
    deployed_at_block = receipt["blockNumber"]

    assert generic_adapter.functions.getIntegrationManager().call() == deployment.contracts.integration_manager.address
    assert comptroller.functions.getDenominationAsset().call() == usdc.address
    assert vault.functions.getTrackedAssets().call() == [usdc.address]
    assert vault.functions.canManageAssets(asset_manager).call()
    assert guard.functions.isAllowedSender(vault.address).call()  # vault = asset manager for the guard

    usdc_token = fetch_erc20_details(web3, usdc.address)
    all_assets = [usdc_token] + whitelisted_assets
    for asset in all_assets:
        logger.info("Whitelisting %s", asset)
        tx_hash = guard.functions.whitelistToken(asset.address, f"Whitelisting {asset.symbol}").transact({"from": deployer})
        assert_transaction_success_with_explanation(web3, tx_hash)

    # We cannot directly transfer the ownership to a multisig,
    # but we can set nominated ownership pending
    if owner != deployer:
        tx_hash = vault.functions.setNominatedOwner(owner).transact({"from": deployer})
        assert_transaction_success_with_explanation(web3, tx_hash)
        logger.info("New vault owner nominated to be %s", owner)

    vault = Vault.fetch(
        web3,
        vault_address=vault.address,
        payment_forwarder=payment_forwarder.address,
        generic_adapter_address=generic_adapter.address,
        deployed_at_block=deployed_at_block,
        asset_manager=asset_manager,
    )
    assert vault.guard_contract.address == guard.address

    logger.info(
        "Deployed. Vault is %s, initial owner is %s, asset manager is %s",
        vault.vault.address,
        vault.get_owner(),
        asset_manager,
    )

    return vault
