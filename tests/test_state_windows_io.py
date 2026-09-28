"""Regression tests for state reads and atomic replacement on Windows."""

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from state_manager import StateManager


class StateWindowsIOTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "bot_state.json")

    def test_transient_access_denied_retries_and_cleans_temp_file(self):
        manager = StateManager(self.path)
        manager.state["bot_status"] = "testing"
        original_replace = os.replace
        attempts = []

        def replace_after_contention(source, target):
            attempts.append((source, target))
            if len(attempts) < 3:
                raise PermissionError(13, "Access is denied", target)
            return original_replace(source, target)

        with patch("state_manager.os.replace", side_effect=replace_after_contention):
            with patch("state_manager.time.sleep") as sleep:
                manager.save(strict=True)

        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["bot_status"], "testing")
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.directory.name)))

    def test_persistent_access_denied_still_raises_in_strict_mode(self):
        manager = StateManager(self.path)
        with patch("state_manager.os.replace", side_effect=PermissionError(13, "Access is denied")):
            with patch("state_manager.time.sleep"):
                with self.assertRaises(PermissionError):
                    manager.save(strict=True)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.directory.name)))

    def test_reader_and_writer_share_in_process_file_lock(self):
        writer = StateManager(self.path)
        writer.state["bot_status"] = "ready"
        writer.save(strict=True)
        reader = StateManager(self.path)
        read_started = threading.Event()
        allow_read_to_finish = threading.Event()
        writer_finished = threading.Event()

        original_load = json.load

        def slow_load(handle):
            read_started.set()
            allow_read_to_finish.wait(timeout=3)
            return original_load(handle)

        def read_state():
            reader._load_state(log=False)

        def write_state():
            writer.state["bot_status"] = "updated"
            writer.save(strict=True)
            writer_finished.set()

        with patch("state_manager.json.load", side_effect=slow_load):
            read_thread = threading.Thread(target=read_state)
            read_thread.start()
            self.assertTrue(read_started.wait(timeout=3))
            write_thread = threading.Thread(target=write_state)
            write_thread.start()
            self.assertFalse(writer_finished.wait(timeout=0.05))
            allow_read_to_finish.set()
            read_thread.join(timeout=3)
            write_thread.join(timeout=3)
        self.assertTrue(writer_finished.is_set())


if __name__ == "__main__":
    unittest.main()
