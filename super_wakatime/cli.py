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
PROCESS_TIMEOUT_SECONDS = 120.0
OFFLINE_EXIT_CODES = (0, 102, 112)

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
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path(), encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    if not parser.has_section("settings"):
        return {}
    return dict(parser.items("settings"))


def write_config(values):
    parser = configparser.ConfigParser(interpolation=None)
    path = config_path()
    parser.read(path, encoding="utf-8")
    if not parser.has_section("settings"):
        parser.add_section("settings")
    for key, value in values.items():
        parser.set("settings", key, value)
    with open(path, "w", encoding="utf-8") as out:
        parser.write(out)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def heartbeat_args(heartbeat):
    args = [
        "--entity", heartbeat["entity"],
        "--entity-type", heartbeat["type"],
        "--category", heartbeat["category"],
        "--lineno", str(heartbeat["lineno"]),
        "--cursorpos", str(heartbeat["cursorpos"]),
        "--lines-in-file", str(heartbeat["lines"]),
        "--time", f"{heartbeat['time']:.3f}",
        "--alternate-project", heartbeat["alternate_project"],
        "--write=true" if heartbeat["is_write"] else "--write=false",
    ]
    if heartbeat["language"]:
        args += ["--language", heartbeat["language"]]
    if heartbeat["is_unsaved_entity"]:
        args.append("--is-unsaved-entity")
    return args


def extra_heartbeat(heartbeat):
    data = dict(heartbeat)
    if not data["language"]:
        del data["language"]
    return data


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

    def prepare(self):
        self._put("prepare")

    def submit(self, heartbeats):
        if heartbeats:
            self._put(list(heartbeats))

    def stop(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.queue.put(None)
            self.thread = None

    def _put(self, item):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="super-wakatime", daemon=True)
                self.thread.start()
            self.queue.put(item)

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                cli = self._find_or_download()
                if cli and item != "prepare":
                    self._send(cli, item)
            except Exception as exc:
                self.last_error = str(exc)
                self.log(f"Super-Wakatime: {exc}")

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

    def _send(self, cli, batch):
        first, extra = batch[0], batch[1:]
        args = [cli, *heartbeat_args(first), "--plugin", self.plugin]
        if extra:
            args.append("--extra-heartbeats")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if extra else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        payload = json.dumps([extra_heartbeat(h) for h in extra]).encode() if extra else None
        try:
            _, stderr = process.communicate(payload, timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("wakatime-cli timed out")
        if process.returncode not in OFFLINE_EXIT_CODES:
            message = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"wakatime-cli exited with {process.returncode}: {message}")
        self.last_error = ""
