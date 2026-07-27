"""PEP 517 compatibility entry point for the independent Charterforge package.

Packaging is intentionally enabled for this fork. The upstream Hermes
distribution guard rejected wheels and sdists outside a Nix build, which made
the independently branded runtime impossible to install from an artifact.
Project metadata, package discovery, entry points, and package data live in
``pyproject.toml``; this file only delegates to setuptools for legacy tools.
"""

from setuptools import setup


setup()
