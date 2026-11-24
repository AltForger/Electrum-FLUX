"""Transparent Flux Sapling-v4 transfers and offline multisignature proposals."""
from io import BytesIO
from struct import pack
from hashlib import blake2b
from embit import ec
from embit.compact import to_bytes as compact
from embit.transaction import TransactionInput, TransactionOutput
from confirmations import sha256d, require
from flux_wallet import destination_script
from wallet_keys import public

HEADER = bytes.fromhex("0400008085202f89")
SEQUENCE = bytes.fromhex("ffffffff")
GENESIS = "00052461a5006c2e3b74ce48992a08695607912d5604c3eb8da25749b0900444"


def txid(raw):
    return sha256d(raw)[::-1].hex()


def read_count(stream):
    first = stream.read(1)
    require(bool(first), "Unexpected end of transaction")
    tag = first[0]
    if tag < 253:
        return tag
    size = {253: 2, 254: 4, 255: 8}[tag]
    raw = stream.read(size)
    require(len(raw) == size, "Truncated count")
    value = int.from_bytes(raw, "little")
    require(value >= {2: 253, 4: 65536, 8: 2**32}[size] and value <= 2_000_000, "Invalid compact size")
    return value


def transparent_parts(raw):
    """Read the transparent vectors only. Shielded fields are covered by the TXID."""
    stream = BytesIO(raw)
    version = int.from_bytes(stream.read(4), "little")
    if version in (5, 6):  # FluxNode registration/confirmation, no spendable outputs
        return [], []
    if version in (0x80000003, 0x80000004):
        group = stream.read(4).hex()
        require(group == ("7082c403" if version == 0x80000003 else "85202f89"), "Unknown FLUX transaction group")
    else:
        require(version in (1, 2), "Unknown FLUX transaction version")
    vin = [TransactionInput.read_from(stream) for _ in range(read_count(stream))]
    vout = [TransactionOutput.read_from(stream) for _ in range(read_count(stream))]
    return vin, vout


def blob(data):
    return compact(len(data)) + data


def push(data):
    length = len(data)
    if length < 76:
        return bytes([length]) + data
    if length < 256:
        return b"\x4c" + bytes([length]) + data
    return b"\x4d" + pack("<H", length) + data


def checked_inputs(wallet, p):
    require(p["format"] == "flux-v4-1" and p["genesis"] == GENESIS, "Wrong transaction network or format")
    require(type(p["expiry"]) is int and 0 < p["expiry"] < 500_000_000, "Invalid expiry height")
    require(bool(p["inputs"]) and bool(p["outputs"]), "Empty payment")
    seen = set()
    decoded = {}
    for item in p["inputs"]:
        h, n, branch, i = (item[k] for k in ("txid", "n", "branch", "child"))
        require(type(n) is int and n >= 0 and branch in (0, 1) and type(i) is int and 0 <= i < 2**31,
                "Invalid input or derivation")
        require((h, n) not in seen, "Duplicate input")
        seen.add((h, n))
        if h not in decoded:
            raw = bytes.fromhex(p["parents"][h])
            require(txid(raw) == h, "Incorrect previous transaction hash")
            decoded[h] = transparent_parts(raw)[1]
        parent_out = decoded[h][n]
        script = destination_script(wallet.address(branch, i))
        require(parent_out.script_pubkey.data == script and 0 < parent_out.value < 2**63, "Incorrect input owner or value")
        yield bytes.fromhex(h)[::-1] + pack("<I", n), parent_out.value, script, item


def inspect(wallet, p):
    total = sum(value for _, value, _, _ in checked_inputs(wallet, p))
    outputs = []
    for item in p["outputs"]:
        address, value = item["address"], item["value"]
        destination_script(address)
        require(type(value) is int and 0 < value <= total, "Invalid output value")
        outputs.append((address, value))
    fee = total - sum(value for _, value in outputs)
    require(fee >= 0, "Outputs exceed inputs")
    return outputs, fee


def personalized(tag, data):
    return blake2b(data, digest_size=32, person=tag).digest()


def sign(wallet, p):
    outputs, fee = inspect(wallet, p)
    inputs = list(checked_inputs(wallet, p))
    encoded = [pack("<Q", v) + blob(destination_script(a)) for a, v in outputs]
    tail = pack("<IIq", 0, p["expiry"], 0)
    common = (HEADER + personalized(b"ZcashPrevoutHash", b"".join(x[0] for x in inputs))
              + personalized(b"ZcashSequencHash", SEQUENCE * len(inputs))
              + personalized(b"ZcashOutputsHash", b"".join(encoded)) + bytes(96) + tail + pack("<I", 1))
    scripts = []
    keys = wallet.keys
    for point, value, script, item in inputs:
        branch, child = item["branch"], item["child"]
        multisig = keys.kind == "multisig"
        if multisig:
            script = keys.redeem(branch, child).data
        digest = personalized(b"ZcashSigHash" + pack("<I", 0x76b809bb),
                              common + point + blob(script) + pack("<Q", value) + SEQUENCE)
        if keys.entries:
            own = keys.entries[child][1]
        else:
            own = keys.accounts[0].derive([branch, child]).key if keys.accounts[0].is_private else None
        sigs = item.setdefault("signatures", {})
        if own:
            sigs[own.get_public_key().sec().hex()] = (own.sign(digest).serialize() + b"\x01").hex()
        if multisig:
            pubs = sorted(k.get_public_key().sec().hex() for k in keys.children(branch, child))
        elif own:
            pubs = [own.get_public_key().sec().hex()]
        else:
            pubs = []
        valid = []
        for pubkey in pubs:
            if pubkey in sigs:
                signature = bytes.fromhex(sigs[pubkey])
                require(signature[-1:] == b"\x01" and ec.PublicKey.parse(bytes.fromhex(pubkey)).verify(
                    ec.Signature.parse(signature[:-1]), digest), "Invalid cosigner signature")
                valid.append(signature)
        if len(valid) < keys.threshold:
            scripts.append(None)
        elif multisig:
            scripts.append(b"\x00" + b"".join(push(s) for s in valid[:keys.threshold]) + push(script))
        else:
            scripts.append(push(valid[0]) + push(bytes.fromhex(pubs[0])))
    if None in scripts:
        return {"proposal": p}
    body = b"".join(inp[0] + blob(s) + SEQUENCE for inp, s in zip(inputs, scripts))
    raw = HEADER + compact(len(inputs)) + body + compact(len(encoded)) + b"".join(encoded) + tail + bytes(3)
    return dict(network="mainnet", address=outputs[0][0], amount=outputs[0][1], fee=fee, expiry=p["expiry"],
                txid=txid(raw), hex=raw.hex())
