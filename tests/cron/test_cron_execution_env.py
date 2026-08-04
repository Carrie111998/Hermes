"""Execution identity at the cron pre-run script boundary."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cron import scheduler


class CronScriptExecutionIdentityTests(unittest.TestCase):
    def _script_home(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "show_env.py").write_text(
            "import json, os\n"
            "print(json.dumps({\n"
            "  'cron': os.environ.get('HERMES_CRON_SESSION'),\n"
            "  'execution': os.environ.get('HERMES_CRON_EXECUTION_ID'),\n"
            "}))\n",
            encoding="utf-8",
        )
        return tmp, root

    def test_exact_execution_identity_reaches_script(self):
        tmp, root = self._script_home()
        self.addCleanup(tmp.cleanup)

        with patch.object(scheduler, "_get_hermes_home", return_value=root):
            ok, output = scheduler._run_job_script(
                "show_env.py", execution_id="exec-123"
            )

        self.assertTrue(ok)
        self.assertEqual(
            json.loads(output),
            {"cron": "1", "execution": "exec-123"},
        )

    def test_direct_script_run_does_not_inherit_stale_execution_identity(self):
        tmp, root = self._script_home()
        self.addCleanup(tmp.cleanup)

        with patch.object(scheduler, "_get_hermes_home", return_value=root):
            with patch.dict(
                os.environ,
                {"HERMES_CRON_EXECUTION_ID": "stale-exec"},
                clear=False,
            ):
                ok, output = scheduler._run_job_script("show_env.py")

        self.assertTrue(ok)
        self.assertIsNone(json.loads(output)["execution"])

    def test_run_job_forwards_execution_identity_to_script_runner(self):
        job = {
            "id": "job-123",
            "name": "test",
            "script": "show_env.py",
            "no_agent": True,
            "deliver": "local",
            "execution_id": "exec-123",
        }
        with patch.object(
            scheduler,
            "_run_job_script_with_claim_heartbeat",
            return_value=(True, "report"),
        ) as run_script:
            success, _doc, final_response, error = scheduler.run_job(job)

        self.assertTrue(success)
        self.assertEqual(final_response, "report")
        self.assertIsNone(error)
        self.assertEqual(run_script.call_args.kwargs["execution_id"], "exec-123")

    def test_run_one_job_forwards_ledger_execution_identity(self):
        job = {
            "id": "job-123",
            "name": "test",
            "execution_id": "exec-123",
            "deliver": "local",
        }
        with patch.object(scheduler, "claim_dispatch", return_value=True), \
             patch.object(scheduler, "mark_execution_running"), \
             patch.object(
                 scheduler,
                 "run_job",
                 return_value=(True, "doc", "report", None),
             ) as run_job, \
             patch.object(scheduler, "save_job_output", return_value=Path("out")), \
             patch.object(scheduler, "_is_interrupted", return_value=False), \
             patch.object(scheduler, "_deliver_result", return_value=None), \
             patch.object(scheduler, "_consume_interrupted_flag", return_value=False), \
             patch.object(scheduler, "mark_job_run"), \
             patch.object(scheduler, "finish_execution"), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value={}), \
             patch("agent.secret_scope.set_secret_scope", return_value=object()), \
             patch("agent.secret_scope.reset_secret_scope"):
            self.assertTrue(scheduler.run_one_job(job))

        self.assertEqual(run_job.call_args.args[0]["execution_id"], "exec-123")


if __name__ == "__main__":
    unittest.main()
