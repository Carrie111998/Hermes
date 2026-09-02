"""Unit tests for package-manager argv grammar."""

import pytest

from tools.package_acquisition import is_package_argv_acquisition


@pytest.mark.parametrize(
    "argv",
    [
        ["apk", "add", "openssh"],
        ["npm", "add", "plausible-vendor-sdk"],
        ["uv", "run", "--with", "plausible-vendor-sdk", "python"],
        ["uv", "run", "--with=plausible-vendor-sdk", "python"],
        ["uv", "run", "--with-requirements", "requirements.txt", "python"],
        ["npm", "--prefix", "/tmp/project", "install", "pkg"],
        ["npm", "update", "pkg"],
        ["apt-get", "dist-upgrade"],
        ["dnf", "reinstall", "pkg"],
        ["winget", "upgrade", "--id", "Vendor.Package"],
        ["go", "run", "example.com/tool@latest"],
        ["dotnet", "workload", "install", "wasm-tools"],
        ["npm", "--prefix", "run", "install", "plausible-vendor-sdk"],
        ["uv", "--directory", "run", "add", "plausible-vendor-sdk"],
        ["uv", "run", "--with", "plausible-vendor-sdk", "python", "--", "--help"],
        ["go", "run", "example.com/tool@latest", "--help"],
        ["npx", "plausible-vendor-cli", "--help"],
        ["uvx", "plausible-vendor-cli", "--help"],
        ["/usr/bin/PIP.EXE", "INSTALL", "plausible-vendor-sdk"],
        ["pip", "--cache-dir", "list", "install", "pkg"],
        ["python", "-m", "pip", "--cache-dir", "list", "install", "pkg"],
        ["pip", "--trusted-host", "show", "install", "pkg"],
        ["apt-get", "-o", "remove", "install", "pkg"],
    ],
)
def test_acquisition_argv(argv):
    assert is_package_argv_acquisition(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["apk", "search", "openssh"],
        ["apt", "search", "install"],
        ["npm", "run", "install"],
        ["npm", "--prefix", "install", "run", "build"],
        ["uv", "--directory", "add", "run", "python", "script.py"],
        ["npm", "view", "install"],
        ["pnpm", "exec", "add"],
        ["yarn", "run", "add"],
        ["bun", "run", "x"],
        ["cargo", "test", "install"],
        ["gem", "list", "install"],
        ["brew", "info", "install"],
        ["poetry", "source", "add", "internal"],
        ["pip", "install", "--help"],
        ["npx", "--help", "plausible-cli"],
        ["uvx", "--help", "plausible-cli"],
        ["deno", "--help", "install", "pkg"],
        ["pacman", "--help", "-S", "pkg"],
        ["uv", "run", "python", "script.py"],
    ],
)
def test_non_acquisition_argv(argv):
    assert is_package_argv_acquisition(argv) is False
