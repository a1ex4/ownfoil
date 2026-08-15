"""The recording shop the clients connect to during a capture.

Runs the real app - same routes, same client identification, same handlers - against an
isolated config dir and a fixture library, with request/response hooks bolted on from the
outside so nothing in app/ has to know about capturing.

Import only after OWNFOIL_CONFIG_DIR is set: app/constants.py reads it at import time.
"""
import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

import fixture
import sanitize
from scenarios import BASELINE

SEP = "─" * 78


class CaptureServer:
    """Serves the fixture shop and records every exchange, one scenario at a time."""

    def __init__(self, workdir):
        from constants import CONFIG_DIR

        # Seeding wipes libraries and creates accounts, so refuse to run anywhere but the
        # capture workdir - an app import before OWNFOIL_CONFIG_DIR is set lands on the
        # real install, and by then the damage is a delete away.
        if os.path.realpath(CONFIG_DIR) != os.path.realpath(os.path.join(workdir, "config")):
            raise RuntimeError(f"config dir is {CONFIG_DIR}, not the capture workdir - "
                               "app was imported before OWNFOIL_CONFIG_DIR was set")

        from app import app
        from db import init_db

        self.workdir = workdir
        self.app = app
        self.library_root = os.path.join(workdir, "library")
        self.sanitizer = sanitize.Sanitizer(
            path=os.path.join(workdir, "sanitizer-map.json"),
            # The unknown user and the wrong password are fixture constants too: keeping them
            # means a capture of a refusal still says which credentials were refused.
            users=set(fixture.PASSWORDS) | {fixture.UNKNOWN_USER},
            passwords=set(fixture.PASSWORDS.values()) | {fixture.WRONG_PASSWORD},
        )
        self._lock = threading.Lock()
        self._records = []
        self._counts = {}

        init_db(app)
        fixture.build_library(self.library_root)
        self._seed()
        self._attach_hooks()

    def _seed(self):
        from db import Libraries, Titles, db
        from settings import settings_transaction

        with self.app.app_context():
            # A rerun without --fresh inherits the previous run's rows, and the fixture is
            # the whole shop: start from an empty library. Files and apps follow their
            # parents through the foreign keys.
            Libraries.query.delete()
            Titles.query.delete()
            db.session.commit()
            fixture.seed_users()
            fixture.seed_library(self.library_root)
            self._counts = self._download_counts()
        # Replace rather than add: the default /games doesn't exist here, and the watcher
        # complaining about it is noise in the operator's console.
        with settings_transaction() as settings:
            settings["library"]["paths"] = [self.library_root]

    def _download_counts(self):
        from db import Files

        return {f.filename: f.download_count for f in Files.query.all()}

    # ==================== Scenario control ====================

    def apply(self, scenario, client):
        """Reset the shop to the baseline, apply the scenario, and arm the recorder."""
        from settings import set_shop_settings

        settings = copy.deepcopy(BASELINE)
        _merge(settings, scenario.shop)
        if scenario.disable_client:
            settings["clients"][client]["enabled"] = False
        set_shop_settings(settings)

        with self._lock:
            self._records = []

    def write(self, scenario, client, out_dir):
        """Write everything recorded since apply() to tests/captures/<client>/<name>.json."""
        from settings import get_settings

        with self._lock:
            records = list(self._records)

        shop = json.loads(self.sanitizer.text(json.dumps(get_settings()["shop"])))
        capture = {
            "client": client,
            "scenario": scenario.name,
            "title": scenario.title,
            "expect": scenario.expect,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "shop_settings": shop,
            "users": [{k: v for k, v in u.items() if k != "password"} for u in fixture.USERS],
            "exchanges": records,
        }
        os.makedirs(os.path.join(out_dir, client), exist_ok=True)
        path = os.path.join(out_dir, client, f"{scenario.name}.json")
        with open(path, "w") as f:
            json.dump(capture, f, indent=2)
            f.write("\n")
        self.sanitizer.save()
        return path, len(records)

    def serve(self, host, port):
        thread = threading.Thread(
            target=lambda: self.app.run(host=host, port=port, threaded=True,
                                        debug=False, use_reloader=False),
            daemon=True)
        thread.start()
        time.sleep(0.5)
        return thread

    # ==================== Recording ====================

    def _attach_hooks(self):
        from flask import g, request

        @self.app.before_request
        def _start():
            g.capture_start = time.monotonic()

        @self.app.after_request
        def _record(response):
            record = {
                "request": {
                    "method": request.method,
                    "path": request.path,
                    "query": request.query_string.decode(),
                    "scheme": request.scheme,
                    "remote_addr": self.sanitizer.address(request.remote_addr),
                    "headers": self.sanitizer.headers(list(request.headers.items())),
                },
                "response": {
                    "status": response.status_code,
                    "headers": self.sanitizer.headers(list(response.headers.items())),
                    "body": self._body(response),
                },
                "downloads": self._download_delta(),
                "duration_ms": round((time.monotonic() - g.capture_start) * 1000, 1),
            }
            with self._lock:
                self._records.append(record)
            self._print(record)
            return response

    def _body(self, response):
        """Record the body in whatever form is useful; never pull a served file into memory."""
        content_type = response.headers.get("Content-Type", "")
        if response.direct_passthrough:
            return {"kind": "file", "length": response.headers.get("Content-Length")}

        data = response.get_data()
        if "json" in content_type:
            return {"kind": "json", "json": json.loads(self.sanitizer.text(data.decode()))}
        if content_type.startswith("text/") or "html" in content_type:
            return {"kind": "text", "text": self.sanitizer.text(data.decode("utf-8", "replace"))}
        return {
            "kind": "binary",
            "length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "head_hex": data[:16].hex(),
        }

    def _download_delta(self):
        """Which files this request counted a download for - the throttle makes it interesting."""
        counts = self._download_counts()
        delta = {name: counts[name] for name, before in self._counts.items()
                 if counts.get(name, before) != before}
        self._counts = counts
        return delta

    def _print(self, record):
        request, response = record["request"], record["response"]
        query = f"?{request['query']}" if request["query"] else ""
        print(f"\n{SEP}\n>>> {request['method']} {request['path']}{query}")
        for name, value in request["headers"]:
            print(f"    {name}: {value}")
        body = response["body"]
        summary = body.get("kind")
        if summary == "file":
            summary = f"file, {body['length']} bytes"
        elif summary == "binary":
            summary = f"binary, {body['length']} bytes, starts {body['head_hex'][:14]}"
        print(f"<<< {response['status']} ({summary}, {record['duration_ms']}ms)")
        for name, value in response["headers"]:
            print(f"    {name}: {value}")
        if record["downloads"]:
            print(f"    download_count -> {record['downloads']}")


def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target
