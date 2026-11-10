#!/bin/bash
set -u
REPO_SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
CONFIG="${CONFIG:-/tmp/bh-config}"
OUT="${OUT:-/tmp}"
IMAGE=linuxserver/blender
CONTAINER=bh-test
STAGE="${STAGE:-all}"

step() { echo; echo "=== $*"; }

if [ "$STAGE" = all ] || [ "$STAGE" = setup ]; then
  step "docker"
  command -v dockerd >/dev/null 2>&1 || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null)
  pgrep -x containerd >/dev/null || (containerd >/var/log/containerd.log 2>&1 &)
  for i in $(seq 1 40); do [ -S /run/containerd/containerd.sock ] && break; sleep 0.25; done
  pgrep -x dockerd >/dev/null || (dockerd --containerd=/run/containerd/containerd.sock >/var/log/dockerd.log 2>&1 &)
  for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE"

  step "container"
  mkdir -p "$CONFIG" && chown 1000:1000 "$CONFIG"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" -p 3000:3000 -e PUID=1000 -e PGID=1000 -e PASSWORD=test-password --shm-size=1g -v "$CONFIG":/config "$IMAGE" >/dev/null
  for i in $(seq 1 120); do code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/); [ "$code" != 000 ] && break; sleep 0.5; done
  echo "desktop answered $code"
fi

step "copy repo"
rm -rf "$CONFIG/bh" && cp -r "$REPO_SRC" "$CONFIG/bh" && chown -R 1000:1000 "$CONFIG/bh"

in_app() { docker exec -u abc -e HOME=/config "$CONTAINER" bash -lc "$1"; }

BLENDER="${BLENDER:-$(in_app 'command -v blender || ls /opt/blender*/blender 2>/dev/null | head -1' | tail -1)}"
quit_check() { sleep 5; in_app "grep -q '$1' /tmp/fake/heartbeats.jsonl && echo 'PASS quit sends the queue as unsaved' || echo 'FAIL quit sends the queue as unsaved'"; }
echo "blender: $BLENDER"
in_app "$BLENDER --version | head -1"
GUI_ENV=$(in_app 'for i in $(seq 1 90); do p=$(pgrep -f "[b]lender --python-expr" | head -1); [ -n "$p" ] && break; sleep 1; done; tr "\0" "\n" < /proc/$p/environ | grep -E "^(DISPLAY|WAYLAND_DISPLAY|XDG_RUNTIME_DIR)=" | tr "\n" " "' | tail -1)
GUI_ENV=${GUI_ENV:-DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/config/.XDG}
echo "gui env: $GUI_ENV"

step "build and install extension"
in_app "rm -rf /config/bh/dist && mkdir -p /config/bh/dist && $BLENDER --command extension validate /config/bh/blender_hackatime 2>&1 | tail -3"
in_app "$BLENDER --command extension build --source-dir /config/bh/blender_hackatime --output-dir /config/bh/dist 2>&1 | tail -2"
in_app "$BLENDER --command extension install-file -r user_default -e /config/bh/dist/blender_hackatime-*.zip 2>&1 | tail -2"
in_app "$BLENDER -b --python-expr 'import bpy; bpy.context.preferences.view.show_splash = False; bpy.ops.wm.save_userpref()' >/dev/null 2>&1; echo prefs saved"

step "fake hackatime"
in_app 'rm -rf /tmp/fake /tmp/wakahome && mkdir -p /tmp/fake /tmp/wakahome'
in_app 'printf "[settings]\napi_url = http://127.0.0.1:8765/api/hackatime/v1\napi_key = 6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b\nheartbeat_rate_limit_seconds = 0\n" > /tmp/wakahome/.wakatime.cfg'
docker exec -d -u abc "$CONTAINER" python3 /config/bh/tests/fake_hackatime.py --port 8765 --log /tmp/fake/heartbeats.jsonl
sleep 1
in_app 'curl -s -o /dev/null -w "fake server %{http_code}\n" http://127.0.0.1:8765/'

ENVS="WAKATIME_HOME=/tmp/wakahome FAKE_HACKATIME_LOG=/tmp/fake/heartbeats.jsonl"

step "background test"
in_app ": > /tmp/fake/heartbeats.jsonl; $ENVS TEST_ROOT=/tmp/bh-bg timeout 300 $BLENDER -b --python /config/bh/tests/blender_background.py 2>&1 | tee /tmp/background-full.log | grep -E '^(PASS|FAIL|RESULT|Traceback|  File|[A-Za-z]*Error)' "
quit_check '"lineno": 4002'
in_app 'cat /tmp/fake/heartbeats.jsonl' > "$OUT/background.jsonl"

step "gui test with listener restarts"
in_app ": > /tmp/fake/heartbeats.jsonl; $GUI_ENV $ENVS TEST_ROOT=/tmp/bh-gui LISTENER_LIFETIME=3 AUTOSAVE_WAIT=${AUTOSAVE_WAIT:-100} timeout 400 $BLENDER --enable-event-simulate --python /config/bh/tests/blender_gui.py 2>&1 | tee /tmp/gui-full.log | grep -E '^(PASS|FAIL|RESULT|FACTS|AUTOSAVE|Traceback|  File|[A-Za-z]*Error)'"
quit_check 'untitled.blend'
in_app 'cat /tmp/fake/heartbeats.jsonl' > "$OUT/gui.jsonl"

if [ "${CONTROL:-1}" = 1 ]; then
  step "control: gui test without listener restarts"
  in_app ": > /tmp/fake/heartbeats.jsonl; $GUI_ENV $ENVS TEST_ROOT=/tmp/bh-gui2 LISTENER_LIFETIME=100000 AUTOSAVE_WAIT=${AUTOSAVE_WAIT:-100} timeout 400 $BLENDER --enable-event-simulate --python /config/bh/tests/blender_gui.py 2>&1 | tee /tmp/gui-full.log | grep -E '^(PASS|FAIL|RESULT|FACTS|AUTOSAVE|Traceback|  File|[A-Za-z]*Error)'"
  quit_check 'untitled.blend'
fi
echo "=== done"
