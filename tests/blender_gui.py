import glob
import importlib
import json
import os
import sys
import time
import traceback

import bpy

MODULE = os.environ.get("SUPER_WAKATIME_MODULE", "bl_ext.user_default.super_wakatime")
LOG = os.environ["FAKE_HACKATIME_LOG"]
ROOT = os.environ.get("TEST_ROOT", "/tmp/bsw-gui")
LIFETIME = float(os.environ.get("LISTENER_LIFETIME", "3"))
AUTOSAVE_WAIT = float(os.environ.get("AUTOSAVE_WAIT", "0"))

hooks = importlib.import_module(MODULE + ".hooks")
hooks.LISTENER_LIFETIME = 100000
failures = []
facts = {}


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" ({detail})" if detail and not condition else ""), flush=True)
    if not condition:
        failures.append(name)


def received():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as log:
        return [json.loads(line)["heartbeat"] for line in log if line.strip()]


def window():
    return bpy.context.window_manager.windows[0]


def area_of(kind):
    return max((a for a in window().screen.areas if a.type == kind), key=lambda a: a.width * a.height)


def center(area):
    return area.x + area.width // 2, area.y + area.height // 2


def simulate(kind, value, x, y, unicode=""):
    if unicode:
        window().event_simulate(kind, value, x=x, y=y, unicode=unicode)
    else:
        window().event_simulate(kind, value, x=x, y=y)


def click(x, y):
    simulate("MOUSEMOVE", "NOTHING", x, y)
    simulate("LEFTMOUSE", "PRESS", x, y)
    simulate("LEFTMOUSE", "RELEASE", x, y)


def call(op, **kwargs):
    win = window()
    with bpy.context.temp_override(window=win, screen=win.screen):
        return op(**kwargs)


