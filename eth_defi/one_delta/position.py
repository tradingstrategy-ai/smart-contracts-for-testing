"""1delta integration position handlers"""
from web3.contract.contract import Contract, ContractFunction

from eth_defi.aave_v3.constants import MAX_AMOUNT, AaveV3InterestRateMode
from eth_defi.one_delta.constants import Exchange, TradeOperation
from eth_defi.one_delta.deployment import OneDeltaDeployment
from eth_defi.one_delta.utils import encode_path


def approve(
    one_delta_deployment: OneDeltaDeployment,
    *,
    collateral_token: Contract,
    borrow_token: Contract,
    atoken: Contract,
    vtoken: Contract,
) -> list[ContractFunction]:
    trader = one_delta_deployment.flash_aggregator
    proxy = one_delta_deployment.broker_proxy
    aave_v3_pool = one_delta_deployment.aave_v3.pool

    approval_functions = []

    # TODO: double check if we need to approve everything here
    for token in [
        collateral_token,
        borrow_token,
        atoken,
    ]:
        approval_functions.append(token.functions.approve(trader.address, MAX_AMOUNT))
        approval_functions.append(token.functions.approve(aave_v3_pool.address, MAX_AMOUNT))

    # approve delegate the vToken
    approval_functions.append(vtoken.functions.approveDelegation(proxy.address, MAX_AMOUNT))

    return approval_functions


def open_short_position(
    one_delta_deployment: OneDeltaDeployment,
    *,
    collateral_token: Contract,
    borrow_token: Contract,
    pool_fee: int,
    borrow_amount: int,
    min_collateral_amount_out: int = 0,
    exchange: Exchange = Exchange.UNISWAP_V3,
    interest_mode: AaveV3InterestRateMode = AaveV3InterestRateMode.VARIABLE,
) -> ContractFunction:
    """

    NOTE: only single hop swap is supported at the moment
    """
    path = encode_path(
        path=[
            borrow_token.address,
            collateral_token.address,
        ],
        fees=[pool_fee],
        exchanges=[exchange],
        operation=TradeOperation.OPEN,
        interest_mode=interest_mode,
    )

    return one_delta_deployment.flash_aggregator.functions.flashSwapExactIn(
        borrow_amount,
        min_collateral_amount_out,
        path,
    )


def close_short_position(
    one_delta_deployment: OneDeltaDeployment,
    *,
    collateral_token: Contract,
    borrow_token: Contract,
    pool_fee: int,
    exchange: Exchange = Exchange.UNISWAP_V3,
    interest_mode: AaveV3InterestRateMode = AaveV3InterestRateMode.VARIABLE,
) -> ContractFunction:
    """

    NOTE: only single hop swap is supported at the moment
    """
    path = encode_path(
        path=[
            borrow_token.address,
            collateral_token.address,
        ],
        fees=[pool_fee],
        exchanges=[exchange],
        operation=TradeOperation.CLOSE,
        interest_mode=interest_mode,
    )

    return one_delta_deployment.flash_aggregator.functions.flashSwapAllOut(
        MAX_AMOUNT,
        path,
    )
