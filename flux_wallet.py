"""Native FLUX transparent mainnet keys and payment scripts."""
import secrets
from embit import base58, bip39, hashes

COIN = 100_000_000
NETWORKS = {"mainnet": (19167, bytes.fromhex("1cb8"))}
P2PKH = bytes.fromhex("1cb8")
P2SH = bytes.fromhex("1cbd")


def create_phrase():
    return bip39.mnemonic_from_bytes(secrets.token_bytes(16))


def destination_script(address, network="mainnet"):
    if network != "mainnet":
        raise ValueError("Expected Flux mainnet")
    payload = base58.decode_check(address)
    if len(payload) == 22:
        if payload[:2] == P2PKH:
            return bytes.fromhex("76a914") + payload[2:] + bytes.fromhex("88ac")
        if payload[:2] == P2SH:
            return bytes.fromhex("a914") + payload[2:] + bytes.fromhex("87")
    raise ValueError("Expected a transparent Flux mainnet address")


def output_address(script, network="mainnet"):
    if len(script) == 25 and script[:3].hex() == "76a914" and script[-2:].hex() == "88ac":
        return base58.encode_check(P2PKH + script[3:-2])
    if len(script) == 23 and script[:2].hex() == "a914" and script[-1] == 135:
        return base58.encode_check(P2SH + script[2:-1])
    return None
