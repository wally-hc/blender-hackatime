import argparse
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()


def make_handler(log_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            try:
                payload = json.loads(body or b"null")
            except ValueError:
                self.reply(400, {"error": "invalid json"})
                return
            heartbeats = payload if isinstance(payload, list) else [payload]
            with lock, open(log_path, "a", encoding="utf-8") as log:
                for heartbeat in heartbeats:
                    log.write(json.dumps({
                        "path": self.path,
                        "user_agent": self.headers.get("User-Agent"),
                        "gzip": self.headers.get("Content-Encoding", "").lower() == "gzip",
                        "heartbeat": heartbeat,
                    }) + "\n")
            if self.path.endswith("heartbeats.bulk"):
                self.reply(202, {"responses": [[{"data": h}, 201] for h in heartbeats]})
            else:
                self.reply(201, {"data": heartbeats[0] if heartbeats else None})

        def do_GET(self):
            self.reply(200, {"data": {}})

        def reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.log))
    print(f"listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
