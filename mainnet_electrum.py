"""Flux mainnet wallet synchronization and broadcast over Electrum TCP."""
from flux_i18n import tr
from contextlib import ExitStack
from threading import Lock
import ipaddress
import re
import hashlib
import json
import socket

from confirmations import NETWORKS, block_hash, explorer_get, root_for_block
from flux_transaction import GENESIS, transparent_parts, txid as hash_transaction
from flux_wallet import output_address
SERVERS = ("electrumx.runonflux.io", "electrumx2.runonflux.io")

from flux_version import VERSION


class ServerError(ValueError):
    pass


class Client(ExitStack):
    def __init__(self, host, port=50001):
        super().__init__()
        self.host, self.port = host, port
        self.sequence = 0

    def __enter__(self):
        try:
            tcp = self.enter_context(socket.create_connection((self.host, self.port), timeout=8))
            self.sock = tcp
            self.sock.settimeout(20)
            self.stream = self.enter_context(self.sock.makefile("rb"))
            self.call("server.version", [f"Electrum-FLUX {VERSION}", "1.4"])
            header = bytes.fromhex(self.call("blockchain.block.header", [0]))
            if block_hash(header) != GENESIS or self.call("server.features", [])["genesis_hash"] != GENESIS:
                raise ValueError(tr("The server is connected to a different network."))
            self.cached_blocks = {}
            return self
        except BaseException:
            self.close()
            raise

    def many(self, method, arguments):
        results = []
        for start in range(0, len(arguments), 20):
            requests = []
            for params in arguments[start:start + 20]:
                self.sequence += 1
                requests.append(dict(jsonrpc="2.0", id=self.sequence, method=method, params=params))
            self.sock.sendall(b"".join(json.dumps(req).encode() + b"\n" for req in requests))
            ids = {req["id"] for req in requests}
            replies = {}
            while len(replies) < len(ids):
                line = self.stream.readline(8_000_001)
                if len(line) > 8_000_000 or not line.endswith(b"\n"):
                    raise ConnectionError(tr("Incomplete response from the Electrum server."))
                msg = json.loads(line)
                if "method" in msg and msg.get("id") is None:
                    continue
                idx = msg.get("id")
                if idx not in ids or idx in replies:
                    raise ValueError(tr("Invalid response from the Electrum server."))
                if msg.get("error"):
                    raise ServerError(str(msg["error"]))
                replies[idx] = msg["result"]
            results.extend(replies[req["id"]] for req in requests)
        return results

    def call(self, method, params):
        return self.many(method, [params])[0]


def server_endpoint(host, port):
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError(tr("Check the server name.")) from None
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                     for part in host.split(".")):
            raise ValueError(tr("Enter a server name or IP address. Enter the port separately."))
    if not 1 <= port <= 65535:
        raise ValueError(tr("The port must be between 1 and 65535."))
    return host, port


def endpoint_text(endpoint):
    host, port = endpoint
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class WalletConnection:
    """One serialized RPC stream per open wallet. Publish state after genesis checks."""
    def __init__(self, manual=None):
        self.manual = manual
        self.client = None
        self.status = (tr("Disconnected"), "", "")
        self._lock = Lock()
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
            client, self.client = self.client, None
            self.status = (tr("Disconnected"), "", "")
        if client:
            try:
                client.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

    def run(self, operation):
        targets = [self.manual] if self.manual else [(h, 50001) for h in SERVERS]
        current = self.client
        if current:
            active = (current.host, current.port)
            targets = [active] + [target for target in targets if target != active]
        errors = []
        for target in targets:
            label = endpoint_text(target)
            with self._lock:
                if self._closed:
                    raise ConnectionError(tr("Connection closed."))
                client = self.client
                if client is None:
                    self.status = (tr("Connecting…"), label, "")
            try:
                if client is None:
                    client = Client(*target).__enter__()
                    with self._lock:
                        if self._closed:
                            client.close()
                            raise ConnectionError(tr("Connection closed."))
                        self.client = client
                        self.status = (tr("Connected · TCP"), label, "")
                return operation(client)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{label} — {exc}")
                with self._lock:
                    if self.client is client:
                        self.client = None
                    if not self._closed:
                        self.status = (tr("Disconnected"), "", "\n".join(errors))
                if client:
                    client.close()
        raise ConnectionError("\n".join(errors))


def connected(operation):
    errors = []
    for host in SERVERS:
        try:
            with Client(host) as client:
                return operation(client)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{host}: {exc}")
    raise ConnectionError("\n".join(errors))


def raw_transaction(txid, run=None):
    return (run or connected)(lambda c: c.call("blockchain.transaction.get", [txid]))


def is_seen(client, payment):
    try:
        raw = client.call("blockchain.transaction.get", [payment["txid"]])
    except ServerError as exc:
        if any(t in str(exc).lower() for t in ("not found", "no such", "not in", "unknown transaction",
                                               "non-wallet transaction", "no information available about transaction")):
            return False
        raise
    return raw.lower() == payment["hex"].lower()


def broadcast(client, payment):
    if is_seen(client, payment):
        return
    result = client.call("blockchain.transaction.broadcast", [payment["hex"]])
    if result != payment["txid"]:
        raise ValueError(tr("The server returned a different transaction ID."))


