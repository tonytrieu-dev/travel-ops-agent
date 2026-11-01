"""The shared enforcement setting for identity, ownership, connectors, and evidence."""

from app import config


def enforcement_active() -> bool:
    return config.get_settings().zero_trust_enforced
