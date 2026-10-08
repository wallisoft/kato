"""
Juska: Pro licences.

A licence key is a short signed statement, "pro | how many computers | id | date", issued by tiny-web.uk when
someone buys Juska Pro. It is checked here with the public half of the signing key, so it needs no account and
no internet, and nobody can make one without the private half, which never leaves the server.

    ASP1-<payload, base64url>.<signature, base64url>
"""
import base64
import re

PUBLIC_KEY = "a10f27df2f62cbb78f726b36c3b858e0ca7ae3d2304d03a9168f77290c10f53c"
FREE_COMPUTERS = 3
BUY_URL = "https://juska.uk/#pricing"


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def tidy(key):
    """A key as pasted from an email: wrapped over lines, with spaces, perhaps in quotes or angle brackets."""
    return re.sub(r"[\s\"'<>]+", "", str(key or ""))


def read_licence(key, public_key=PUBLIC_KEY):
    """What a genuine key says, {'computers': 15, 'id': ..., 'issued': ...}, or None if it isn't genuine."""
    key = tidy(key)
    if not key.startswith("ASP1-") or key.count(".") != 1:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        payload_text, signature_text = key[5:].split(".")
        payload, signature = _unb64(payload_text), _unb64(signature_text)
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(signature, payload)
        except InvalidSignature:
            return None
        fields = payload.decode("utf-8").split("|")
        tier, computers, licence_id, issued = fields[:4]          # later keys may carry more after these four
        if tier != "pro" or int(computers) < 1:
            return None
        return {"computers": int(computers), "id": licence_id, "issued": issued, "key": key, "extra": fields[4:]}
    except Exception:
        return None


def best(keys):
    """The most generous genuine licence among some keys, or None."""
    found = [info for info in (read_licence(k) for k in keys if k) if info]
    return max(found, key=lambda i: i["computers"]) if found else None
