import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "blender_hackatime"))

import cli
import core
import fake_hackatime

REAL_CLI = os.environ.get("WAKATIME_CLI") or cli.find_cli()


@unittest.skipUnless(REAL_CLI, "needs a wakatime-cli in ~/.wakatime or WAKATIME_CLI")
class SenderTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="bh-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.log = os.path.join(self.home, "heartbeats.jsonl")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fake_hackatime.make_handler(self.log))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        previous = os.environ.get("WAKATIME_HOME")
        os.environ["WAKATIME_HOME"] = self.home
        self.addCleanup(lambda: os.environ.pop("WAKATIME_HOME") if previous is None else os.environ.__setitem__("WAKATIME_HOME", previous))
        os.makedirs(cli.resource_dir())
        shutil.copy2(REAL_CLI, os.path.join(cli.resource_dir(), cli.build_name() + cli.exe_suffix()))
        cli.write_config({
            "api_url": f"http://127.0.0.1:{self.server.server_port}/api/hackatime/v1",
            "api_key": "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b",
            "heartbeat_rate_limit_seconds": "0", "sync_ai_disabled": "true",
        })
        self.blend = os.path.join(self.home, "rocket", "ship.blend")
        os.makedirs(os.path.dirname(self.blend))
        open(self.blend, "wb").close()
        self.errors = []
        self.sender = cli.Sender("blender/0.0.0 blender-hackatime/test", log=self.errors.append)

    def received(self, count, timeout=60):
        end = time.time() + timeout
        entries = []
        while time.time() < end:
            if os.path.exists(self.log):
                with open(self.log, encoding="utf-8") as log:
                    entries = [json.loads(line)["heartbeat"] for line in log if line.strip()]
            if len(entries) >= count:
                break
            time.sleep(0.2)
        return entries

    def heartbeat(self, x, blend, when):
        heartbeat = core.resolve(core.Heartbeat("building", core.BLEND_LANGUAGE, x, 5, 0), blend)
        heartbeat["time"] = when
        return heartbeat

    def test_batches_reach_the_server(self):
        now = time.time()
        self.sender.submit([self.heartbeat(1, self.blend, now - 30), self.heartbeat(2, self.blend, now - 20)])
        self.sender.submit([self.heartbeat(3, "", now - 10)])
        entries = self.received(3)
        self.sender.stop()
        self.assertEqual(self.errors, [])
        self.assertEqual(sorted(h["lineno"] for h in entries), [1, 2, 3])
        by_line = {h["lineno"]: h for h in entries}
        self.assertEqual(by_line[1]["project"], "rocket")
        self.assertEqual(by_line[1]["lines"], 0)
        self.assertIs(by_line[1]["is_write"], False)
        self.assertEqual(by_line[1]["category"], "building")
        self.assertEqual(os.path.normpath(by_line[3]["entity"]), core.UNSAVED_BLEND)
        self.assertFalse(by_line[3].get("project"))

    def test_stop_sends_what_is_left(self):
        now = time.time()
        self.sender.queue.put([self.heartbeat(8, "", now - 5), self.heartbeat(9, "", now - 4)])
        self.sender.stop()
        entries = self.received(2)
        self.assertEqual(self.errors, [])
        self.assertEqual(sorted(h["lineno"] for h in entries), [8, 9])
        self.assertTrue(all(os.path.normpath(h["entity"]) == core.UNSAVED_BLEND for h in entries))


if __name__ == "__main__":
    unittest.main()
