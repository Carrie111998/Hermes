"""The ancestor exclusion in ``_scan_gateway_pids`` must not hide a real gateway.

``hermes update --gateway`` is spawned BY the gateway when ``/update`` is issued
from a messaging platform, so the gateway sits in the updater's own ancestor
chain. The blanket ancestor exclusion (added for #13242, to stop ``hermes
gateway status`` counting the CLI that invoked it) therefore hid the gateway
from the update pause machinery: nothing was paused, the update mutated the venv
while the gateway still held ``.pyd`` files, and the venv-holder guard aborted
with "Other Hermes processes are running from this install's venv" (#87594).

The exclusion is now gated on the command line: an ancestor that looks like a
gateway runtime stays visible, everything else is still suppressed.

The Windows arm is exercised here because that is where the reported failure
lives (``.pyd`` locking is what makes the pause mandatory), and it is stubbed
end to end so these run on any host.
"""

from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.gateway as gateway_mod

_GATEWAY_CMD = "C:/h/venv/Scripts/pythonw.exe -m hermes_cli.main gateway run --replace"
_UPDATER_CMD = "C:/h/venv/Scripts/python.exe -m hermes_cli.main update --yes --gateway"
_STATUS_CMD = "C:/h/venv/Scripts/python.exe -m hermes_cli.main gateway status"

GATEWAY_PID = 14112
UPDATER_PID = 22001


def _wmic_listing(entries: dict[int, str]) -> str:
    """Render ``wmic process get ProcessId,CommandLine /FORMAT:LIST`` output."""
    blocks = [f"CommandLine={cmd}\nProcessId={pid}\n" for pid, cmd in entries.items()]

    return "\n".join(blocks)


def _scan(
    entries: dict[int, str], ancestors: set[int], exclude: set[int] | None = None
):
    """Run the Windows scan arm against a stubbed process table."""
    result = MagicMock(returncode=0, stdout=_wmic_listing(entries))

    with (
        patch("hermes_cli.gateway.is_windows", return_value=True),
        patch("hermes_cli.gateway.shutil.which", return_value="C:/Windows/wmic.exe"),
        patch("hermes_cli.gateway.subprocess.run", return_value=result),
        patch("hermes_cli.gateway._get_ancestor_pids", return_value=ancestors),
        # Stub-collapsing is a separate Windows concern (venv launcher pairs)
        # and would need a live process table; identity keeps it out of the way.
        patch(
            "hermes_cli.gateway._filter_venv_launcher_stubs", side_effect=lambda p: p
        ),
    ):
        return gateway_mod._scan_gateway_pids(exclude or set(), all_profiles=True)


class TestAncestorGatewayStaysVisible:
    def test_gateway_that_spawned_us_is_reported(self):
        """The #87594 failure: updater spawned by the gateway saw no gateway."""
        pids = _scan(
            {GATEWAY_PID: _GATEWAY_CMD, UPDATER_PID: _UPDATER_CMD},
            ancestors={UPDATER_PID, GATEWAY_PID, 4},
        )

        assert GATEWAY_PID in pids, (
            "a real `gateway run` in our ancestor chain is the process the "
            "update pause path exists to find"
        )

    def test_updater_ancestor_is_not_reported(self):
        """The updater is in its own ancestor chain and is not a gateway."""
        pids = _scan(
            {GATEWAY_PID: _GATEWAY_CMD, UPDATER_PID: _UPDATER_CMD},
            ancestors={UPDATER_PID, GATEWAY_PID, 4},
        )

        assert UPDATER_PID not in pids

    def test_non_gateway_ancestor_is_still_excluded(self):
        """#13242 must keep holding: the invoking CLI is not a gateway."""
        pids = _scan({UPDATER_PID: _UPDATER_CMD}, ancestors={UPDATER_PID})

        assert pids == []

    def test_gateway_status_ancestor_is_still_excluded(self):
        """`gateway status` is the original #13242 case, verbatim."""
        pids = _scan({4242: _STATUS_CMD}, ancestors={4242})

        assert pids == []

    def test_caller_supplied_exclusions_stay_unconditional(self):
        """``exclude_pids`` belongs to the caller and outranks the matcher."""
        pids = _scan(
            {GATEWAY_PID: _GATEWAY_CMD},
            ancestors=set(),
            exclude={GATEWAY_PID},
        )

        assert pids == []

    def test_unrelated_gateway_is_unaffected(self):
        """A gateway that is not an ancestor was never in question."""
        pids = _scan({GATEWAY_PID: _GATEWAY_CMD}, ancestors={UPDATER_PID})

        assert pids == [GATEWAY_PID]


@pytest.mark.linux_only
class TestAncestorGatewayViaProc:
    """Same contract on the /proc arm, which is what Linux hosts take."""

    def _proc_scan(self, entries: dict[int, str], ancestors: set[int]):
        def _isdir(path):
            return str(path) == "/proc"

        def _listdir(path):
            if str(path) == "/proc":
                return [str(pid) for pid in entries]
            raise FileNotFoundError(path)

        def _open(path, mode="r", **kwargs):
            path_str = str(path)
            if "/cmdline" not in path_str:
                raise FileNotFoundError(path)
            pid = int(path_str.split("/proc/")[1].split("/")[0])
            handle = MagicMock()
            handle.read.return_value = (
                entries.get(pid, "").encode("utf-8").replace(b" ", b"\x00")
            )
            handle.__enter__ = lambda s: s
            handle.__exit__ = MagicMock(return_value=False)

            return handle

        with (
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=ancestors),
            patch("subprocess.run"),
        ):
            return gateway_mod._scan_gateway_pids(set(), all_profiles=True)

    def test_gateway_that_spawned_us_is_reported(self):
        pids = self._proc_scan(
            {
                GATEWAY_PID: "python -m hermes_cli.main gateway run",
                UPDATER_PID: "python -m hermes_cli.main update --yes --gateway",
            },
            ancestors={UPDATER_PID, GATEWAY_PID, 1},
        )

        assert GATEWAY_PID in pids
        assert UPDATER_PID not in pids

    def test_non_gateway_ancestor_is_still_excluded(self):
        pids = self._proc_scan(
            {UPDATER_PID: "python -m hermes_cli.main update --yes --gateway"},
            ancestors={UPDATER_PID},
        )

        assert pids == []
