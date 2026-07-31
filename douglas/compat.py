"""Douglas <-> Hermes compatibility helpers.

The actual DOUGLAS_*/HERMES_* environment normalization and home-directory
resolution chain lives in ``hermes_bootstrap.py`` at the repo root, not
here — it must run before any other Hermes module is imported (see that
module's docstring and douglas/README.md, section "Cadena canonica de
resolucion Douglas/Hermes"), which ruled out this module as the
implementation site: the core cannot depend on douglas/ being on
sys.path (see the Parte 1 analysis in the Fase 1 Paso 2 investigation —
Option C, rejected).

This module re-exports the resolved values for convenience, for future
douglas/ product code (plugins, skills, billing) that wants the current
home directory without reaching into hermes_constants directly.
"""

from __future__ import annotations

from hermes_constants import get_hermes_home, display_hermes_home  # noqa: F401
