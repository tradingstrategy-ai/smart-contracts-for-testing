"""Velvet capital tests.

- Test against mainnet fork of live deployed vault on Base

- Vault meta https://api.velvet.capital/api/v3/portfolio/0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25

- Vault UI https://dapp.velvet.capital/ManagerVaultDetails/0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25
"""

import os
from decimal import Decimal

import pytest
from eth_typing import HexAddress
from web3 import Web3

from eth_defi.hotwallet import HotWallet
from eth_defi.provider.anvil import AnvilLaunch, fork_network_anvil, make_anvil_custom_rpc_request
from eth_defi.provider.broken_provider import get_almost_latest_block_number
from eth_defi.provider.multi_provider import create_multi_provider_web3
from eth_defi.token import TokenDetails, fetch_erc20_details
from eth_defi.trace import assert_transaction_success_with_explanation
from eth_defi.vault.base import VaultSpec, TradingUniverse
from eth_defi.velvet import VelvetVault

JSON_RPC_BASE = os.environ.get("JSON_RPC_BASE")

CI = os.environ.get("CI", None) is not None

pytestmark = pytest.mark.skipif(not JSON_RPC_BASE, reason="No JSON_RPC_BASE environment variable")


@pytest.fixture()
def vault_owner() -> HexAddress:
    # Vaut owner
    return "0x0c9db006f1c7bfaa0716d70f012ec470587a8d4f"


@pytest.fixture()
def usdc_holder() -> HexAddress:
    # https://basescan.org/token/0x833589fcd6edb6e08f4c7c32d4f71b54bda02913#balances
    return "0x3304E22DDaa22bCdC5fCa2269b418046aE7b566A"



@pytest.fixture()
def anvil_base_fork(request, vault_owner, usdc_holder, deposit_user) -> AnvilLaunch:
    """Create a testable fork of live BNB chain.

    :return: JSON-RPC URL for Web3
    """

    assert JSON_RPC_BASE is not None, "JSON_RPC_BASE not set"
    launch = fork_network_anvil(
        JSON_RPC_BASE,
        unlocked_addresses=[vault_owner, usdc_holder, deposit_user],
    )
    try:
        yield launch
    finally:
        # Wind down Anvil process after the test is complete
        launch.close()


@pytest.fixture()
def web3(anvil_base_fork) -> Web3:
    """Create Web3 instance.

    - Use mainnet fork with Anvil for local testing

    - If symbolic transaction debugging is needed, you can override
      Anvil manually with a Tenderly virtual testnet
    """
    # Debug using Tenderly debugger
    tenderly_fork_rpc = os.environ.get("JSON_RPC_TENDERLY", None)

    if tenderly_fork_rpc:
        web3 = create_multi_provider_web3(tenderly_fork_rpc)
        snapshot = make_anvil_custom_rpc_request(web3, "evm_snapshot")
        try:
            yield web3
        finally:
            # Revert Tenderly testnet back to its original state
            # https://docs.tenderly.co/forks/guides/testing/reset-transactions-after-completing-the-test
            make_anvil_custom_rpc_request(web3, "evm_revert", [snapshot])
    else:
        # Anvil
        web3 = create_multi_provider_web3(
            anvil_base_fork.json_rpc_url,
            default_http_timeout=(3, 45),
        )
        assert web3.eth.chain_id == 8453
        yield web3


@pytest.fixture()
def usdc(web3) -> TokenDetails:
    return fetch_erc20_details(
        web3,
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )


@pytest.fixture()
def hot_wallet_user(web3, usdc, usdc_holder) -> HotWallet:
    """A test account with USDC balance."""

    hw = HotWallet.create_for_testing(
        web3,
        test_account_n=1,
        eth_amount=10
    )
    hw.sync_nonce(web3)

    # give hot wallet some native token
    web3.eth.send_transaction(
        {
            "from": web3.eth.accounts[9],
            "to": hw.address,
            "value": 1 * 10**18,
        }
    )

    # Top up with 999 USDC
    tx_hash = usdc.contract.functions.transfer(hw.address, 999 * 10**6).transact({"from": usdc_holder, "gas": 100_000})
    assert_transaction_success_with_explanation(web3, tx_hash)
    return hw


@pytest.fixture()
def base_test_vault_spec() -> VaultSpec:
    """Vault https://dapp.velvet.capital/ManagerVaultDetails/0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25"""
    return VaultSpec(1, "0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25")


