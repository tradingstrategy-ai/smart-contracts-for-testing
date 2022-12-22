"""Measurements of block time."""
from web3 import Web3

from eth_defi.event_reader.conversion import convert_jsonrpc_value_to_int


def measure_block_time(web3: Web3, n=5) -> float:
    """Measure block time over N blocks.

    :return:
        Block time in seconds
    """

    last_block = web3.eth.block_number
    start_block = last_block - n

    last_block_data = web3.eth.get_block(last_block)
    start_block_data = web3.eth.get_block(start_block)

    end_time = convert_jsonrpc_value_to_int(last_block_data["timestamp"])
    start_time = convert_jsonrpc_value_to_int(start_block_data["timestamp"])

    return (end_time - start_time) / n
