import atexit
import functools
import os
import time
import tomllib

import bpy
from bpy.app.handlers import persistent

from . import cli, core

LISTENER_LIFETIME = 60.0
WATCH_INTERVAL = 0.25

MOVE_EVENTS = {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}
CLICK_EVENTS = {"LEFTMOUSE", "MIDDLEMOUSE", "RIGHTMOUSE"}
IGNORED_KEYS = {
    "LEFT_ARROW", "RIGHT_ARROW", "UP_ARROW", "DOWN_ARROW",
    "LEFT_ALT", "RIGHT_ALT", "LEFT_SHIFT", "RIGHT_SHIFT",
    "LEFT_CTRL", "RIGHT_CTRL", "OSKEY", "HYPER",
}
NON_KEY_MARKERS = ("MOUSE", "WHEEL", "TRACKPAD", "NDOF", "TIMER", "WINDOW", "ACTIONZONE", "XR_", "PEN", "ERASER", "NONE", "TEXTINPUT")
IGNORED_AREAS = {"PREFERENCES", "FILE_BROWSER", "INFO", "CONSOLE"}


def read_version():
    path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
    try:
        with open(path, "rb") as manifest:
            return tomllib.load(manifest).get("version", "unknown")
    except OSError:
        return "unknown"


def user_agent():
    blender = ".".join(str(part) for part in bpy.app.version)
    return f"blender/{blender} blender-hackatime/{read_version()}"


class State:
    def __init__(self):
        self.sender = None
        self.tracker = None
        self.generation = 0
        self.listeners = set()
        self.timers = {}
        self.logged = None
        self.scene_pointer = None
        self.text_under_pointer = None
        self.text_dirty = {}
        self.text_files = {}


state = State()


def report(message):
    if message != state.logged:
        state.logged = message
        print(f"Blender Hackatime: {message}")


def is_key(event):
    if event.value != "PRESS" or event.type in IGNORED_KEYS:
        return False
    return not any(marker in event.type for marker in NON_KEY_MARKERS)


def area_at(window, x, y):
    screen = window.screen
    if screen is None:
        return None
    for area in screen.areas:
        if area.x <= x < area.x + area.width and area.y <= y < area.y + area.height:
            return area
    return None


def mouse_heartbeat(window, x, y, area):
    lineno, cursorpos = core.mouse_position(x, y, window.height)
    category = core.category_for(area.type, area.ui_type, bpy.context.mode)
    return core.Heartbeat(category, core.BLEND_LANGUAGE, lineno, cursorpos, 0)


def text_file(text):
    if not text.filepath or text.is_in_memory:
        return None
    path = os.path.normpath(bpy.path.abspath(text.filepath))
    return path if os.path.isfile(path) else None


def text_heartbeat(text):
    path = text_file(text)
    lineno, cursorpos = core.text_position(text.current_line_index, text.current_character)
    return core.Heartbeat(
        "coding",
        core.text_language(text.name, path),
        lineno,
        cursorpos,
        len(text.lines),
        path=path,
        inner=None if path else text.name,
    )


def save_heartbeat():
    if state.text_under_pointer is not None:
        text = bpy.data.texts.get(state.text_under_pointer)
        if text is not None and text_file(text) is None:
            return text_heartbeat(text)
    if state.scene_pointer is None:
        return None
    window_key, x, y = state.scene_pointer
    for window in bpy.context.window_manager.windows:
        if window.as_pointer() == window_key:
            area = area_at(window, x, y)
            if area is None or area.type in IGNORED_AREAS or area.type == "TEXT_EDITOR":
                return None
            return mouse_heartbeat(window, x, y, area)
    return None


def snapshot_text(name):
    text = bpy.data.texts.get(name)
    if text is not None and state.tracker is not None:
        state.tracker.activity(text_heartbeat(text))
    return None


def handle_event(window, event):
    move = event.type in MOVE_EVENTS
    click = event.type in CLICK_EVENTS and event.value == "PRESS"
    key = not move and not click and is_key(event)
    if not (move or click or key):
        return
    area = area_at(window, event.mouse_x, event.mouse_y)
    if area is None or area.type in IGNORED_AREAS:
        return
    if area.type == "TEXT_EDITOR":
        text = area.spaces.active.text
        state.text_under_pointer = text.name if text is not None else None
        if key and text is not None:
            bpy.app.timers.register(functools.partial(snapshot_text, text.name), first_interval=0.0)
        return
    state.text_under_pointer = None
    state.scene_pointer = (window.as_pointer(), event.mouse_x, event.mouse_y)
    if move:
        state.tracker.touch()
        return
    state.tracker.activity(mouse_heartbeat(window, event.mouse_x, event.mouse_y, area))