def scan(client, wallet, history):
    from desktop_wallet import Coin, Snapshot, destination_script

    tip = client.call("blockchain.headers.subscribe", [])["height"]
    base = NETWORKS["mainnet"][0]
    tip = min(tip, explorer_get(base, "status?q=getInfo")["info"]["blocks"])
    def canonical(height):
        return explorer_get(base, f"block-index/{height}")["blockHash"]
    if block_hash(bytes.fromhex(client.call("blockchain.block.header", [tip]))) != canonical(tip):
        raise ValueError("Electrum and Flux explorer disagree on the current chain")
    owned = {}
    heights = {}
    next_indices = [0, 0]
    fixed = wallet.keys.entries if wallet.keys else []
    for branch in range(1 if fixed else 2):
        start = 0
        while start < (len(fixed) if fixed else next_indices[branch] + 20):
            end = len(fixed) if fixed else next_indices[branch] + 20
            addresses = [wallet.address(branch, i) for i in range(start, end)]
            hashes = [hashlib.sha256(destination_script(a, wallet.network)).digest()[::-1].hex() for a in addresses]
            histories = client.many("blockchain.scripthash.get_history", [[h] for h in hashes])
            for index, (addr, entries) in enumerate(zip(addresses, histories), start):
                if fixed or entries:
                    owned[addr] = (branch, index, len(entries))
                if entries:
                    next_indices[branch] = index + 1
                    for entry in entries:
                        heights[entry["tx_hash"]] = entry["height"]
            start += len(addresses)

    ids = list(heights)
    raws = client.many("blockchain.transaction.get", [[txid] for txid in ids])
    outputs, spent, records = {}, set(), {}
    for txid, raw in zip(ids, raws):
        raw = bytes.fromhex(raw)
        vin, outs = transparent_parts(raw)
        if hash_transaction(raw) != txid:
            raise ValueError(tr("The retrieved transaction has a different TXID."))
        is_coinbase = len(vin) == 1 and vin[0].txid == bytes(32) and vin[0].vout == 0xffffffff
        inputs = [(i.txid.hex(), i.vout) for i in vin]
        spent.update(inputs)
        vout = []
        for n, out in enumerate(outs):
            addr = output_address(out.script_pubkey.data, wallet.network)
            vout.append({"addresses": [addr] if addr else [], "value": out.value})
            if addr in owned:
                outputs[txid, n] = (addr, out.value, is_coinbase)
        records[txid] = (inputs, vout)

    balance = dict.fromkeys(owned, 0)
    confirmed = pending = 0
    utxos = []
    for outpoint, (addr, value, coinbase) in outputs.items():
        if outpoint not in spent:
            balance[addr] += value
            txid, n = outpoint
            height = heights[txid]
            if height > 0 and (not coinbase or tip - height + 1 >= 100):
                branch, idx, _ = owned[addr]
                utxos.append(Coin(txid, n, value, branch, idx))

    dates = {}
    by_height = {}
    for txid, height in heights.items():
        if height > 0:
            by_height.setdefault(height, []).append(txid)
    for h, members in by_height.items():
        block_id = canonical(h)
        if h > tip:
            raise ValueError("Chain changed during synchronization; retry")
        cached = client.cached_blocks.get(h)
        if cached is None or block_id != cached[0]:
            header = bytes.fromhex(client.call("blockchain.block.header", [h]))
            block = explorer_get(base, "block/" + block_id)
            if block_hash(header) != block_id or root_for_block(block["tx"], members[0]) != header[36:68]:
                raise ValueError("Invalid FLUX block or Merkle root")
            cached = (block_id, set(block["tx"]), int.from_bytes(header[100:104], "little"))
            client.cached_blocks[h] = cached
        if not set(members).issubset(cached[1]):
            raise ValueError("Transaction absent from canonical block")
        dates[h] = cached[2]
    transactions = []
    for txid, (ins, vout) in records.items():
        vin = [{"addresses": [outputs[i][0]], "value": outputs[i][1]} for i in ins if i in outputs]
        incoming = sum(o["value"] for o in vout if any(a in owned for a in o["addresses"]))
        outgoing = sum(i["value"] for i in vin)
        height = heights[txid]
        if height > 0:
            confirmed += incoming - outgoing
        else:
            pending += incoming - outgoing
        if history:
            fee = outgoing - sum(o["value"] for o in vout) if len(vin) == len(ins) else None
            transactions.append(dict(txid=txid, vin=vin, vout=vout, confirmations=max(0, tip - height + 1) if height > 0 else 0,
                                     blockHeight=height if height > 0 else None, blockTime=dates.get(height), fees=fee))
    transactions.sort(key=lambda t: (t["confirmations"] == 0, t["blockHeight"] or 0), reverse=True)
    rows = [(branch, idx, a, balance[a], count) for a, (branch, idx, count) in owned.items()]
    if by_height and block_hash(bytes.fromhex(client.call("blockchain.block.header", [tip]))) != canonical(tip):
        raise ValueError("Chain changed during synchronization; retry")
    return Snapshot(confirmed, pending, utxos, next_indices, tip, rows, transactions)
