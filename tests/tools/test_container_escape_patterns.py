"""Tests for the container-escape / supervisor-primitive DANGEROUS_PATTERNS.

Covers the three rule groups added for containerized gateway deployments
that mount docker.sock read-write:

- ``docker exec|run|cp|create|commit|start`` — cross-container escape verbs
  (root in the target container); read-only verbs stay auto-approved.
- ``s6-svc`` down/signal flags and ``s6-svscanctl`` — reaching past the
  gated wrapper spellings (``hermes gateway stop``, ``docker compose
  restart`` …) straight to the s6 supervision primitives.
- bare ``kill 1`` — signaling the container's init/supervision tree; the
  HARDLINE ``kill -1`` rule requires a leading dash and does not match it.
"""

from tools.approval import detect_dangerous_command


ESCAPE_KEY = "cross-container / container escape"


class TestDockerEscapeVerbs:
    def test_docker_exec_detected(self):
        is_dangerous, _key, description = detect_dangerous_command(
            "docker exec other-container sh")
        assert is_dangerous is True
        assert ESCAPE_KEY in description

    def test_docker_run_with_root_mount_detected(self):
        is_dangerous, _key, description = detect_dangerous_command(
            "docker run -v /:/host -it alpine sh")
        assert is_dangerous is True
        assert ESCAPE_KEY in description

    def test_docker_cp_detected(self):
        is_dangerous, _key, _description = detect_dangerous_command(
            "docker cp web:/etc/shadow /tmp/shadow")
        assert is_dangerous is True

    def test_docker_create_commit_start_detected(self):
        for cmd in (
            "docker create --name x alpine",
            "docker commit web evil:latest",
            "docker start web",
        ):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is True, cmd

    def test_compose_spellings_detected(self):
        # `docker compose exec` / legacy `docker-compose exec` reach the same
        # root shell in another container as the plain `docker exec`.
        for cmd in (
            "docker compose exec forge sh",
            "docker-compose exec forge sh",
            "docker compose run --rm x",
            "docker-compose cp forge:/etc/shadow /tmp/shadow",
        ):
            is_dangerous, _key, description = detect_dangerous_command(cmd)
            assert is_dangerous is True, cmd
            assert ESCAPE_KEY in description, cmd

    def test_global_flags_before_verb_detected(self):
        # Global flags between `docker`/`compose` and the verb must not let
        # the verb slip past (same shape the lifecycle rules tolerate).
        for cmd in (
            "docker --log-level debug exec forge sh",
            "docker compose -f prod.yml run --rm x",
            "docker compose --project-name p -f prod.yml exec forge sh",
            "docker --config /tmp/cfg cp forge:/etc/shadow /tmp/shadow",
        ):
            is_dangerous, _key, description = detect_dangerous_command(cmd)
            assert is_dangerous is True, cmd
            assert ESCAPE_KEY in description, cmd

    def test_readonly_docker_verbs_not_detected(self):
        for cmd in ("docker ps", "docker logs web", "docker inspect web",
                    "docker images", "docker logs x"):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is False, cmd

    def test_readonly_compose_verbs_not_detected(self):
        for cmd in ("docker compose ps", "docker compose logs",
                    "docker-compose ps", "docker compose -f prod.yml ps",
                    "docker --log-level debug ps"):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is False, cmd


class TestS6Primitives:
    def test_s6_svc_down_flag_detected(self):
        is_dangerous, _key, description = detect_dangerous_command(
            "s6-svc -d /run/service/main-hermes")
        assert is_dangerous is True
        assert "s6-svc" in description

    def test_s6_svc_once_down_flag_detected(self):
        # `-o` (once-down: bring up, don't restart when it exits) is a down
        # flag too and must not slip through the class.
        is_dangerous, _key, description = detect_dangerous_command(
            "s6-svc -o /run/service/main-hermes")
        assert is_dangerous is True
        assert "s6-svc" in description

    def test_s6_svc_signal_flags_detected(self):
        for flag in ("-o", "-t", "-k", "-h", "-i", "-q", "-p", "-r"):
            is_dangerous, _key, _description = detect_dangerous_command(
                f"s6-svc {flag} /run/service/main-hermes")
            assert is_dangerous is True, flag

    def test_s6_svc_via_docker_exec_wrapper_detected(self):
        # Detection runs over the full command string, so the wrapper
        # spelling is covered without its own rule.
        is_dangerous, _key, _description = detect_dangerous_command(
            "docker exec hermes s6-svc -d /run/service/main-hermes")
        assert is_dangerous is True

    def test_s6_svscanctl_detected(self):
        is_dangerous, _key, description = detect_dangerous_command(
            "s6-svscanctl -h /run/service")
        assert is_dangerous is True
        assert "s6-svscanctl" in description

    def test_readonly_s6_siblings_not_detected(self):
        for cmd in ("s6-svstat /run/service/main-hermes",
                    "s6-svwait -u /run/service/main-hermes",
                    "s6-svstat /run/service/x",
                    "s6-svwait -u /run/service/x",
                    # -d/-o only count as s6-svc flags, not on the siblings
                    "s6-svwait -d /run/service/x"):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is False, cmd


class TestKillPidOne:
    def test_bare_kill_1_detected(self):
        is_dangerous, _key, description = detect_dangerous_command("kill 1")
        assert is_dangerous is True
        assert "PID 1" in description

    def test_kill_with_signal_flag_then_1_detected(self):
        for cmd in ("kill -9 1", "kill -TERM 1", "kill -s KILL 1"):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is True, cmd

    def test_kill_of_ordinary_pids_not_detected(self):
        for cmd in ("kill 1234", "kill -9 4321", "kill 12", "killall foo"):
            is_dangerous, _key, _description = detect_dangerous_command(cmd)
            assert is_dangerous is False, cmd
