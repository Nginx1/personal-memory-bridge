from dataclasses import dataclass, field
import hashlib
import json
import re


@dataclass(frozen=True)
class Document:
    source: str
    key: str
    content: str
    scope: str = "personal"
    metadata: dict = field(default_factory=dict)

    @property
    def id(self):
        # Source names identify a logical collection, independent of mount path.
        return hashlib.sha256((self.source + "\0" + self.key).encode()).hexdigest()

    @property
    def digest(self):
        payload = json.dumps([self.content, self.scope, self.metadata],
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Redactor:
    """Best effort credential filtering. Explicit source allowlists remain required."""
    patterns = [
        (r"(?s)-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----.*?-----END (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
        (r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
        (r"(?im)((?:api[_-]?key|password|passwd|secret|token)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"((?:密码|密钥)(?:是|为|：|:|\s)+(?:通用的)?)\S+", r"\1[REDACTED]"),
    ]

    def __init__(self, secrets=()):
        self.secrets = tuple(x for x in secrets if x)

    def __call__(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern, replacement in self.patterns:
            text = re.sub(pattern, replacement, text)
        return text
