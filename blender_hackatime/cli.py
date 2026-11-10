import configparser
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

CLI_VERSION = "v2.28.4"
RELEASE_URL = "https://github.com/wakatime/wakatime-cli/releases/download/{version}/{build}.zip"
FALLBACK_PATHS = ("/opt/wakatime/wakatime-cli-linux-amd64",)
DEFAULT_API_URL = "https://hackatime.hackclub.com/api/hackatime/v1"
DOWNLOAD_RETRY_SECONDS = 300.0
PROCESS_TIMEOUT_SECONDS = 600.0
OFFLINE_EXIT_CODES = (0, 102, 112)
PREPARE = "prepare"

ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "x86": "386",
}


def wakatime_home():
    home = os.environ.get("WAKATIME_HOME")
    if home:
        return os.path.expanduser(home)
    return os.path.expanduser("~")


def resource_dir():
    return os.path.join(wakatime_home(), ".wakatime")


def config_path():
    return os.path.join(wakatime_home(), ".wakatime.cfg")


def build_name():
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = ARCHITECTURES.get(machine, "arm" if machine.startswith("arm") else machine)
    return f"wakatime-cli-{system}-{arch}"


def exe_suffix():
    return ".exe" if sys.platform == "win32" else ""


def find_cli():
    candidates = [
        os.path.join(resource_dir(), build_name() + exe_suffix()),
        os.path.join(resource_dir(), "wakatime-cli" + exe_suffix()),
        *FALLBACK_PATHS,
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def download_cli():
    member = build_name() + exe_suffix()
    url = RELEASE_URL.format(version=CLI_VERSION, build=build_name())
    target_dir = resource_dir()
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_dir) as tmp:
        archive = os.path.join(tmp, "wakatime-cli.zip")
        with urllib.request.urlopen(url, timeout=60) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out)
        with zipfile.ZipFile(archive) as zf:
            zf.extract(member, tmp)
        source = os.path.join(tmp, member)
        os.chmod(source, os.stat(source).st_mode | 0o755)
        target = os.path.join(target_dir, member)
        os.replace(source, target)
    return target


def read_config():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(config_path(), encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not parser.has_section("settings"):
        return {}
    return dict(parser.items("settings"))


def update_settings(text, values):
    lines = text.splitlines()
    missing = set(values)
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        if start is None and stripped[1:-1].strip().lower() == "settings":
            start = index
        elif start is not None:
            end = index
            break
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[settings]")
        start = len(lines) - 1
        end = len(lines)
    for index in range(start + 1, end):
        line = lines[index]
        if line[:1] in ("#", ";", " ", "\t") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().lower()
        if key in values:
            lines[index] = f"{key} = {values[key]}"
            missing.discard(key)
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = [f"{key} = {value}" for key, value in values.items() if key in missing]
    return "\n".join(lines) + "\n"


def write_config(values):
    path = config_path()
    try:
        with open(path, encoding="utf-8") as existing:
            text = existing.read()
    except FileNotFoundError:
        text = ""
    updated = update_settings(text, values)
    directory = os.path.dirname(path) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".wakatime.cfg.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(updated)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def heartbeat_args(heartbeat):
    args = [
        "--entity", heartbeat["entity"],
        "--entity-type", heartbeat["type"],
        "--category", heartbeat["category"],
        "--lineno", str(heartbeat["lineno"]),
        "--cursorpos", str(heartbeat["cursorpos"]),
        "--lines-in-file", str(heartbeat["lines"]),
        "--time", f"{heartbeat['time']:.3f}",
        "--write=true" if heartbeat["is_write"] else "--write=false",
    ]
    if heartbeat["alternate_project"]:
        args += ["--alternate-project", heartbeat["alternate_project"]]
    if heartbeat["language"]:
        args += ["--language", heartbeat["language"]]
    if heartbeat["is_unsaved_entity"]:
        args.append("--is-unsaved-entity")
    return args


def extra_heartbeat(heartbeat):
    return {key: value for key, value in heartbeat.items() if value is not None}


class Sender:
    def __init__(self, plugin, log=print):
        self.plugin = plugin
        self.log = log
        self.queue = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()
        self.cli = None
        self.download_failed_at = None
        self.last_error = ""
        self.logged = None

    def prepare(self):
        self._put(PREPARE)

    def submit(self, heartbeats):
        if heartbeats:
            self._put(list(heartbeats))

    def stop(self):
        with self.lock:
            thread, self.thread = self.thread, None
        leftover, _ = self._drain()
        if thread is not None and thread.is_alive():
            self.queue.put(None)
        if not leftover:
            return
        cli = self.cli if self.cli and os.path.isfile(self.cli) else find_cli()
        if not cli:
            return
        try:
            self._spawn(cli, leftover, wait=False)
        except Exception as exc:
            self._report(str(exc))

    def _put(self, item):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="blender-hackatime", daemon=True)
                self.thread.start()
            self.queue.put(item)

    def _drain(self):
        batch = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return batch, False
            if item is None:
                return batch, True
            if isinstance(item, list):
                batch.extend(item)

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            batch = item if isinstance(item, list) else []
            more, stopped = self._drain()
            batch.extend(more)
            try:
                cli = self._find_or_download()
                if cli and batch:
                    self._spawn(cli, batch, wait=True)
            except Exception as exc:
                self._report(str(exc))
            if stopped:
                return

    def _report(self, message):
        self.last_error = message
        if message != self.logged:
            self.logged = message
            self.log(f"Blender Hackatime: {message}")

    def _find_or_download(self):
        if self.cli and os.path.isfile(self.cli):
            return self.cli
        self.cli = find_cli()
        if self.cli:
            return self.cli
        now = time.monotonic()
        if self.download_failed_at is not None and now - self.download_failed_at < DOWNLOAD_RETRY_SECONDS:
            return None
        try:
            self.cli = download_cli()
        except Exception as exc:
            self.download_failed_at = now
            raise RuntimeError(f"could not download wakatime-cli: {exc}") from exc
        return self.cli

    def _spawn(self, cli, batch, wait):
        first, extra = batch[0], batch[1:]
        args = [cli, *heartbeat_args(first), "--plugin", self.plugin]
        if extra:
            args.append("--extra-heartbeats")
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE if wait else subprocess.DEVNULL}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        elif not wait:
            options["start_new_session"] = True
        if extra:
            with tempfile.TemporaryFile() as stdin:
                stdin.write(json.dumps([extra_heartbeat(h) for h in extra]).encode() + b"\n")
                stdin.seek(0)
                process = subprocess.Popen(args, stdin=stdin, **options)
        else:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, **options)
        if not wait:
            return
        try:
            _, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("wakatime-cli timed out")
        if process.returncode not in OFFLINE_EXIT_CODES:
            message = stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"wakatime-cli exited with {process.returncode}: {message[-1] if message else ''}")
        self.last_error = ""
        self.logged = None
