"""Optional, strict configuration-file loading and data-directory resolution.

Step 02 accepts **one** optional configuration file, selected explicitly with
``--config PATH``. It is a versioned JSON document with a deliberately tiny
schema:

.. code-block:: json

    {"schema_version": 1, "data_dir": "/absolute/or/relative/path"}

Design rules (``docs/implementation/02-project-foundation.md`` and
``docs/decisions/002-step01-contracts-and-policies.md``):

* **Fail closed and strict.** Any unknown field is rejected — including any
  credential-looking field (``api_key``, ``token``, ``password``, ...). The CLI
  never ingests a provider secret, model setting, or budget/execution setting
  from this file.
* **No value echo.** A rejected configuration never prints a field *value*;
  only the offending field *names* and the structural schema version are
  reported, so a stray secret cannot be leaked into logs.
* **Versioned.** A ``schema_version`` newer than this application supports is
  refused with an actionable message rather than being read or downgraded.
* **Precedence.** An explicit ``--data-dir`` overrides the file, which overrides
  the XDG default (:func:`review_article_agent.storage.paths.default_data_dir`).

The data directory itself is made owner-private (``0o700``) by the registry when
it is first used; this module only resolves *which* directory that is.
"""

from __future__ import annotations

import json
import os
from typing import Mapping

from ..domain.errors import ReviewAgentError
from ..storage import paths

#: The only supported configuration schema version.
CONFIG_SCHEMA_VERSION = 1

#: The exact set of permitted top-level configuration fields.
ALLOWED_CONFIG_FIELDS = frozenset({"schema_version", "data_dir"})

#: Upper bound on a configuration file we will even attempt to parse.
MAX_CONFIG_BYTES = 1 << 20  # 1 MiB


class ConfigError(ReviewAgentError):
    """The optional configuration file is missing, malformed, or unsupported.

    Messages name the offending field(s) and the structural schema version but
    never print a field value, so a mis-placed secret is not echoed.
    """


def load_config_file(path: str | os.PathLike[str]) -> dict[str, object]:
    """Load and strictly validate a configuration file.

    Returns a normalized mapping with exactly ``schema_version`` and
    ``data_dir`` keys (``data_dir`` may be ``None``). Raises
    :class:`ConfigError` with an actionable, value-free message on any problem.
    """
    try:
        with open(os.fspath(path), "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ConfigError(
            f"the configuration file could not be read ({reason}); check the "
            f"--config path and its permissions."
        ) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(
            f"the configuration file is larger than the {MAX_CONFIG_BYTES}-byte "
            f"limit; refusing to parse it."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "the configuration file is not valid UTF-8; it must be a UTF-8 JSON "
            "document."
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "the configuration file is not valid JSON; fix or remove it and retry."
        ) from exc
    return validate_config(data)


def validate_config(data: object) -> dict[str, object]:
    """Validate an already-parsed configuration object.

    Separate from :func:`load_config_file` so tests (and future callers) can
    validate a structure without touching the filesystem.
    """
    if not isinstance(data, dict):
        raise ConfigError("the configuration root must be a JSON object.")
    for key in data:
        if not isinstance(key, str):
            raise ConfigError("configuration field names must be strings.")

    unknown = sorted(key for key in data if key not in ALLOWED_CONFIG_FIELDS)
    if unknown:
        raise ConfigError(
            "unknown configuration field(s): "
            + ", ".join(unknown)
            + "; the only permitted fields are "
            + ", ".join(sorted(ALLOWED_CONFIG_FIELDS))
            + ". Credentials, provider, and model/budget settings are not read "
            "from this file."
        )

    if "schema_version" not in data:
        raise ConfigError(
            "the configuration is missing the required 'schema_version' field "
            f"(expected {CONFIG_SCHEMA_VERSION})."
        )
    version = data["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("configuration field 'schema_version' must be an integer.")
    if version > CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            f"configuration schema_version {version} is newer than this "
            f"application supports (max {CONFIG_SCHEMA_VERSION}); upgrade "
            f"review_article_agent instead of reading it with an older version."
        )
    if version < 1:
        raise ConfigError("configuration field 'schema_version' must be >= 1.")

    data_dir: str | None = None
    if "data_dir" in data:
        value = data["data_dir"]
        if not isinstance(value, str) or value == "" or value != value.strip():
            raise ConfigError(
                "configuration field 'data_dir' must be a non-empty string path "
                "with no leading/trailing whitespace."
            )
        data_dir = value

    return {"schema_version": version, "data_dir": data_dir}


def resolve_data_dir(
    explicit: str | None,
    config: Mapping[str, object] | None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the application data directory with documented precedence.

    ``--data-dir`` (``explicit``) wins over the configuration file's
    ``data_dir``, which wins over the XDG-default data directory. The result is
    canonicalized with :func:`review_article_agent.storage.paths.resolve_root`.
    """
    if explicit is not None and explicit != "":
        return paths.resolve_root(explicit)
    if config is not None:
        from_config = config.get("data_dir")
        if isinstance(from_config, str) and from_config != "":
            return paths.resolve_root(from_config)
    return paths.default_data_dir(None if env is None else dict(env))


__all__ = [
    "ALLOWED_CONFIG_FIELDS",
    "CONFIG_SCHEMA_VERSION",
    "MAX_CONFIG_BYTES",
    "ConfigError",
    "load_config_file",
    "resolve_data_dir",
    "validate_config",
]
