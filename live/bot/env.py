"""Compatibilidad para importar el cargador de perfiles desde `bot.env`."""

try:
    from ..env_profiles import _load_simple_env, load_direct_profile
except ImportError:
    from env_profiles import _load_simple_env, load_direct_profile

__all__ = ["_load_simple_env", "load_direct_profile"]
