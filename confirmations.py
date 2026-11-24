"""Verify native FLUX transaction inclusion against the explorer's active chain.

Usage: python confirmations.py TXID [--network mainnet] [--electrum HOST:PORT]
The optional Electrum connection is read-only TCP. HTTPS anchors the block hash.
"""

import argparse
import hashlib
import json
import socket
from urllib.request import Request, urlopen


NETWORKS = {
    "testnet": ("https://testnet.runonflux.io/api",
                "0042202a64a929fc25cc10e68615ddbe38007b1b40da08acd3f530f83c79b9d1"),
    "mainnet": ("https://explorer.runonflux.io/api",
                "00052461a5006c2e3b74ce48992a08695607912d5604c3eb8da25749b0900444"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def internal_hash(value):
    data = bytes.fromhex(value)
    require(len(data) == 32, "Expected a 32-byte hash")
    return data[::-1]


def root_for_block(txids, expected_txid):
    require(len(set(txids)) == len(txids), "Duplicate transaction in block")
    require(expected_txid in txids, "Transaction absent from block")
    level = [internal_hash(txid) for txid in txids]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256d(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def block_hash(header):
    # fluxd CBlockHeader::SerializationOp omits the PoN signature for hashing.
    version = int.from_bytes(header[:4], "little", signed=True)
    if version >= 100:
        require(len(header) >= 144, "Short proof-of-node header")
        return sha256d(header[:144])[::-1].hex()
    require(len(header) >= 141, "Short proof-of-work header")
    marker = header[140]
    width = {253: 2, 254: 4, 255: 8}.get(marker, 0)
    size = int.from_bytes(header[141:141 + width], "little") if width else marker
    end = 141 + width + size
    require(len(header) >= end, "Truncated Equihash solution")
    return sha256d(header[:end])[::-1].hex()


def explorer_get(base, path):
    request = Request(base + "/" + path, headers={"User-Agent": "Electrum-FLUX/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def verify(txid, network, rpc=None):
    base, genesis = NETWORKS[network]
    txhash = internal_hash(txid)

    def get(path):
        return explorer_get(base, path)

    def hash_at(height):
        return get(f"block-index/{height}")["blockHash"]

    require(hash_at(0) == genesis, "Wrong explorer network")
    if rpc:
        rpc("server.version", ["FluxConfirmationCheck", "1.4"])
        require(rpc("server.features", [])["genesis_hash"] == genesis, "Wrong Electrum network")
    tx = get("tx/" + txid)
    raw = (rpc("blockchain.transaction.get", [txid]) if rpc
           else get("rawtx/" + txid)["rawtx"])
    require(sha256d(bytes.fromhex(raw)) == txhash, "Raw transaction hash mismatch")
    height = tx.get("blockheight")
    if height is None or height < 1 or not tx.get("blockhash"):
        return {"network": network, "txid": txid, "confirmations": 0}
    canonical = hash_at(height)
    require(canonical == tx["blockhash"], "Transaction block is no longer active")
    block = get("block/" + canonical)
    if rpc:
        header = bytes.fromhex(rpc("blockchain.block.header", [height]))
    else:
        header = bytes.fromhex(get("rawblock/" + canonical)["rawblock"])
    require(block_hash(header) == canonical, "Header hash mismatch")
    root = root_for_block(block["tx"], txid)
    require(root == header[36:68] == internal_hash(block["merkleroot"]), "Merkle root mismatch")
    tip = get("status?q=getInfo")["info"]["blocks"]
    if rpc:
        tip = min(tip, rpc("blockchain.headers.subscribe", [])["height"])
        require(block_hash(bytes.fromhex(rpc("blockchain.block.header", [tip]))) == hash_at(tip),
                "Electrum and explorer report different chains")
    require(tip >= height and hash_at(height) == canonical, "Chain changed; retry")
    return {"network": network, "txid": txid, "block": canonical, "height": height,
            "tip": tip, "confirmations": tip - height + 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("txid")
    parser.add_argument("--network", choices=NETWORKS, default="testnet")
    parser.add_argument("--electrum", metavar="HOST:PORT")
    args = parser.parse_args()
    if not args.electrum:
        print(json.dumps(verify(args.txid, args.network)))
        return
    host, port = args.electrum.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=15) as sock, sock.makefile("rb") as stream:
        sequence = 0

        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            sock.sendall((json.dumps(dict(id=sequence, method=method, params=params)) + "\n").encode())
            while line := stream.readline():
                msg = json.loads(line)
                if msg.get("id") == sequence:
                    require(not msg.get("error"), str(msg.get("error")))
                    return msg["result"]
            raise ConnectionError("Electrum disconnected")

        print(json.dumps(verify(args.txid, args.network, rpc)))


if __name__ == "__main__":
    main()
