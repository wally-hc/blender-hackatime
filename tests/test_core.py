import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "blender_hackatime"))

import cli
import core


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def scene(x=10, y=20, category="building"):
    return core.Heartbeat(category, core.BLEND_LANGUAGE, x, y, 0)


def script(line=1, col=1, lines=5, path=None, inner="script.py"):
    return core.Heartbeat("coding", "Python", line, col, lines, path=path, inner=inner)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = Clock()
        self.tracker = core.Tracker(self.sent.extend, clock=self.clock)
        self.blend = os.path.join(os.sep, "work", "rocket", "ship.blend")
        self.tracker.file_loaded(self.blend)

    def test_interval_limits_repeat_activity(self):
        self.tracker.activity(scene())
        self.clock.now += 30
        self.tracker.activity(scene(11, 21))
        self.clock.now += core.HEARTBEAT_INTERVAL
        self.tracker.activity(scene(12, 22))
        self.assertEqual([h["lineno"] for h in self.sent], [10, 12])

    def test_entity_or_category_change_sends_immediately(self):
        self.tracker.activity(scene())
        self.tracker.activity(scene(category="designing"))
        self.tracker.activity(script())
        self.assertEqual([h["category"] for h in self.sent], ["building", "designing", "coding"])

    def test_resolved_fields(self):
        self.tracker.activity(scene(5, 6))
        self.tracker.activity(script(3, 4, 9))
        blend, text = self.sent
        self.assertEqual(blend["entity"], self.blend)
        self.assertEqual((blend["lineno"], blend["cursorpos"], blend["lines"]), (5, 6, 0))
        self.assertEqual(blend["language"], "Blender")
        self.assertEqual(blend["alternate_project"], "rocket")
        self.assertFalse(blend["is_write"])
        self.assertFalse(blend["is_unsaved_entity"])
        self.assertEqual(text["entity"], self.blend + os.sep + "script.py")
        self.assertTrue(text["is_unsaved_entity"])
        self.assertEqual((text["lineno"], text["cursorpos"], text["lines"]), (3, 4, 9))

    def test_blend_save_needs_activity(self):
        self.tracker.blend_saved(self.blend, scene())
        self.assertEqual(self.sent, [])
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]["is_write"])
        self.tracker.blend_saved(self.blend, scene())
        self.assertEqual(len(self.sent), 1)

    def test_duplicate_write_skipped_within_window(self):
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene(1, 1))
        self.clock.now += 60
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene(1, 1))
        self.clock.now += 60
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene(2, 2))
        self.clock.now += core.DUPLICATE_WINDOW
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene(2, 2))
        self.assertEqual([h["lineno"] for h in self.sent], [1, 2, 2])

    def test_unsaved_blend_is_queued_until_first_save(self):
        self.tracker.file_loaded("")
        self.tracker.activity(scene(1, 1))
        first_time = self.clock.now
        self.clock.now += 200
        self.tracker.activity(scene(2, 2))
        self.tracker.activity(script(path=os.path.join(os.sep, "work", "tool.py"), inner=None))
        self.assertEqual(self.sent, [])
        self.clock.now += 10
        saved = os.path.join(os.sep, "art", "car", "car.blend")
        self.tracker.blend_saved(saved, scene(3, 3))
        self.assertEqual([h["lineno"] for h in self.sent], [1, 2, 1, 3])
        self.assertEqual(self.sent[0]["time"], first_time)
        self.assertEqual(self.sent[0]["entity"], saved)
        self.assertEqual(self.sent[2]["entity"], os.path.join(os.sep, "work", "tool.py"))
        self.assertTrue(all(h["alternate_project"] == "car" for h in self.sent))
        self.assertEqual([h["is_write"] for h in self.sent], [False, False, False, True])

    def test_loading_a_file_sends_the_queue_as_unsaved(self):
        self.tracker.file_loaded("")
        self.tracker.activity(scene(7, 8))
        queued_time = self.clock.now
        self.tracker.activity(script(2, 3, 4))
        self.tracker.activity(script(path=os.path.join(os.sep, "work", "tool.py"), inner=None))
        self.clock.now += 50
        self.tracker.file_loaded(self.blend)
        blend, inner, external = self.sent
        self.assertEqual(blend["entity"], core.UNSAVED_BLEND)
        self.assertTrue(blend["is_unsaved_entity"])
        self.assertIsNone(blend["alternate_project"])
        self.assertEqual((blend["lineno"], blend["cursorpos"], blend["time"]), (7, 8, queued_time))
        self.assertEqual(inner["entity"], core.UNSAVED_BLEND + os.sep + "script.py")
        self.assertTrue(inner["is_unsaved_entity"])
        self.assertEqual(external["entity"], os.path.join(os.sep, "work", "tool.py"))
        self.assertFalse(external["is_unsaved_entity"])
        self.tracker.touch()
        self.tracker.blend_saved(self.blend, scene())
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.sent[3]["entity"], self.blend)

    def test_closing_sends_the_queue_as_unsaved(self):
        self.tracker.file_loaded("")
        self.tracker.activity(scene())
        self.tracker.close()
        self.tracker.close()
        self.assertEqual([h["entity"] for h in self.sent], [core.UNSAVED_BLEND])

    def test_adopting_a_path_sends_the_queue_under_it(self):
        self.tracker.file_loaded("")
        self.tracker.activity(scene(3, 4))
        self.tracker.adopt("")
        self.assertEqual(self.sent, [])
        self.tracker.adopt(self.blend)
        self.tracker.adopt(os.path.join(os.sep, "other", "file.blend"))
        self.assertEqual([(h["entity"], h["is_unsaved_entity"], h["is_write"]) for h in self.sent], [(self.blend, False, False)])
        self.tracker.activity(scene(5, 6, "designing"))
        self.assertEqual(self.sent[-1]["entity"], self.blend)

    def test_closing_a_saved_file_sends_nothing(self):
        self.tracker.activity(scene())
        self.tracker.close()
        self.assertEqual(len(self.sent), 1)

    def test_full_queue_sends_the_oldest_as_unsaved(self):
        self.tracker.file_loaded("")
        for index in range(core.MAX_PENDING + 2):
            self.tracker.activity(scene(index, 0, "building" if index % 2 else "designing"))
        self.assertEqual([h["lineno"] for h in self.sent], [0, 1])
        self.assertTrue(all(h["is_unsaved_entity"] for h in self.sent))
        self.assertEqual(len(self.tracker.pending), core.MAX_PENDING)

    def test_text_save_is_write(self):
        self.tracker.text_saved(script(2, 3, path="/work/tool.py", inner=None))
        self.tracker.text_saved(script(2, 3, path="/work/tool.py", inner=None))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]["is_write"])


class HelpersTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(core.category_for("TEXT_EDITOR", "TEXT_EDITOR", "OBJECT"), "coding")
        self.assertEqual(core.category_for("VIEW_3D", "VIEW_3D", "OBJECT"), "building")
        self.assertEqual(core.category_for("VIEW_3D", "VIEW_3D", "EDIT_MESH"), "building")
        self.assertEqual(core.category_for("VIEW_3D", "VIEW_3D", "SCULPT"), "designing")
        self.assertEqual(core.category_for("VIEW_3D", "VIEW_3D", "PAINT_TEXTURE"), "designing")
        self.assertEqual(core.category_for("NODE_EDITOR", "GeometryNodeTree", "OBJECT"), "building")
        self.assertEqual(core.category_for("NODE_EDITOR", "ShaderNodeTree", "OBJECT"), "designing")
        self.assertEqual(core.category_for("GRAPH_EDITOR", "FCURVES", "OBJECT"), "designing")
        self.assertEqual(core.category_for("OUTLINER", "OUTLINER", "OBJECT"), "building")

    def test_positions(self):
        self.assertEqual(core.mouse_position(100, 0, 800), (100, 799))
        self.assertEqual(core.mouse_position(100, 799, 800), (100, 0))
        self.assertEqual(core.text_position(0, 0), (1, 1))

    def test_languages(self):
        self.assertEqual(core.text_language("Text", None), "Python")
        self.assertEqual(core.text_language("rig.py", None), "Python")
        self.assertIsNone(core.text_language("notes.md", None))

    def test_cli_args(self):
        heartbeat = core.resolve(script(2, 3, 4), "/a/b/c.blend")
        heartbeat["time"] = 12.5
        args = cli.heartbeat_args(heartbeat)
        self.assertIn("--write=false", args)
        self.assertIn("--is-unsaved-entity", args)
        self.assertEqual(args[args.index("--lines-in-file") + 1], "4")
        self.assertEqual(args[args.index("--language") + 1], "Python")
        self.assertEqual(args[args.index("--alternate-project") + 1], "b")

    def test_cli_args_for_unsaved_blend(self):
        heartbeat = core.resolve(scene(), "")
        args = cli.heartbeat_args(heartbeat)
        self.assertEqual(args[args.index("--entity") + 1], core.UNSAVED_BLEND)
        self.assertTrue(os.path.isabs(core.UNSAVED_BLEND))
        self.assertIn("--is-unsaved-entity", args)
        self.assertNotIn("--alternate-project", args)
        self.assertNotIn("alternate_project", cli.extra_heartbeat(heartbeat))

    def test_config_update_keeps_other_lines(self):
        text = "# mine\n[settings]\napi_key = old\ndebug = true\n\n[git]\nsubmodules_disabled = false\n"
        updated = cli.update_settings(text, {"api_url": "https://example.test/api", "api_key": "new"})
        self.assertEqual(
            updated,
            "# mine\n[settings]\napi_key = new\ndebug = true\napi_url = https://example.test/api\n\n[git]\nsubmodules_disabled = false\n",
        )

    def test_config_update_creates_settings(self):
        self.assertEqual(cli.update_settings("", {"api_key": "k"}), "[settings]\napi_key = k\n")
        self.assertEqual(cli.update_settings("[git]\na = b", {"api_key": "k"}), "[git]\na = b\n\n[settings]\napi_key = k\n")


if __name__ == "__main__":
    unittest.main()
