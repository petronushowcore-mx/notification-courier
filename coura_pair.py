"""Pairing codes, device keys, and a small text QR.

Secret keys stay on the device that created them. A pairing message carries
an address, a public key, and a signature. The short code is the human check
that both sides are looking at the same exchange.
"""
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

import courier


P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
I = pow(2, (P - 1) // 4, P)
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
JOIN_SCHEMA = "coura.pair.join.v1"
ACCEPT_SCHEMA = "coura.pair.accept.v1"


class PairError(Exception):
    """Pairing refused. No peer was trusted."""


def _sha(data):
    return hashlib.sha512(data).digest()


def _recover_x(y, sign):
    if y >= P:
        raise PairError("Public key is not a point")
    y2 = y * y % P
    xx = (y2 - 1) * pow(D * y2 + 1, P - 2, P) % P
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = x * I % P
    if (x * x - xx) % P != 0:
        raise PairError("Public key is not a point")
    if x == 0 and sign:
        raise PairError("Point encoding is not canonical")
    if x % 2 != sign:
        x = P - x
    return x


def _add(pt, qt):
    x1, y1 = pt
    x2, y2 = qt
    product = D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * pow(1 + product, P - 2, P) % P
    y3 = (y1 * y2 + x1 * x2) * pow(1 - product, P - 2, P) % P
    return x3, y3


def _scale(pt, scalar):
    result = (0, 1)
    while scalar:
        if scalar & 1:
            result = _add(result, pt)
        pt = _add(pt, pt)
        scalar >>= 1
    return result


_BY = (4 * pow(5, P - 2, P)) % P
_B = (_recover_x(_BY, 0), _BY)


def _encode(pt):
    x, y = pt
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data):
    if len(data) != 32:
        raise PairError("Public key is not a point")
    raw = int.from_bytes(data, "little")
    sign = (raw >> 255) & 1
    y = raw & ((1 << 255) - 1)
    point = (_recover_x(y, sign), y)
    if _encode(point) != data:
        raise PairError("Point encoding is not canonical")
    return point


def _expand(seed):
    if len(seed) != 32:
        raise PairError("Local secret key is not 32 bytes")
    digest = _sha(seed)
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed):
    scalar, _prefix = _expand(seed)
    return _encode(_scale(_B, scalar))


def sign(seed, message):
    scalar, prefix = _expand(seed)
    encoded = _encode(_scale(_B, scalar))
    nonce = int.from_bytes(_sha(prefix + message), "little") % L
    point = _encode(_scale(_B, nonce))
    challenge = int.from_bytes(_sha(point + encoded + message), "little") % L
    return point + ((nonce + challenge * scalar) % L).to_bytes(32, "little")


def verify(public, message, signature):
    try:
        if len(signature) != 64:
            return False
        point = _decode(signature[:32])
        account = _decode(public)
        if account == (0, 1) or _scale(account, L) != (0, 1):
            return False
        if _scale(point, L) != (0, 1):
            return False
        scalar = int.from_bytes(signature[32:], "little")
        if scalar >= L:
            return False
        challenge = int.from_bytes(_sha(signature[:32] + public + message), "little") % L
        return _scale(_B, scalar) == _add(point, _scale(account, challenge))
    except (PairError, ValueError):
        return False