def wait_count(count, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if len(received()) >= count:
            return
        yield 0.25


def steps():
    os.makedirs(os.path.join(ROOT, "garden"), exist_ok=True)
    blend = os.path.join(ROOT, "garden", "shed.blend")
    yield 2.0
    win = window()
    facts["height"] = win.height
    check("listener running", win.as_pointer() in hooks.state.listeners, str(hooks.state.listeners))

    view = area_of("VIEW_3D")
    vx, vy = center(view)
    click(vx, vy)
    yield 1.0
    check("unsaved click held back", len(received()) == 0 and len(hooks.state.tracker.pending) == 1, f"{received()} {hooks.state.tracker.pending}")

    call(bpy.ops.wm.save_as_mainfile, filepath=blend)
    yield from wait_count(2)
    got = received()
    check("first save sends click + write", len(got) == 2, str(got))
    if len(got) >= 2:
        clicked, write = got[0], got[1]
        check("click entity", clicked.get("entity") == blend, clicked.get("entity"))
        check("click lineno is mouse x", clicked.get("lineno") == vx, f"{clicked.get('lineno')} vs {vx}")
        check("click cursorpos is flipped mouse y", clicked.get("cursorpos") == win.height - 1 - vy, f"{clicked.get('cursorpos')} vs {win.height - 1 - vy}")
        check("click lines 0", clicked.get("lines") == 0, clicked.get("lines"))
        check("click category building", clicked.get("category") == "building", clicked.get("category"))
        check("click language", clicked.get("language") == "Blender", clicked.get("language"))
        check("click project", clicked.get("project") == "garden", clicked.get("project"))
        check("click is_write false", clicked.get("is_write") is False, clicked.get("is_write"))
        check("write is_write true", write.get("is_write") is True, write.get("is_write"))
        check("write uses last mouse position", write.get("lineno") == vx, write.get("lineno"))

    click(vx + 5, vy + 5)
    yield 1.0
    check("repeat click within interval skipped", len(received()) == 2, str(len(received())))

    call(bpy.ops.object.mode_set, mode="SCULPT") if bpy.context.object else None
    mode_ok = bpy.context.mode == "SCULPT"
    click(vx + 10, vy - 10)
    yield from wait_count(3)
    got = received()
    if mode_ok:
        check("sculpt click is designing", len(got) >= 3 and got[2].get("category") == "designing", str(got[2:]))
    else:
        check("sculpt mode available", False, bpy.context.mode)
    call(bpy.ops.object.mode_set, mode="OBJECT")
    yield 0.5

    other = min((a for a in win.screen.areas if a.type != "VIEW_3D"), key=lambda a: -a.width * a.height)
    other.type = "TEXT_EDITOR"
    text = bpy.data.texts.new("garden_tools.py")
    other.spaces.active.text = text
    yield 0.5
    tx, ty = center(other)
    simulate("MOUSEMOVE", "NOTHING", tx, ty)
    for ch in "hi":
        simulate(ch.upper(), "PRESS", tx, ty, unicode=ch)
        simulate(ch.upper(), "RELEASE", tx, ty)
    yield from wait_count(4)
    got = received()
    hb = got[3] if len(got) >= 4 else {}
    check("typing sends coding", bool(hb) and hb.get("category") in (None, "coding") and hb.get("language") == "Python", str(hb))
    check("typed text content", text.as_string() == "hi", repr(text.as_string()))
    check("typing entity", hb.get("entity") == blend + os.sep + "garden_tools.py", hb.get("entity"))
    check("typing lineno 1-based", hb.get("lineno") == 1, hb.get("lineno"))
    check("typing cursorpos after keys", hb.get("cursorpos") in (2, 3), hb.get("cursorpos"))
    check("typing lines", hb.get("lines") == 1, hb.get("lines"))
    check("typing language", hb.get("language") == "Python", hb.get("language"))

    simulate("RET", "PRESS", tx, ty)
    simulate("RET", "RELEASE", tx, ty)
    yield 0.5
    call(bpy.ops.wm.save_mainfile)
    yield from wait_count(5)
    got = received()
    hb = got[4] if len(got) >= 5 else {}
    check("save over text editor writes the internal script", hb.get("is_write") is True and hb.get("entity") == blend + os.sep + "garden_tools.py" and hb.get("lineno") == 2, str(hb))

    if LIFETIME < 1000:
        hooks.LISTENER_LIFETIME = LIFETIME
        starts = []
        original = hooks.start_listeners

        def counting():
            before = set(hooks.state.listeners)
            original()
            starts.extend(hooks.state.listeners - before)

        hooks.start_listeners = counting
        for _ in range(int(LIFETIME * 2) + 6):
            simulate("MOUSEMOVE", "NOTHING", vx, vy)
            yield 0.5
        hooks.start_listeners = original
        check("listener stopped and restarted", len(starts) >= 1 and win.as_pointer() in hooks.state.listeners, f"{starts} {hooks.state.listeners}")
        click(vx + 1, vy)
        yield 1.0
        check("events still handled after restart", hooks.state.scene_pointer is not None and hooks.state.scene_pointer[1] == vx + 1, str(hooks.state.scene_pointer))

    if AUTOSAVE_WAIT:
        prefs = bpy.context.preferences.filepaths
        prefs.use_auto_save_temporary_files = True
        prefs.auto_save_time = 1
        started = time.time()
        bases = {os.path.dirname(bpy.app.tempdir.rstrip("/\\")), "/tmp", prefs.temporary_directory or "/tmp"}
        name = os.path.splitext(os.path.basename(bpy.data.filepath))[0] + "_*autosave.blend"
        click(vx + 2, vy + 2)
        yield 0.5
        call(bpy.ops.ed.undo_push, message="autosave test")
        found = []
        while time.time() - started < AUTOSAVE_WAIT and not found:
            simulate("MOUSEMOVE", "NOTHING", vx + int(time.time()) % 20, vy)
            found = [p for base in bases for p in glob.glob(os.path.join(base, name)) if os.path.getmtime(p) >= started]
            yield 1.0
        facts["autosave"] = found
        print(f"AUTOSAVE {found}", flush=True)
        check("autosave ran" if LIFETIME < 1000 else "autosave blocked without restarts", bool(found) == (LIFETIME < 1000), str(found))


gen = steps()


def tick():
    try:
        return next(gen)
    except StopIteration:
        pass
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    print("RESULT " + ("FAIL " + ", ".join(failures) if failures else "OK"), flush=True)
    print("FACTS " + json.dumps(facts), flush=True)
    os._exit(1 if failures else 0)


bpy.app.timers.register(tick, first_interval=3.0)