@pytest.fixture()
def vault(web3, base_test_vault_spec: VaultSpec) -> VelvetVault:
    return VelvetVault(web3, base_test_vault_spec)


@pytest.fixture()
def deposit_user() -> HexAddress:
    """A user that has preapproved 5 USDC deposit for the vault above, no approve() needed."""
    return "0x7612A94AafF7a552C373e3124654C1539a4486A8"


def test_fetch_info(vault: VelvetVault):
    """Read vault metadata from private Velvet endpoint."""
    data = vault.fetch_info()
    assert data["owner"] == "0x0c9db006f1c7bfaa0716d70f012ec470587a8d4f"
    assert data["vaultAddress"] == "0x9d247fbc63e4d50b257be939a264d68758b43d04"

    assert vault.vault_address == "0x9d247fbc63e4d50b257be939a264d68758b43d04"
    assert vault.owner_address == "0x0c9db006f1c7bfaa0716d70f012ec470587a8d4f"


def test_fetch_vault_portfolio(vault: VelvetVault):
    """Read vault token balances."""
    web3 = vault.web3
    universe = TradingUniverse(
        spot_token_addresses={
            "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",  # DogInMe
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
        }
    )
    latest_block = get_almost_latest_block_number(web3)
    portfolio = vault.fetch_portfolio(universe, latest_block)
    assert portfolio.spot_erc20["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"] > 0
    assert portfolio.spot_erc20["0x6921B130D297cc43754afba22e5EAc0FBf8Db75b"] > 0


#@pytest.mark.skipif(CI, reason="Enso is such unstable crap that there is no hope we could run any tests with in CI")
def test_vault_swap_partially(
    vault: VelvetVault,
    vault_owner: HexAddress,
):
    """Simulate swap tokens using Enzo.

    - Swap 1 SUDC to DogInMe

    - See balances update in the vault
    """
    web3 = vault.web3
    universe = TradingUniverse(
        spot_token_addresses={
            "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",  # DogInMe
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
        }
    )
    latest_block = get_almost_latest_block_number(web3)
    portfolio = vault.fetch_portfolio(universe, latest_block)

    existing_dogmein_balance = portfolio.spot_erc20["0x6921B130D297cc43754afba22e5EAc0FBf8Db75b"]
    assert existing_dogmein_balance > 0

    existing_usdc_balance = portfolio.spot_erc20["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"]
    assert existing_usdc_balance > Decimal(1.0)

    # Build tx using Velvet API
    tx_data = vault.prepare_swap_with_enso(
        token_in="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        token_out="0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",
        swap_amount=1_000_000,  # 1 USDC
        slippage=0.01,
        remaining_tokens=universe.spot_token_addresses,
        swap_all=False,
        from_=vault_owner,
    )

    # Perform swap
    tx_hash = web3.eth.send_transaction(tx_data)
    assert_transaction_success_with_explanation(web3, tx_hash)

    # Check our balances updated
    latest_block = web3.eth.block_number
    portfolio = vault.fetch_portfolio(universe, latest_block)
    assert portfolio.spot_erc20["0x6921B130D297cc43754afba22e5EAc0FBf8Db75b"] > existing_dogmein_balance
    assert portfolio.spot_erc20["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"] < existing_usdc_balance


#@pytest.mark.skipif(CI, reason="Enso is such unstable crap that there is no hope we could run any tests with in CI")
def test_vault_swap_very_little(
    vault: VelvetVault,
    vault_owner: HexAddress,
):
    """Simulate swap tokens using Enzo.

    - Do a very small amount of USDC
    """
    web3 = vault.web3
    universe = TradingUniverse(
        spot_token_addresses={
            "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",  # DogInMe
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
        }
    )
    # Build tx using Velvet API
    tx_data = vault.prepare_swap_with_enso(
        token_in="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        token_out="0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",
        swap_amount=1,  # 1 USDC
        slippage=0.01,
        remaining_tokens=universe.spot_token_addresses,
        swap_all=False,
        from_=vault_owner,
    )

    # Perform swap
    tx_hash = web3.eth.send_transaction(tx_data)
    assert_transaction_success_with_explanation(web3, tx_hash)


