"""Tests for the state file, the lock and the stage runner's resume logic."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from wpmigrate.engine import runner as runner_module
from wpmigrate.engine.context import Context, Options, Secrets
from wpmigrate.engine.errors import AlreadyRunning
from wpmigrate.engine.events import EventList, Status
from wpmigrate.engine.prompts import ScriptedPrompter
from wpmigrate.engine.state import RunLock, SitePaths, StateStore, site_slug
from wpmigrate.engine.system import LocalSystem
from wpmigrate.redact import Redactor


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.paths = SitePaths(Path(self.tmp.name) / "site")
        self.paths.create()

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_permissions(self):
        store = StateStore(self.paths)
        store.set("source_url", "https://example.com")
        self.assertEqual(self.paths.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.paths.state_file.stat().st_mode & 0o777, 0o600)

    def test_round_trip_and_stages(self):
        store = StateStore(self.paths)
        store.update({"a": 1})
        store.mark_stage("download", "done")
        again = StateStore(self.paths)
        self.assertEqual(again.get("a"), 1)
        self.assertTrue(again.stage_done("download"))
        again.reset_stage("download")
        self.assertFalse(StateStore(self.paths).stage_done("download"))

    def test_refuses_secrets(self):
        store = StateStore(self.paths)
        with self.assertRaises(ValueError):
            store.set("dest_db", {"name": "x", "password": "hunter2"})
        self.assertNotIn("hunter2", self.paths.state_file.read_text() if self.paths.state_file.exists() else "")

    def test_destructive_steps_recorded_once(self):
        store = StateStore(self.paths)
        self.assertFalse(store.destructive_done("existing_site_removed"))
        store.record_destructive_started("existing_site_removed")
        self.assertTrue(StateStore(self.paths).destructive_started("existing_site_removed"))
        store.record_destructive("existing_site_removed", entries=["wp-admin"])
        again = StateStore(self.paths)
        self.assertTrue(again.destructive_done("existing_site_removed"))
        self.assertFalse(again.destructive_started("existing_site_removed"))

    def test_damaged_state_file(self):
        self.paths.state_file.write_text("{not json")
        with self.assertRaises(Exception) as caught:
            StateStore(self.paths)
        self.assertIn("damaged", str(caught.exception))

    def test_lock_excludes_second_run(self):
        first = RunLock(self.paths)
        first.acquire()
        try:
            with self.assertRaises(AlreadyRunning):
                RunLock(self.paths).acquire()
        finally:
            first.release()
        RunLock(self.paths).acquire()

    def test_site_slug(self):
        self.assertEqual(site_slug("https://Example.com/blog/"), "example.com_blog")
        self.assertEqual(site_slug("https://a.b:8443"), "a.b_8443")


class ResumeTests(unittest.TestCase):
    """The runner skips finished stages and continues after the last one."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.calls = []
        self.failures_left = {"download": 1}

    def tearDown(self):
        self.tmp.cleanup()

    def _context(self, answers):
        system = LocalSystem()
        system.home = lambda: self.home  # type: ignore[method-assign]
        return Context(
            system=system,
            prompter=ScriptedPrompter(answers),
            sink=EventList(),
            options=Options(source_url="https://example.com", admin_user="admin", new_site_url="https://example.com", start_dir=self.home),
            secrets=Secrets("pw", "dbpw"),
            redactor=Redactor(),
        )

    def _fake(self, name, fail_once=False):
        def stage(ctx):
            self.calls.append(name)
            if fail_once and self.failures_left.get(name):
                self.failures_left[name] -= 1
                from wpmigrate.engine.errors import TransferFailed
                raise TransferFailed("boom", "again")
        return stage

    def test_resume_after_failure(self):
        stages = [(name, name, self._fake(name, fail_once=(name == "download"))) for name in runner_module.STAGE_NAMES]
        stages[0] = ("collect", "collect", runner_module.stage_collect)
        with mock.patch.object(runner_module, "STAGES", stages), mock.patch.object(runner_module, "stage_collect", runner_module.stage_collect):
            ctx = self._context({})
            code = runner_module.Engine(ctx).run()
            self.assertEqual(code, 12)
            self.assertEqual(self.calls, ["requirements", "login", "plugin", "authorize", "download"])
            self.calls.clear()
            ctx = self._context({})
            code = runner_module.Engine(ctx).run()
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["download", "destination", "deploy", "cleanup_source", "summary"])
            failed = [event for event in ctx.sink.events if event.status == Status.FAILED]
            self.assertEqual(failed, [])

    def test_state_has_no_password(self):
        stages = [(name, name, self._fake(name)) for name in runner_module.STAGE_NAMES]
        stages[0] = ("collect", "collect", runner_module.stage_collect)
        with mock.patch.object(runner_module, "STAGES", stages):
            ctx = self._context({})
            runner_module.Engine(ctx).run()
            text = ctx.p.state_file.read_text()
            self.assertNotIn('"pw"', text)
            self.assertNotIn("dbpw", text)
            self.assertEqual(json.loads(text)["data"]["admin_user"], "admin")


if __name__ == "__main__":
    unittest.main()
