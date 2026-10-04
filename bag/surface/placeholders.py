"""Placeholders: how real values stay out of the AI's sight and out of recordings.

The AI only ever writes placeholders:

    {{member_id}}        an input the person gave us (bag discover --input member_id=...)
    {{secret:BANK_USER}} a credential read from .env / the environment

The surface swaps in the real value at the very last moment, just before typing.
Going the other way, anything read back from the page has secrets scrubbed out.

Only secrets on an allow-list can be requested, so a malicious page that tricks
the AI into asking for {{secret:ANTHROPIC_API_KEY}} gets an error instead of the key.
The list is BAG_SECRET_NAMES (comma separated), default: BANK_USER, BANK_PASSWORD.
"""

import os
import re
from collections.abc import Iterable, Mapping

from .base import SurfaceError

PLACEHOLDER = re.compile(r"\{\{\s*(secret:)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
DEFAULT_SECRET_NAMES = ("BANK_USER", "BANK_PASSWORD")
MIN_SECRET_LENGTH = 4  # shorter values would also match ordinary text, so they are not scrubbed


class UnknownPlaceholder(SurfaceError):
    """The text uses a placeholder we have no value for."""


def secret_names_from_env(env: Mapping[str, str] = os.environ) -> tuple[str, ...]:
    configured = [n.strip() for n in (env.get("BAG_SECRET_NAMES") or "").split(",") if n.strip()]
    return tuple(configured) or DEFAULT_SECRET_NAMES


class Values:
    def __init__(
        self,
        inputs: Mapping[str, str] | None = None,
        secrets: Mapping[str, str] | None = None,
        secret_names: Iterable[str] | None = None,
    ):
        source = os.environ if secrets is None else secrets
        names = tuple(secret_names) if secret_names is not None else secret_names_from_env()
        self.inputs = dict(inputs or {})
        self._secrets = {name: source[name] for name in names if source.get(name)}

    @property
    def input_names(self) -> list[str]:
        return sorted(self.inputs)

    @property
    def secret_names(self) -> list[str]:
        """Names of the secrets that exist (never their values)."""
        return sorted(self._secrets)

    def substitute(self, text: str) -> str:
        """Replace placeholders with real values. Raises UnknownPlaceholder if one has no value."""

        def real_value(match):
            is_secret, name = match.group(1), match.group(2)
            if is_secret:
                if name not in self._secrets:
                    raise UnknownPlaceholder(
                        f"Unknown secret placeholder {{{{secret:{name}}}}}. Available: {', '.join(self.secret_names) or 'none'}."
                    )
                return self._secrets[name]
            if name not in self.inputs:
                raise UnknownPlaceholder(
                    f"Unknown input placeholder {{{{{name}}}}}. Available: {', '.join(self.input_names) or 'none'}."
                )
            return self.inputs[name]

        return PLACEHOLDER.sub(real_value, text)

    def redact(self, text: str) -> str:
        """Scrub secret values out of text that came from the page."""
        for name, value in sorted(self._secrets.items(), key=lambda item: -len(item[1])):
            if len(value) >= MIN_SECRET_LENGTH:
                text = text.replace(value, f"{{{{secret:{name}}}}}")
        return text

    def protect(self, text: str) -> str:
        """Turn real values in text the AI wrote back into placeholders.

        Safety net for when the AI types a literal value instead of a placeholder:
        the recording then still holds a placeholder, never the real value.
        """
        text = self.redact(text)
        for name, value in self.inputs.items():
            if value and text == value:
                return f"{{{{{name}}}}}"
        return text