#@pytest.mark.skipif(CI, reason="Enso is such unstable crap that there is no hope we could run any tests with in CI")
def test_vault_swap_sell_to_usdc(
    vault: VelvetVault,
    vault_owner: HexAddress,
):
    """Simulate swap tokens using Enzo.

    - Sell base token to get more USDC
    """
    web3 = vault.web3
    universe = TradingUniverse(
        spot_token_addresses={
            "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",  # DogInMe
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
        }
    )
    latest_block = get_almost_latest_block_number(web3)
    portfolio = vault.fetch_portfolio(universe, latest_block)
    existing_usdc_balance = portfolio.spot_erc20["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"]
    assert existing_usdc_balance > Decimal(1.0)

    # Build tx using Velvet API
    tx_data = vault.prepare_swap_with_enso(
        token_in="0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",
        token_out="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        swap_amount=500 * 10**18,
        slippage=0.01,
        remaining_tokens=universe.spot_token_addresses,
        swap_all=False,
        from_=vault_owner,
    )

    tx_hash = web3.eth.send_transaction(tx_data)
    assert_transaction_success_with_explanation(web3, tx_hash)

    latest_block = web3.eth.block_number
    portfolio = vault.fetch_portfolio(universe, latest_block)
    assert portfolio.spot_erc20["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"] > existing_usdc_balance


#@pytest.mark.skipif(CI, reason="Enso is such unstable crap that there is no hope we could run any tests with in CI")
def test_velvet_api_deposit(
    vault: VelvetVault,
    vault_owner: HexAddress,
    deposit_user: HexAddress,
    usdc: TokenDetails,
):
    """Use Velvet API to perform deposit"""

    web3 = vault.web3

    # Velvet vault tracked assets
    universe = TradingUniverse(
        spot_token_addresses={
            "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b",  # DogInMe
            usdc.address,  # USDC on Base
        }
    )

    # Check the existing portfolio USDC balance before starting the
    # the deposit process
    latest_block = get_almost_latest_block_number(web3)
    portfolio = vault.fetch_portfolio(universe, latest_block)
    existing_usdc_balance = portfolio.spot_erc20[usdc.address]
    assert existing_usdc_balance > Decimal(1.0)

    # Velvet deposit manager on Base,
    # the destination of allowance
    deposit_manager = "0xe4e23120a38c4348D7e22Ab23976Fa0c4Bf6e2ED"

    # Check there is ready-made manual approve() waiting onchain
    allowance = usdc.contract.functions.allowance(
        Web3.to_checksum_address(deposit_user),
        Web3.to_checksum_address(deposit_manager),
        ).call()
    assert allowance == 5 * 10**6

    # E               eth_defi.trace.TransactionAssertionError: Revert reason: execution reverted: revert: TransferHelper::transferFrom: transferFrom failed
    # E               Solidity stack trace:
    # E               CALL: [reverted] 0xe4e23120a38c4348D7e22Ab23976Fa0c4Bf6e2ED.0x9136d415(<unknown>) [29803 gas]
    # E               └── CALL: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913.transferFrom(sender=0x7612a94aaff7a552c373e3124654c1539a4486a8, recipient=0x6e3e0fe13dae2c42cca7ae2e849b0976e2e63e05, amount=5000000) [18763 gas]
    # E                   └── DELEGATECALL: 0x2Ce6311ddAE708829bc0784C967b7d77D19FD779.0x23b872dd(<unknown>) [11573 gas]
    # E               Transaction details:

    # Prepare the deposit tx payload
    tx_data = vault.prepare_deposit_with_enso(
        from_=deposit_user,
        deposit_token_address=usdc.address,
        amount=5 * 10**6,
    )
    assert tx_data["to"] == deposit_manager
    tx_hash = web3.eth.send_transaction(tx_data)
    try:
        assert_transaction_success_with_explanation(web3, tx_hash)
    except Exception as e:
        # Double check allowance - Anvil bug
        allowance = usdc.contract.functions.allowance(
            Web3.to_checksum_address(deposit_user),
            Web3.to_checksum_address(deposit_manager),
        ).call()
        raise RuntimeError(f"transferFrom() failed, allowance after broadcast {allowance / 10**6} USDC: {e}") from e

    # USDC balance has increased after the deposit
    portfolio = vault.fetch_portfolio(universe, web3.eth.block_number)
    assert portfolio.spot_erc20[usdc.address] > existing_usdc_balance
