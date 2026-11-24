"""Flux wallet services for the Windows desktop application."""

from flux_i18n import tr
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json

from electrum.storage import WalletStorage
from electrum.stored_dict import StorageEncryptionVersion
from flux_transaction import txid, sign
from flux_wallet import destination_script as flux_script
from wallet_keys import own_account

import mainnet_electrum
from flux_wallet import COIN, NETWORKS
from wallet_keys import KeySet


DUST = 546


def coins(value):
    return format(Decimal(value) / COIN, ".8f").rstrip("0").rstrip(".") or "0"


def parse_amount(text):
    try:
        amount = Decimal(text.strip().replace(",", "."))
        if not amount.is_finite() or not Decimal("0.00000546") <= amount < Decimal("10000000000.00000001"):
            raise ValueError
        base = amount * COIN
        if base != base.to_integral_value():
            raise ValueError
        return int(base)
    except (InvalidOperation, ValueError):
        raise ValueError(tr("Enter at least 0.00000546 FLUX with up to 8 decimal places.")) from None


def destination_script(address, network="mainnet"):
    try:
        return flux_script(address, network)
    except Exception:
        raise ValueError(tr("Enter a Flux address for {network}.").format(network=network)) from None


def network_genesis(network):
    if network != "mainnet":
        raise ValueError(tr("Mainnet is required."))
    return mainnet_electrum.GENESIS


def transport(network, run):
    if run is not None:
        return run
    if network != "mainnet":
        raise ValueError(tr("Mainnet is required."))
    return mainnet_electrum.connected


def account_from_words(words, network="mainnet"):
    network_genesis(network)
    return own_account({"words": " ".join(words.lower().split())})


class Wallet:
    def __init__(self, storage, document, network="mainnet"):
        self.network = network
        self.genesis = network_genesis(network)
        if document.get("format") != "electrum-flux-mainnet-1" or document.get("genesis") != self.genesis:
            raise ValueError(tr("This wallet file does not belong to {network}.").format(network=network))
        self.storage = storage
        self.document = document
        self.keys = KeySet(document)
        if self.keys.ledger:
            raise ValueError("Unsupported keystore")

    @classmethod
    def create(cls, path, words, password, network="mainnet"):
        return cls.create_with_keys(path, {"words": " ".join(words.lower().split())}, password, network)

    @classmethod
    def create_with_keys(cls, path, keys, password, network="mainnet"):
        if not password:
            raise ValueError(tr("Enter a password to encrypt the wallet file."))
        storage = WalletStorage(str(path))
        if storage.file_exists():
            raise ValueError(tr("A file with this name already exists."))
        doc = {**keys, "format": f"electrum-flux-{network}-1", "genesis": network_genesis(network)}
        wallet = cls(storage, doc, network)
        storage.set_password(password, StorageEncryptionVersion.USER_PASSWORD)
        wallet.save()
        return wallet

    @classmethod
    def open(cls, path, password, network="mainnet"):
        storage = WalletStorage(str(path))
        if not storage.file_exists() or not storage.is_encrypted_with_user_pw():
            raise ValueError(tr("Select an encrypted Electrum-FLUX wallet file."))
        try:
            storage.decrypt(password)
        except Exception as e:
            raise ValueError(tr("Could not open the wallet. Check the password and the file.")) from e
        return cls(storage, json.loads(storage.read()), network)

    def change_address(self, index):
        if self.keys and self.keys.entries:
            return self.address(0, 0)
        return self.address(1, index)

    def save(self):
        self.storage.write(json.dumps(self.document))

    def address(self, branch, index):
        return self.keys.address(branch, index)


@dataclass(frozen=True)
class Coin:
    txid: str
    index: int
    value: int
    branch: int
    child: int


@dataclass
class Snapshot:
    confirmed: int
    pending: int
    utxos: list
    next_indices: list
    height: int = 0
    addresses: list = field(default_factory=list)
    transactions: list = field(default_factory=list)


def discover(wallet, *, history=False, run=None):
    return transport(wallet.network, run)(lambda client: mainnet_electrum.scan(client, wallet, history))


def estimated_fee(input_count, rate, input_size=149, output_count=2):
    from embit.compact import to_bytes
    size = 27 + len(to_bytes(input_count)) + len(to_bytes(output_count)) + input_count * input_size + output_count * 34
    return (size * rate + 999) // 1000


def prepare(wallet, snapshot, destination, amount, fee_rate=1000, *, run=None):
    destination_script(destination, wallet.network)
    if type(amount) is not int or amount < DUST or not 100 <= fee_rate <= 100000:
        raise ValueError(tr("Invalid amount or fee"))
    selected = []
    total = 0
    size = wallet.keys.input_bytes()
    change = 0
    for coin in sorted(snapshot.utxos, key=lambda c: c.value, reverse=True):
        selected.append(coin)
        total += coin.value
        fee = estimated_fee(len(selected), fee_rate, size)
        change = total - amount - fee
        if change >= 0 or total >= amount + estimated_fee(len(selected), fee_rate, size, 1):
            break
    else:
        raise ValueError(tr("Insufficient confirmed FLUX to cover the amount and fee."))
    outs = [dict(address=destination, value=amount)]
    if change >= DUST:
        outs.append(dict(address=wallet.change_address(snapshot.next_indices[1]), value=change))
    proposal = dict(format="flux-v4-1", genesis=wallet.genesis, expiry=snapshot.height+120,
                    inputs=[], outputs=outs, parents={})
    for coin in selected:
        if coin.txid not in proposal["parents"]:
            proposal["parents"][coin.txid] = mainnet_electrum.raw_transaction(coin.txid, transport(wallet.network, run))
        proposal["inputs"].append(dict(txid=coin.txid, n=coin.index, branch=coin.branch, child=coin.child))
    from flux_transaction import checked_inputs
    values = [v for _, v, _, _ in checked_inputs(wallet, proposal)]
    if values != [coin.value for coin in selected]:
        raise ValueError(tr("The input amount or owner does not match."))
    if wallet.keys.kind == "multisig" or wallet.keys.watch:
        return {"proposal": proposal}
    return sign(wallet, proposal)


def seen(payment, *, run=None):
    return transport(payment["network"], run)(lambda c: mainnet_electrum.is_seen(c, payment))


def broadcast(payment, network="mainnet", *, run=None):
    if network != payment["network"] or txid(bytes.fromhex(payment["hex"])) != payment["txid"]:
        raise ValueError(tr("The TXID does not match the signed transaction."))
    return transport(network, run)(lambda c: mainnet_electrum.broadcast(c, payment))