class BLENDERHACKATIME_OT_listener(bpy.types.Operator):
    bl_idname = "wm.blender_hackatime_listener"
    bl_label = "Blender Hackatime Listener"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        window = context.window
        if window is None:
            return {"CANCELLED"}
        self.window_key = window.as_pointer()
        self.generation = state.generation
        self.started = time.monotonic()
        if self.window_key not in state.timers:
            state.timers[self.window_key] = context.window_manager.event_timer_add(LISTENER_LIFETIME, window=window)
        context.window_manager.modal_handler_add(self)
        state.listeners.add(self.window_key)
        return {"RUNNING_MODAL"}

    def finish(self, context):
        if self.generation == state.generation:
            state.listeners.discard(self.window_key)
        return {"CANCELLED", "PASS_THROUGH"}

    def modal(self, context, event):
        if self.generation != state.generation or state.tracker is None:
            return self.finish(context)
        try:
            if context.window is not None:
                handle_event(context.window, event)
        except Exception as exc:
            report(str(exc))
        if time.monotonic() - self.started >= LISTENER_LIFETIME:
            return self.finish(context)
        return {"PASS_THROUGH"}


def start_listeners():
    windows = {window.as_pointer(): window for window in bpy.context.window_manager.windows}
    state.listeners &= set(windows)
    for key in set(state.timers) - set(windows):
        del state.timers[key]
    for key, window in windows.items():
        if key in state.listeners:
            continue
        try:
            with bpy.context.temp_override(window=window, screen=window.screen):
                bpy.ops.wm.blender_hackatime_listener("INVOKE_DEFAULT")
        except Exception as exc:
            report(f"could not start listener: {exc}")


def poll_texts():
    seen = set()
    for text in bpy.data.texts:
        key = text.as_pointer()
        seen.add(key)
        dirty = text.is_dirty
        was_dirty = state.text_dirty.get(key)
        path = text_file(text)
        moved = state.text_files.get(key) not in (None, path)
        state.text_dirty[key] = dirty
        state.text_files[key] = path
        if path and (was_dirty and not dirty or moved):
            state.tracker.text_saved(text_heartbeat(text))
    for key in set(state.text_dirty) - seen:
        state.text_dirty.pop(key, None)
        state.text_files.pop(key, None)


def watch():
    if state.tracker is None:
        return None
    try:
        state.tracker.adopt(bpy.data.filepath)
        start_listeners()
        poll_texts()
    except Exception as exc:
        report(str(exc))
    return WATCH_INTERVAL


def same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@persistent
def on_save_post(*args):
    if state.tracker is None:
        return
    blend = bpy.data.filepath
    saved = args[0] if args and isinstance(args[0], str) else ""
    if not blend or not saved or not same_file(saved, blend):
        return
    state.tracker.blend_saved(blend, save_heartbeat())


@persistent
def on_load_post(*args):
    if state.tracker is None:
        return
    state.generation += 1
    state.listeners.clear()
    state.timers.clear()
    state.scene_pointer = None
    state.text_under_pointer = None
    state.text_dirty.clear()
    state.text_files.clear()
    state.tracker.file_loaded(bpy.data.filepath)


def current_blend():
    try:
        return bpy.data.filepath
    except AttributeError:
        return ""


def remove_timers():
    try:
        manager = bpy.context.window_manager
        for timer in state.timers.values():
            manager.event_timer_remove(timer)
    except Exception:
        pass
    state.timers.clear()


def shutdown():
    if state.tracker is not None:
        state.tracker.close()
        state.tracker = None
    if state.sender is not None:
        state.sender.stop()
        state.sender = None


def register():
    state.sender = cli.Sender(user_agent())
    state.tracker = core.Tracker(state.sender.submit)
    state.tracker.file_loaded(current_blend())
    state.generation += 1
    state.listeners.clear()
    bpy.utils.register_class(BLENDERHACKATIME_OT_listener)
    bpy.app.handlers.save_post.append(on_save_post)
    bpy.app.handlers.load_post.append(on_load_post)
    bpy.app.timers.register(watch, first_interval=WATCH_INTERVAL, persistent=True)
    atexit.register(shutdown)
    if not bpy.app.background:
        state.sender.prepare()


def unregister():
    if bpy.app.timers.is_registered(watch):
        bpy.app.timers.unregister(watch)
    for handlers, handler in ((bpy.app.handlers.save_post, on_save_post), (bpy.app.handlers.load_post, on_load_post)):
        if handler in handlers:
            handlers.remove(handler)
    atexit.unregister(shutdown)
    state.generation += 1
    state.listeners.clear()
    shutdown()
    bpy.utils.unregister_class(BLENDERHACKATIME_OT_listener)
    remove_timers()
