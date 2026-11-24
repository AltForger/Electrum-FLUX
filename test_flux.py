import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from embit import base58, ec
from embit.transaction import Transaction, TransactionInput, TransactionOutput
from embit.script import Script

from desktop_wallet import Wallet, Coin, Snapshot, prepare, destination_script, estimated_fee
from flux_wallet import create_phrase
from flux_transaction import GENESIS, inspect, sign, transparent_parts, txid
from wallet_keys import own_account, public, single_address
from flux_i18n import set_ui_language, tr
from mainnet_electrum import broadcast, ServerError


class WalletTests(unittest.TestCase):
    def wallet(self, **keys):
        return Wallet(None, dict(keys, format="electrum-flux-mainnet-1", genesis=GENESIS))

    def transfer(self, wallet, amount=50000):
        recipient = single_address(ec.PrivateKey(bytes([12])*32).get_public_key())
        previous = Transaction(vin=[TransactionInput(bytes([4])*32, 0)], vout=[
            TransactionOutput(100000, Script(destination_script(wallet.address(0, 0))))])
        raw = previous.serialize()
        coin = Coin(txid(raw), 0, 100000, 0, 0)
        snapshot = Snapshot(100000, 0, [coin], [1,0], 3150000)
        with patch("mainnet_electrum.raw_transaction", return_value=raw.hex()):
            return prepare(wallet, snapshot, recipient, amount)

    def test_create_restore_open(self):
        words = create_phrase()
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp)/"default_wallet"
            w = Wallet.create(f, words, "unit-test-only")
            self.assertNotIn(words.encode(), f.read_bytes())
            self.assertEqual(w.address(0,3), Wallet.open(f,"unit-test-only").address(0,3))
            self.assertEqual(w.address(1,2), self.wallet(words=words).address(1,2))
            with self.assertRaises(ValueError):
                Wallet.open(f,"wrong")
            with self.assertRaises(ValueError):
                Wallet.create(f, words, "another-password")

    def test_standard_and_max(self):
        w = self.wallet(words=create_phrase())
        partial = self.transfer(w)
        self.assertEqual(len(transparent_parts(bytes.fromhex(partial["hex"]))[1]), 2)
        fee = estimated_fee(1, 1000, w.keys.input_bytes(), 1)
        sweep = self.transfer(w, 100000-fee)
        raw = bytes.fromhex(sweep["hex"])
        self.assertEqual(sweep["txid"], txid(raw))
        self.assertEqual(sweep["fee"], fee)
        self.assertEqual(len(transparent_parts(raw)[1]), 1)
        self.assertGreaterEqual(fee, len(raw))

    def test_import_and_watch(self):
        key = ec.PrivateKey(bytes([23])*32)
        wif = base58.encode_check(b"\x80"+key.secret+b"\x01")
        spend = self.wallet(kind="import",entries=[wif])
        watch = self.wallet(kind="import",entries=[spend.address(0,0)])
        plan = self.transfer(watch)["proposal"]
        self.assertIn("proposal", sign(watch,plan))
        self.assertIn("txid", sign(spend,plan))
        plan["parents"][plan["inputs"][0]["txid"]] += "00"
        with self.assertRaises(ValueError):
            inspect(spend,plan)

    def test_new_and_retried_broadcast(self):
        plan = self.transfer(self.wallet(words=create_phrase()))
        client = Mock()
        client.call.side_effect = [ServerError("daemon error -5: No information available about transaction"), plan["txid"]]
        broadcast(client, plan)
        client.call.assert_called_with("blockchain.transaction.broadcast", [plan["hex"]])
        client.reset_mock()
        client.call.side_effect = [plan["hex"]]
        broadcast(client, plan)
        client.call.assert_called_once_with("blockchain.transaction.get", [plan["txid"]])

    def test_multisig_offline(self):
        docs = [dict(words=create_phrase()) for _ in range(2)]
        pubs = [public(own_account(d)).to_base58() for d in docs]
        wallets = [self.wallet(**d,kind="multisig",threshold=2,cosigners=[pubs[1-i]]) for i,d in enumerate(docs)]
        self.assertEqual(wallets[0].address(0,0),wallets[1].address(0,0))
        plan = self.transfer(wallets[0])["proposal"]
        signed = sign(wallets[0],plan)["proposal"]
        final = sign(wallets[1],signed)
        self.assertEqual(final["txid"],txid(bytes.fromhex(final["hex"])))
        signed["outputs"][0]["value"] -= 100
        with self.assertRaises(ValueError):
            sign(wallets[1],signed)

    def test_languages_and_network(self):
        try:
            self.assertEqual(set_ui_language(["ru-RU"]), "ru")
            self.assertNotEqual(tr("Receive"), "Receive")
            self.assertEqual(set_ui_language(["en-US"]), "en")
            self.assertEqual(tr("Receive"), "Receive")
        finally:
            set_ui_language(["en"])
        with self.assertRaises(ValueError):
            destination_script(base58.encode_check(bytes.fromhex("1d25")+bytes(20)))
