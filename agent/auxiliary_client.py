"""Public auxiliary-client module; implementation is sharded under agent/."""

from agent.auxiliary_client_runtime import install as _install

_install(globals())
del _install