def canonical(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def normalize_code(text):
    if type(text) is not str:
        raise PairError("That code is not a Coura pairing code")
    raw = "".join(char for char in text.upper() if char != "-" and not char.isspace())
    if len(raw) != 8 or any(char not in ALPHABET for char in raw):
        raise PairError("That code is not a Coura pairing code")
    return raw[:4] + "-" + raw[4:]


def _code_hash(code):
    return hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


def _write_private(path, data):
    courier.ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity_paths(state):
    root = Path(state) / "identity"
    return root / "secret.key", root / "public.key"


def ensure_identity(state):
    secret_path, public_path = identity_paths(state)
    with courier.file_lock(secret_path.parent / "gate"):
        if secret_path.exists():
            secret = secret_path.read_bytes()
            public = public_key(secret)
            if not public_path.exists() or public_path.read_bytes() != public:
                _write_private(public_path, public)
            return secret, public
        secret = secrets.token_bytes(32)
        public = public_key(secret)
        _write_private(secret_path, secret)
        _write_private(public_path, public)
        return secret, public


def _offer_path(state):
    return Path(state) / "pairing" / "offer.json"


def _peers_path(state):
    return Path(state) / "pairing" / "peers.json"


def _pending_path(state):
    return Path(state) / "pairing" / "pending.json"


def _read_json(path, fallback):
    if not path.exists():
        return fallback
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not type(fallback):
        raise PairError("Pairing state is unreadable")
    return value


def _write_json(path, value):
    _write_private(path, (json.dumps(
        value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def peer_key(kind, identity):
    address = courier.Address(kind, identity)
    return address.client + ":" + address.identity


def _trusted_peers(state):
    """Normalize legacy id-only slots from each row's full address."""
    peers = {}
    for row in _read_json(_peers_path(state), {}).values():
        if type(row) is not dict:
            raise PairError("Pairing state is unreadable")
        try:
            key = peer_key(row.get("kind"), row.get("id"))
        except courier.Refused as error:
            raise PairError("Pairing state is unreadable") from error
        if key in peers and peers[key] != row:
            raise PairError("Conflicting pairing state is unreadable")
        peers[key] = row
    return peers


def _pairing_lock(state):
    return courier.file_lock(Path(state) / "pairing" / "gate")


def create_offer(state, kind, identity, ttl, now):
    """Create a single-use code. Only its hash is stored.

    An unused code that has not expired is left in place. A later offer does
    not replace it.
    """
    if type(ttl) is not int or ttl < 0 or ttl > 3600:
        raise PairError("Pairing lifetime must be from 0 to 3600 seconds")
    if type(now) is not int:
        raise PairError("Pairing time is invalid")
    address = courier.Address(kind, identity)
    _secret, public = ensure_identity(state)
    raw = "".join(secrets.choice(ALPHABET) for _index in range(8))
    code = raw[:4] + "-" + raw[4:]
    with _pairing_lock(state):
        path = _offer_path(state)
        current = _read_json(path, {}) if path.exists() else {}
        expires = current.get("expires")
        if (current and current.get("used") is not True and
                type(expires) is int and now < expires):
            raise PairError(
                "An unused pairing code is already waiting. It was not replaced.")
        _write_json(path, {
            "code_sha256": _code_hash(code),
            "expires": now + ttl,
            "used": False,
            "kind": address.client,
            "id": address.identity,
            "public_key": public.hex(),
        })
    return code


def _sign_body(body, secret):
    signed = dict(body)
    signed["signature"] = sign(secret, canonical(body)).hex()
    payload = canonical(signed)
    if len(payload) > courier.MAX_DATA:
        raise PairError("Pairing message exceeds 6144 bytes")
    return payload


def _open_signed(payload):
    if type(payload) is not bytes or len(payload) > courier.MAX_DATA:
        raise PairError("Pairing message exceeds 6144 bytes")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=courier.unique_object)
    except (UnicodeError, ValueError):
        raise PairError("Pairing message is not JSON")
    if (type(document) is not dict or type(document.get("signature")) is not str
            or type(document.get("public_key")) is not str):
        raise PairError("Pairing signature did not verify")
    try:
        signature = bytes.fromhex(document["signature"])
        public = bytes.fromhex(document["public_key"])
    except ValueError:
        raise PairError("Pairing signature did not verify")
    body = {key: value for key, value in document.items() if key != "signature"}
    if not verify(public, canonical(body), signature):
        raise PairError("Pairing signature did not verify")
    return document


def join_payload(state, code, kind, identity):
    address = courier.Address(kind, identity)
    secret, public = ensure_identity(state)
    body = {
        "schema": JOIN_SCHEMA,
        "code": normalize_code(code),
        "address": {"kind": address.client, "id": address.identity},
        "public_key": public.hex(),
    }
    _write_json(_pending_path(state), {
        "code_sha256": _code_hash(code),
        "kind": address.client,
        "id": address.identity,
    })
    return _sign_body(body, secret)


def _matching_alias(instance, address):
    hits = [name for name, target in instance.recipients.items()
            if target.client == address["kind"] and target.identity == address["id"]]
    if len(hits) != 1:
        raise PairError("No configured route matches that address. Pairing does not add one.")
    return hits[0]


def review_join(state, payload, now):
    """Check a join message. This does not trust the peer or spend the code."""
    document = _open_signed(payload)
    if document.get("schema") != JOIN_SCHEMA:
        raise PairError("That message is not a pairing join")
    offer = _read_json(_offer_path(state), {})
    if not offer:
        raise PairError("There is no open pairing code on this device")
    if offer.get("used") is True:
        raise PairError("That pairing code was already used")
    try:
        presented = _code_hash(document.get("code", ""))
    except PairError:
        raise PairError("That code does not match the one this device printed")
    if presented != offer.get("code_sha256"):
        raise PairError("That code does not match the one this device printed")
    if type(offer.get("expires")) is not int or now >= offer["expires"]:
        raise PairError("That pairing code has expired")
    address = document.get("address")
    if type(address) is not dict:
        raise PairError("Pairing address is missing")
    courier.Address(address.get("kind"), address.get("id"))
    return {
        "kind": address["kind"],
        "id": address["id"],
        "public_key": document["public_key"],
    }


def reply_to_join(state, payload, now):
    """Check a join and build the signed reply. This does not trust or spend."""
    preview = review_join(state, payload, now)
    offer = _read_json(_offer_path(state), {})
    secret, public = ensure_identity(state)
    body = {
        "schema": ACCEPT_SCHEMA,
        "code": normalize_code(json.loads(payload.decode("utf-8"))["code"]),
        "address": {"kind": offer["kind"], "id": offer["id"]},
        "public_key": public.hex(),
    }
    return preview, _sign_body(body, secret)


def commit_join(state, payload, now):
    """Spend the code and trust the peer that review_join accepts."""
    with _pairing_lock(state):
        preview = review_join(state, payload, now)
        offer = _read_json(_offer_path(state), {})
        offer["used"] = True
        _write_json(_offer_path(state), offer)
        peers = _trusted_peers(state)
        peers[peer_key(preview["kind"], preview["id"])] = {
            "kind": preview["kind"],
            "id": preview["id"],
            "public_key": preview["public_key"],
            "confirmed": True,
        }
        _write_json(_peers_path(state), peers)
        return preview


def confirm_join(state, payload, now):
    """Trust the join, spend the code, and build the signed reply."""
    _preview, reply = reply_to_join(state, payload, now)
    commit_join(state, payload, now)
    return reply


def accept_attempt(state):
    offer = _read_json(_offer_path(state), {})
    attempt = offer.get("accept_attempt", 0)
    if type(attempt) is not int or attempt < 0 or attempt > 1000:
        raise PairError("Pairing attempt is invalid")
    return attempt


def accept_transport_key(code, attempt):
    """Journal key for one reply attempt. A refused attempt uses the next key."""
    if type(attempt) is not int or attempt < 0 or attempt > 1000:
        raise PairError("Pairing attempt is invalid")
    base = "pair-accept-" + _code_hash(code)[:16]
    if attempt == 0:
        return base
    return "%s-%d" % (base, attempt)


def remember_refusal(state, payload, now):
    """Record that this reply attempt was refused. The code stays unused."""
    with _pairing_lock(state):
        review_join(state, payload, now)
        offer = _read_json(_offer_path(state), {})
        attempt = offer.get("accept_attempt", 0)
        if type(attempt) is not int or attempt < 0 or attempt >= 1000:
            raise PairError("Pairing attempt is invalid")
        offer["accept_attempt"] = attempt + 1
        _write_json(_offer_path(state), offer)
        return offer["accept_attempt"]


def review_accept(state, payload):
    document = _open_signed(payload)
    if document.get("schema") != ACCEPT_SCHEMA:
        raise PairError("That message is not a pairing reply")
    pending = _read_json(_pending_path(state), {})
    if not pending:
        raise PairError("This device has not entered a pairing code")
    if _code_hash(document.get("code", "")) != pending.get("code_sha256"):
        raise PairError("The reply is for a different pairing code")
    address = document.get("address")
    if type(address) is not dict:
        raise PairError("Pairing address is missing")
    courier.Address(address.get("kind"), address.get("id"))
    return {"kind": address["kind"], "id": address["id"], "public_key": document["public_key"]}


def confirm_accept(state, payload):
    with _pairing_lock(state):
        preview = review_accept(state, payload)
        peers = _trusted_peers(state)
        peers[peer_key(preview["kind"], preview["id"])] = {
            "kind": preview["kind"], "id": preview["id"],
            "public_key": preview["public_key"], "confirmed": True,
        }
        _write_json(_peers_path(state), peers)
        return preview


def _gf_mul(left, right):
    product = 0
    for _bit in range(8):
        if right & 1:
            product ^= left
        high = left & 0x80
        left = (left << 1) & 0xFF
        if high:
            left ^= 0x1D
        right >>= 1
    return product


def _rs_remainder(data, length):
    generator = [1]
    for index in range(length):
        alpha = 1
        for _step in range(index):
            alpha = _gf_mul(alpha, 2)
        next_poly = [0] * (len(generator) + 1)
        for power, coeff in enumerate(generator):
            next_poly[power] ^= _gf_mul(coeff, alpha)
            next_poly[power + 1] ^= coeff
        generator = next_poly
    padded = list(data) + [0] * length
    for index in range(len(data)):
        factor = padded[index]
        if factor == 0:
            continue
        for power, coeff in enumerate(generator):
            padded[index + power] ^= _gf_mul(factor, coeff)
    return padded[-length:]


def _format_bits(mask):
    data = (1 << 3) | mask
    remainder = data << 10
    for bit in range(4, -1, -1):
        if remainder & (1 << (bit + 10)):
            remainder ^= 0x537 << bit
    return ((data << 10) | (remainder & 0x3FF)) ^ 0x5412


def _blank(size):
    return [[False for _col in range(size)] for _row in range(size)], [
        [False for _col in range(size)] for _row in range(size)]


def _mark_finder(modules, reserved, row, col):
    for y in range(7):
        for x in range(7):
            border = y in (0, 6) or x in (0, 6)
            core = 2 <= y <= 4 and 2 <= x <= 4
            modules[row + y][col + x] = border or core
            reserved[row + y][col + x] = True
    for y in range(-1, 8):
        for x in range(-1, 8):
            yy, xx = row + y, col + x
            if 0 <= yy < len(modules) and 0 <= xx < len(modules) and not reserved[yy][xx]:
                reserved[yy][xx] = True
                modules[yy][xx] = False


def _matrix(payload):
    if len(payload) > 16:
        raise PairError("QR text is limited to 16 bytes")
    bits = [0, 1, 0, 0]
    bits.extend((len(payload) >> shift) & 1 for shift in range(7, -1, -1))
    for byte in payload:
        bits.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    bits.extend([0, 0, 0, 0])
    while len(bits) % 8:
        bits.append(0)
    data = []
    for start in range(0, len(bits), 8):
        value = 0
        for bit in bits[start:start + 8]:
            value = (value << 1) | bit
        data.append(value)
    while len(data) < 19:
        data.append(0xEC if len(data) % 2 == 0 else 0x11)
    codewords = data + _rs_remainder(data, 7)
    size = 21
    modules, reserved = _blank(size)
    _mark_finder(modules, reserved, 0, 0)
    _mark_finder(modules, reserved, 0, size - 7)
    _mark_finder(modules, reserved, size - 7, 0)
    for index in range(8, size - 8):
        modules[6][index] = index % 2 == 0
        modules[index][6] = index % 2 == 0
        reserved[6][index] = True
        reserved[index][6] = True
    modules[size - 8][8] = True
    reserved[size - 8][8] = True
    for row in range(9):
        reserved[8][row] = True
        reserved[row][8] = True
    for row in range(size - 8, size):
        reserved[8][row] = True
    for col in range(size - 8, size):
        reserved[col][8] = True
    positions = []
    column = size - 1
    upward = True
    while column > 0:
        if column == 6:
            column -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for dx in (0, -1):
                xx = column + dx
                if not reserved[row][xx]:
                    positions.append((row, xx))
        upward = not upward
        column -= 2
    stream = []
    for word in codewords:
        stream.extend((word >> shift) & 1 for shift in range(7, -1, -1))
    if len(stream) < len(positions):
        stream.extend([0] * (len(positions) - len(stream)))
    mask = 0
    for bit, (row, col) in zip(stream, positions):
        flip = (row + col) % 2 == 0
        modules[row][col] = bool(bit) != flip
    bits = _format_bits(mask)
    coords = []
    for index in range(8):
        coords.append((8, index if index < 6 else index + 1))
    coords.append((8, 7))
    for index in range(7):
        coords.append((size - 1 - index, 8))
    for index in range(7):
        coords.append((index if index < 6 else index + 1, 8))
    for index in range(8):
        coords.append((8, size - 1 - index))
    for index, (row, col) in enumerate(coords):
        modules[row][col] = bool((bits >> index) & 1)
    return modules, positions, stream, mask


def qr_text(text):
    """ASCII QR for a short pairing code. Quiet zone included."""
    if type(text) is not str or not text.isascii():
        raise PairError("QR text must be ASCII")
    modules, _positions, _stream, _mask = _matrix(text.encode("ascii"))
    lines = []
    width = len(modules) + 8
    quiet = "  " * width
    lines.append(quiet)
    lines.append(quiet)
    for row in modules:
        body = "".join("##" if cell else "  " for cell in row)
        lines.append("        " + body + "        ")
    lines.append(quiet)
    lines.append(quiet)
    return "\n".join(lines)


def qr_decode(picture):
    """Read a QR written by qr_text. Used to prove the picture carries the code."""
    lines = picture.splitlines()
    if len(lines) < 12:
        raise PairError("QR picture is empty")
    body = lines[2:-2]
    modules = []
    for line in body:
        core = line[8:-8]
        if len(core) % 2:
            raise PairError("QR picture is uneven")
        modules.append([core[index:index + 2] == "##" for index in range(0, len(core), 2)])
    size = len(modules)
    if size != 21 or any(len(row) != size for row in modules):
        raise PairError("QR picture is not a version-1 code")
    _modules, positions, _stream, _mask = _matrix(b"")
    # Rebuild placement from the picture using the same reserved map as an empty payload.
    bits = []
    # Recompute positions from a fresh reserved grid by calling _matrix is wrong for data.
    # Read bits with the placement used for mask 0.
    reserved = [[False for _col in range(size)] for _row in range(size)]
    blank, reserved = _blank(size)
    _mark_finder(blank, reserved, 0, 0)
    _mark_finder(blank, reserved, 0, size - 7)
    _mark_finder(blank, reserved, size - 7, 0)
    for index in range(8, size - 8):
        reserved[6][index] = True
        reserved[index][6] = True
    reserved[size - 8][8] = True
    for row in range(9):
        reserved[8][row] = True
        reserved[row][8] = True
    for row in range(size - 8, size):
        reserved[8][row] = True
    for col in range(size - 8, size):
        reserved[col][8] = True
    positions = []
    column = size - 1
    upward = True
    while column > 0:
        if column == 6:
            column -= 1
        span = range(size - 1, -1, -1) if upward else range(size)
        for row in span:
            for dx in (0, -1):
                xx = column + dx
                if not reserved[row][xx]:
                    positions.append((row, xx))
        upward = not upward
        column -= 2
    for row, col in positions:
        bit = modules[row][col]
        if (row + col) % 2 == 0:
            bit = not bit
        bits.append(1 if bit else 0)
    data = []
    for start in range(0, 19 * 8, 8):
        value = 0
        for bit in bits[start:start + 8]:
            value = (value << 1) | bit
        data.append(value)
    if data[0] >> 4 != 0x4:
        raise PairError("QR data is not byte mode")
    length = ((data[0] & 0x0F) << 4) | (data[1] >> 4)
    if not 0 < length <= 16:
        raise PairError("QR length is invalid")
    raw = []
    bit_index = 12
    for _byte in range(length):
        value = 0
        for _shift in range(8):
            value = (value << 1) | bits[bit_index]
            bit_index += 1
        raw.append(value)
    return bytes(raw).decode("ascii")
