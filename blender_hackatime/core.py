import os
import time
from dataclasses import dataclass, field

HEARTBEAT_INTERVAL = 120.0
DUPLICATE_WINDOW = 600.0
MAX_PENDING = 1000
LINE_BASE = 1
FLIP_MOUSE_Y = True
BLEND_LANGUAGE = "Blender"
UNSAVED_BLEND = os.path.join(os.path.expanduser("~"), "untitled.blend")

DESIGNING_EDITORS = {
    "IMAGE_EDITOR",
    "DOPESHEET_EDITOR",
    "GRAPH_EDITOR",
    "NLA_EDITOR",
    "SEQUENCE_EDITOR",
    "CLIP_EDITOR",
}
DESIGNING_NODE_TREES = {"ShaderNodeTree", "CompositorNodeTree", "TextureNodeTree"}
DESIGNING_MODE_PREFIXES = ("SCULPT", "PAINT", "WEIGHT", "VERTEX", "PARTICLE")


def category_for(area_type, ui_type, mode):
    if area_type == "TEXT_EDITOR":
        return "coding"
    if area_type == "NODE_EDITOR":
        return "designing" if ui_type in DESIGNING_NODE_TREES else "building"
    if area_type in DESIGNING_EDITORS:
        return "designing"
    if area_type == "VIEW_3D" and (mode or "").startswith(DESIGNING_MODE_PREFIXES):
        return "designing"
    return "building"


def mouse_position(x, y, window_height):
    if FLIP_MOUSE_Y:
        y = window_height - 1 - y
    return max(int(x), 0), max(int(y), 0)


def text_position(line_index, character):
    return line_index + LINE_BASE, character + LINE_BASE


def text_language(name, path):
    base = os.path.basename(path or name)
    ext = os.path.splitext(base)[1].lower()
    if ext in ("", ".py"):
        return "Python"
    return None


def project_for(blend_path):
    folder = os.path.basename(os.path.dirname(blend_path))
    if folder:
        return folder
    return os.path.splitext(os.path.basename(blend_path))[0]


@dataclass
class Heartbeat:
    category: str
    language: object
    lineno: int
    cursorpos: int
    lines: int
    is_write: bool = False
    path: object = None
    inner: object = None
    time: float = field(default=0.0)

    def key(self):
        return (self.path, self.inner)


def resolve(heartbeat, blend_path):
    base = blend_path or UNSAVED_BLEND
    unsaved = False
    if heartbeat.path:
        entity = heartbeat.path
    elif heartbeat.inner:
        entity = base + os.sep + heartbeat.inner
        unsaved = True
    else:
        entity = base
        unsaved = not blend_path
    return {
        "entity": entity,
        "type": "file",
        "category": heartbeat.category,
        "language": heartbeat.language,
        "lineno": heartbeat.lineno,
        "cursorpos": heartbeat.cursorpos,
        "lines": heartbeat.lines,
        "is_write": heartbeat.is_write,
        "is_unsaved_entity": unsaved,
        "alternate_project": project_for(blend_path) if blend_path else None,
        "time": heartbeat.time,
    }


class Tracker:
    def __init__(self, sink, clock=time.time):
        self.sink = sink
        self.clock = clock
        self.reset("")

    def reset(self, blend_path):
        self.blend_path = blend_path or ""
        self.pending = []
        self.last_key = None
        self.last_category = None
        self.last_sent = None
        self.last_writes = {}
        self.active_since_save = False

    def file_loaded(self, blend_path):
        self.flush_unsaved()
        self.reset(blend_path)

    def adopt(self, blend_path):
        if self.blend_path or not blend_path:
            return
        self.blend_path = blend_path
        batch, self.pending = self.pending, []
        if batch:
            self.sink([resolve(h, blend_path) for h in batch])

    def close(self):
        self.flush_unsaved()

    def flush_unsaved(self):
        batch, self.pending = self.pending, []
        if batch:
            self.sink([resolve(h, "") for h in batch])

    def touch(self):
        self.active_since_save = True

    def activity(self, heartbeat):
        now = self.clock()
        heartbeat.time = now
        heartbeat.is_write = False
        self.active_since_save = True
        changed = heartbeat.key() != self.last_key or heartbeat.category != self.last_category
        if changed or self.last_sent is None or now - self.last_sent >= HEARTBEAT_INTERVAL:
            self._mark(heartbeat, now)
            self._emit([heartbeat])

    def text_saved(self, heartbeat):
        now = self.clock()
        heartbeat.time = now
        heartbeat.is_write = True
        if self._duplicate(heartbeat, now):
            return
        self._mark(heartbeat, now)
        self._emit([heartbeat])

    def blend_saved(self, blend_path, heartbeat):
        now = self.clock()
        self.blend_path = blend_path
        batch, self.pending = self.pending, []
        if heartbeat is not None and self.active_since_save:
            heartbeat.time = now
            heartbeat.is_write = True
            if not self._duplicate(heartbeat, now):
                self._mark(heartbeat, now)
                batch.append(heartbeat)
        self.active_since_save = False
        if batch:
            self.sink([resolve(h, blend_path) for h in batch])

    def _mark(self, heartbeat, now):
        self.last_key = heartbeat.key()
        self.last_category = heartbeat.category
        self.last_sent = now

    def _duplicate(self, heartbeat, now):
        key = heartbeat.key()
        position = (heartbeat.lineno, heartbeat.cursorpos)
        previous = self.last_writes.get(key)
        self.last_writes[key] = (now, position)
        return previous is not None and now - previous[0] < DUPLICATE_WINDOW and previous[1] == position

    def _emit(self, heartbeats):
        if not self.blend_path:
            self.pending.extend(heartbeats)
            overflow = len(self.pending) - MAX_PENDING
            if overflow > 0:
                oldest, self.pending = self.pending[:overflow], self.pending[overflow:]
                self.sink([resolve(h, "") for h in oldest])
            return
        self.sink([resolve(h, self.blend_path) for h in heartbeats])
