import importlib
import json
import os
import sys
import time
import traceback

import bpy

MODULE = os.environ.get("BLENDER_HACKATIME_MODULE", "bl_ext.user_default.blender_hackatime")
LOG = os.environ["FAKE_HACKATIME_LOG"]
ROOT = os.environ.get("TEST_ROOT", "/tmp/bh-background")

hooks = importlib.import_module(MODULE + ".hooks")
failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def received():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as log:
        return [json.loads(line) for line in log if line.strip()]


def wait_for(count, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        entries = received()
        if len(entries) >= count:
            time.sleep(1)
            return received()
        time.sleep(0.25)
    return received()


def set_cursor(text, line, character):
    text.current_line_index = line
    text.current_character = character
    text.select_end_line_index = line
    text.select_end_character = character


def run():
    os.makedirs(os.path.join(ROOT, "rocket"), exist_ok=True)
    blend = os.path.join(ROOT, "rocket", "ship.blend")
    external_path = os.path.join(ROOT, "rocket", "tool.py")
    with open(external_path, "w", encoding="utf-8") as out:
        out.write("import bpy\nprint('a')\nprint('b')\nprint('c')\n")

    check("starts unsaved", hooks.state.tracker.blend_path == "", hooks.state.tracker.blend_path)

    inner = bpy.data.texts.new("rig_helpers.py")
    inner.write("line one\nline two\nline three")
    set_cursor(inner, 1, 4)
    hooks.state.tracker.activity(hooks.text_heartbeat(inner))
    time.sleep(2)
    check("unsaved activity is held back", len(received()) == 0, str(received()))

    hooks.state.text_under_pointer = inner.name
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    entries = wait_for(2)
    check("first save sends queued + write", len(entries) == 2, str(len(entries)))
    queued, write = (e["heartbeat"] for e in entries[:2])
    check("queued entity", queued.get("entity") == blend + os.sep + "rig_helpers.py", queued.get("entity"))
    check("queued lineno", queued.get("lineno") == 2, queued.get("lineno"))
    check("queued cursorpos", queued.get("cursorpos") == 5, queued.get("cursorpos"))
    check("queued lines", queued.get("lines") == 3, queued.get("lines"))
    check("queued category coding is left out by wakatime-cli", queued.get("category") in (None, "coding"), str(queued.get("category")))
    check("queued language", queued.get("language") == "Python", queued.get("language"))
    check("queued is_write false", queued.get("is_write") is False, queued.get("is_write"))
    check("queued project", queued.get("project") == "rocket", queued.get("project"))
    check("queued type", queued.get("type") == "file", queued.get("type"))
    check("queued keeps its time", queued.get("time", 0) < write.get("time", 0) - 1, f"{queued.get('time')} vs {write.get('time')}")
    check("write is_write true", write.get("is_write") is True, write.get("is_write"))
    blender_version = ".".join(str(part) for part in bpy.app.version)
    agent = entries[0]["user_agent"] or ""
    check("user agent", f"blender/{blender_version} blender-hackatime/0.1.0" in agent, agent)

    external = bpy.data.texts.load(external_path)
    set_cursor(external, 3, 2)
    hooks.poll_texts()
    hooks.state.tracker.activity(hooks.text_heartbeat(external))
    entries = wait_for(3)
    hb = entries[2]["heartbeat"] if len(entries) > 2 else {}
    check("external entity", hb.get("entity") == external_path, hb.get("entity"))
    check("external lineno", hb.get("lineno") == 4 and hb.get("cursorpos") == 3, f"{hb.get('lineno')},{hb.get('cursorpos')}")
    check("external lines", hb.get("lines") == 5, hb.get("lines"))
    check("external not unsaved", "is_unsaved_entity" not in hb or not hb.get("is_unsaved_entity"), str(hb))

    set_cursor(external, 0, 0)
    external.write("# edited\n")
    hooks.poll_texts()
    with bpy.context.temp_override(edit_text=external):
        bpy.ops.text.save()
    hooks.poll_texts()
    entries = wait_for(4)
    hb = entries[3]["heartbeat"] if len(entries) > 3 else {}
    check("text save is write", hb.get("is_write") is True and hb.get("entity") == external_path, str(hb))

    hooks.state.tracker.touch()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(ROOT, "copy.blend"), copy=True)
    time.sleep(3)
    check("save copy ignored", len(received()) == 4, str(len(received())))

    bpy.ops.wm.read_homefile(use_empty=True)
    check("loading resets to unsaved", hooks.state.tracker.blend_path == "" and hooks.state.tracker.pending == [], hooks.state.tracker.blend_path)

    hooks.state.tracker.activity(hooks.core.Heartbeat("building", hooks.core.BLEND_LANGUAGE, 4001, 7, 0))
    time.sleep(2)
    check("unsaved activity is held back again", len(received()) == 4 and len(hooks.state.tracker.pending) == 1, str(len(received())))
    bpy.ops.wm.read_homefile(use_empty=True)
    entries = wait_for(5)
    hb = entries[4]["heartbeat"] if len(entries) > 4 else {}
    check("opening another file sends the queue as unsaved", hb.get("lineno") == 4001 and str(hb.get("entity", "")).endswith("untitled.blend"), str(hb))
    check("unsaved heartbeat has no project", not hb.get("project"), str(hb.get("project")))
    check("unsaved heartbeat keeps its fields", hb.get("category") == "building" and hb.get("language") == "Blender" and hb.get("lines") == 0 and hb.get("is_write") is False, str(hb))

    hooks.state.tracker.activity(hooks.core.Heartbeat("designing", hooks.core.BLEND_LANGUAGE, 4002, 7, 0))
    check("queued before quit", len(hooks.state.tracker.pending) == 1, str(len(hooks.state.tracker.pending)))


try:
    run()
except Exception:
    traceback.print_exc()
    failures.append("exception")

print("RESULT " + ("FAIL " + ", ".join(failures) if failures else "OK"))
sys.stdout.flush()
