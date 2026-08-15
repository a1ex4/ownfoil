"""Obfuscation of identifying data, applied before a capture reaches disk.

Captures are committed to the repo, and real clients announce a device id (Uid), values
derived from it (Hauth, Uauth), the operator's LAN address and whatever credentials were
typed. None of that is needed to replay a request, so it is replaced here rather than
scrubbed later - the raw values are never written anywhere.

Replacements are stable across runs through a map kept in the capture workdir, which is
gitignored. Two captures of the same device therefore agree, which matters because Hauth
continuity between requests is itself under test.
"""
import base64
import hashlib
import json
import os
import re

# Device id and the values derived from it. Hex strings; length is preserved because the
# clients' own format is part of what a capture documents.
HEX_HEADERS = {"uid", "hauth", "uauth"}
HOST_HEADERS = {"host", "x-forwarded-host"}
IP_HEADERS = {"x-forwarded-for", "x-real-ip"}
DROP_HEADERS = {"cookie", "set-cookie"}

REDACTED_USER = "redacted-user"
REDACTED_PASSWORD = "redacted-password"

# RFC 5737 / RFC 2606 ranges, so a capture can never name a real host.
FAKE_IP = "192.0.2.{}"
FAKE_HOST = "shop{}.example.net"


class Sanitizer:
    """Deterministic pseudonyms for the identifying parts of a captured exchange."""

    def __init__(self, path=None, users=None, passwords=None):
        self.path = path
        self.users = set(users or [])
        self.passwords = set(passwords or [])
        self._map = {}
        if path and os.path.exists(path):
            with open(path) as f:
                self._map = json.load(f)

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._map, f, indent=2, sort_keys=True)

    # ==================== Values ====================

    def _pseudonym(self, kind, value, make):
        key = f"{kind}:{value}"
        if key not in self._map:
            index = sum(1 for k in self._map if k.startswith(f"{kind}:")) + 1
            self._map[key] = make(value, index)
        return self._map[key]

    def _fake_hex(self, value, index):
        """Hex of the same length, so a client's id format survives the swap."""
        digest = hashlib.sha256(f"{index}:{value}".encode()).hexdigest()
        out = (digest * (len(value) // len(digest) + 1))[:len(value)]
        return out.upper() if value.isupper() else out

    def address(self, ip):
        if not ip:
            return ip
        return self._pseudonym("ip", ip, lambda v, i: FAKE_IP.format(9 + i))

    def host(self, value):
        """Map a host, keeping the port and whether it was an address or a name."""
        if not value:
            return value
        name, _, port = value.partition(":")

        def make(v, i):
            base = FAKE_IP.format(9 + i) if _looks_like_ip(name) else FAKE_HOST.format(i)
            return f"{base}:{port}" if port else base

        return self._pseudonym("host", value, make)

    def authorization(self, value):
        """Canonicalize Basic credentials to the fixture accounts; redact anything else."""
        scheme, _, payload = value.partition(" ")
        if scheme.lower() != "basic":
            return f"{scheme} {REDACTED_PASSWORD}"
        try:
            user, _, password = base64.b64decode(payload).decode("utf-8", "replace").partition(":")
        except Exception:
            return f"{scheme} {REDACTED_PASSWORD}"
        if user not in self.users:
            user = REDACTED_USER
        if password not in self.passwords:
            password = REDACTED_PASSWORD
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    # ==================== Exchanges ====================

    def header(self, name, value):
        """Return the value to record, or None to drop the header entirely."""
        key = name.lower()
        if key in DROP_HEADERS:
            return None
        if key in HEX_HEADERS:
            return self._pseudonym("hex", value, self._fake_hex)
        if key in HOST_HEADERS:
            return self.host(value)
        if key in IP_HEADERS:
            return ", ".join(self.address(ip.strip()) for ip in value.split(","))
        if key == "authorization":
            return self.authorization(value)
        return value

    def headers(self, items):
        """Sanitize an ordered header list, preserving order and duplicates."""
        out = []
        for name, value in items:
            value = self.header(name, value)
            if value is not None:
                out.append([name, value])
        return out

    def text(self, value):
        """Replace any already-mapped original that leaked into a response body."""
        if not value:
            return value
        for key, replacement in self._map.items():
            original = key.split(":", 1)[1]
            if len(original) > 3 and original in value:
                value = value.replace(original, replacement)
        return value


def _looks_like_ip(value):
    return bool(re.fullmatch(r"[0-9.]+|\[[0-9a-fA-F:]+\]", value))
