import hashlib
import os
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import requests

logger = logging.getLogger("downloads.qbittorrent")


def test_torrent_client(client_type, url, username=None, password=None, timeout_seconds=10):
    if not url:
        return False, "Client URL is required."
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _test_qbittorrent(url, username, password, timeout_seconds)
    if client_type == "transmission":
        return _test_transmission(url, username, password, timeout_seconds)
    if client_type == "deluge":
        return _test_deluge(url, password, timeout_seconds)
    return False, "Unsupported client type."


def add_torrent(client_type, url, username=None, password=None, download_url=None, category=None, download_path=None, timeout_seconds=15, expected_name=None, update_only=False, exclude_russian=False, expected_update_number=None, expected_version=None):
    if not download_url:
        return False, "Download URL is required.", None
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _add_qbittorrent(url, username, password, download_url, category, download_path, timeout_seconds, expected_name, update_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "transmission":
        return _add_transmission(url, username, password, download_url, category, download_path, timeout_seconds, expected_name, update_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "deluge":
        return _add_deluge(url, password, download_url, category, download_path, timeout_seconds, update_only, exclude_russian, expected_update_number, expected_version)
    return False, "Unsupported client type.", None


def list_completed(client_type, url, username=None, password=None, category=None, timeout_seconds=15):
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _list_completed_qbittorrent(url, username, password, category, timeout_seconds)
    if client_type == "transmission":
        return _list_completed_transmission(url, username, password, category, timeout_seconds)
    if client_type == "deluge":
        return _list_completed_deluge(url, password, category, timeout_seconds)
    return []


def remove_torrent(client_type, url, torrent_hash, username=None, password=None, timeout_seconds=15):
    if not torrent_hash:
        return False, "Torrent hash is required."
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _remove_qbittorrent(url, username, password, torrent_hash, timeout_seconds)
    if client_type == "transmission":
        return _remove_transmission(url, username, password, torrent_hash, timeout_seconds)
    if client_type == "deluge":
        return _remove_deluge(url, password, torrent_hash, timeout_seconds)
    return False, "Unsupported client type."


def _test_qbittorrent(url, username=None, password=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        login_resp = session.post(
            f"{base}/api/v2/auth/login",
            data={"username": username or "", "password": password or ""},
            timeout=timeout_seconds,
        )
        if login_resp.status_code != 200 or login_resp.text.strip() not in ("Ok.", ""):
            return False, "qBittorrent login failed."
    version_resp = session.get(f"{base}/api/v2/app/version", timeout=timeout_seconds)
    if version_resp.status_code != 200:
        return False, f"qBittorrent returned {version_resp.status_code}."
    return True, f"qBittorrent OK (v{version_resp.text.strip()})."


def _test_transmission(url, username=None, password=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        session.auth = (username or "", password or "")

    payload = {"method": "session-get"}
    resp = session.post(
        f"{base}/transmission/rpc",
        json=payload,
        timeout=timeout_seconds,
    )
    if resp.status_code == 409:
        session_id = resp.headers.get("X-Transmission-Session-Id")
        if session_id:
            session.headers.update({"X-Transmission-Session-Id": session_id})
            resp = session.post(
                f"{base}/transmission/rpc",
                json=payload,
                timeout=timeout_seconds,
            )
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}."
    return True, "Transmission OK."


def _deluge_json_rpc(url, password, method, params=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if password is None:
        password = ""
    payload = {
        "method": method,
        "params": params or [],
        "id": 1
    }
    resp = session.post(f"{base}/json", json=payload, timeout=timeout_seconds)
    if resp.status_code != 200:
        return False, resp
    data = resp.json()
    if data.get("error"):
        return False, data
    return True, data.get("result")


def _deluge_login(url, password, timeout_seconds=10):
    ok, result = _deluge_json_rpc(url, password, "auth.login", [password], timeout_seconds=timeout_seconds)
    if not ok:
        return False, result
    return bool(result), result


def _test_deluge(url, password=None, timeout_seconds=10):
    ok, result = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not result:
        return False, "Deluge login failed."
    ok, result = _deluge_json_rpc(url, password, "daemon.info", [], timeout_seconds=timeout_seconds)
    if not ok:
        return False, "Deluge returned an error."
    version = None
    if isinstance(result, dict):
        version = result.get("daemon_version") or result.get("version")
    return True, f"Deluge OK{f' (v{version})' if version else ''}."


def _add_deluge(url, password, download_url, category, download_path, timeout_seconds, update_only, exclude_russian, expected_update_number, expected_version):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return False, "Deluge login failed.", None

    options = {}
    if update_only:
        options["add_paused"] = True
    if download_path:
        options["download_location"] = download_path

    if category:
        ok, result = _deluge_json_rpc(url, password, "label.add", [category], timeout_seconds=timeout_seconds)
        if not ok:
            err = result.get("error") if isinstance(result, dict) else None
            if not err or "already" not in str(err).lower():
                return False, "Deluge label error.", None
        options["label"] = category

    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.add_torrent_url",
        [download_url, options],
        timeout_seconds=timeout_seconds
    )
    if not ok:
        return False, "Deluge returned an error.", None
    torrent_hash = None
    if isinstance(result, str):
        torrent_hash = result
    elif isinstance(result, dict):
        torrent_hash = result.get("id")
    if not torrent_hash:
        torrent_hash = _extract_magnet_hash(download_url)

    if update_only and torrent_hash:
        file_names = None
        for _ in range(10):
            ok, status = _deluge_json_rpc(
                url,
                password,
                "core.get_torrent_status",
                [torrent_hash, ["files"]],
                timeout_seconds=timeout_seconds
            )
            if ok and isinstance(status, dict) and status.get("files"):
                files = status.get("files") or []
                file_names = [f.get("path") for f in files]
                break
            time.sleep(1)
        keep_indices = _select_update_file_indices(
            file_names or [],
            expected_update_number=expected_update_number,
            expected_version=expected_version,
            exclude_russian=exclude_russian
        )
        if not keep_indices:
            _remove_deluge(url, password, torrent_hash, timeout_seconds)
            return False, "No matching update version found in torrent.", None
        priorities = [0] * len(file_names or [])
        for idx in keep_indices:
            if idx < len(priorities):
                priorities[idx] = 1
        _deluge_json_rpc(
            url,
            password,
            "core.set_torrent_file_priorities",
            [torrent_hash, priorities],
            timeout_seconds=timeout_seconds
        )
        _deluge_json_rpc(
            url,
            password,
            "core.resume_torrent",
            [[torrent_hash]],
            timeout_seconds=timeout_seconds
        )
    return True, "Deluge accepted torrent.", torrent_hash


def _add_qbittorrent(url, username, password, download_url, category, download_path, timeout_seconds, expected_name, update_only, exclude_russian, expected_update_number, expected_version):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        login_resp = session.post(
            f"{base}/api/v2/auth/login",
            data={"username": username or "", "password": password or ""},
            timeout=timeout_seconds,
        )
        if login_resp.status_code != 200 or login_resp.text.strip() not in ("Ok.", ""):
            return False, "qBittorrent login failed.", None

    data = {"urls": download_url}
    temp_tag = None
    if update_only:
        preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if preflight_files is not None:
            keep_ids = _select_update_file_indices(
                preflight_files,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian
            )
            if not keep_ids:
                return False, "No matching update version found in torrent.", None
        temp_tag = f"ownfoil_update_{int(time.time())}_{secrets.token_hex(3)}"
    if category:
        data["category"] = category
    tags = _build_qbittorrent_tags(category, temp_tag)
    if tags:
        data["tags"] = tags
    if update_only:
        data["paused"] = "true"
    if download_path:
        data["savepath"] = download_path
    added_at = int(time.time())
    infohash_v1 = _compute_torrent_infohash(download_url, timeout_seconds)
    if update_only and infohash_v1:
        logger.info("Computed torrent infohash_v1: %s", infohash_v1)
    resp = session.post(f"{base}/api/v2/torrents/add", data=data, timeout=timeout_seconds)
    if resp.status_code != 200:
        return False, f"qBittorrent returned {resp.status_code}.", None
    torrent_hash = _extract_magnet_hash(download_url)
    if update_only and infohash_v1 and temp_tag:
        torrent_hash = _find_qbittorrent_hash_by_tag_and_infohash(
            session, base, temp_tag, infohash_v1, timeout_seconds
        )
    if update_only and infohash_v1:
        torrent_hash = _find_qbittorrent_hash_by_infohash(session, base, infohash_v1, category, added_at, timeout_seconds)
        if torrent_hash:
            logger.info("Matched torrent hash %s for infohash_v1 %s", torrent_hash, infohash_v1)
    if update_only and not torrent_hash:
        for _ in range(5):
            torrent_hash = _find_recent_qbittorrent_hash(session, base, expected_name, category, timeout_seconds, added_at)
            if torrent_hash:
                break
            time.sleep(1)
    if update_only and torrent_hash:
        normalized = _normalize_hash(session, base, torrent_hash, timeout_seconds)
        if normalized:
            torrent_hash = normalized
    if update_only and torrent_hash:
        logger.info("Selecting highest version for torrent %s", torrent_hash)
        selected = _select_qbittorrent_highest_version(
            session,
            base,
            torrent_hash,
            timeout_seconds,
            exclude_russian,
            expected_update_number=expected_update_number,
            expected_version=expected_version
        )
        if not selected:
            if temp_tag:
                _remove_qbittorrent_tag(session, base, torrent_hash, temp_tag, timeout_seconds)
            _remove_qbittorrent_with_session(session, base, torrent_hash, timeout_seconds)
            return False, "No matching update version found in torrent.", None
        _resume_qbittorrent(session, base, torrent_hash, timeout_seconds)
        if temp_tag:
            _remove_qbittorrent_tag(session, base, torrent_hash, temp_tag, timeout_seconds)
    elif update_only:
        return False, "Unable to resolve torrent hash for file selection.", None
    elif exclude_russian and torrent_hash:
        _exclude_qbittorrent_russian(session, base, torrent_hash, timeout_seconds)
    return True, "qBittorrent accepted torrent.", torrent_hash


def _add_transmission(url, username, password, download_url, category, download_path, timeout_seconds, expected_name, update_only, exclude_russian, expected_update_number, expected_version):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        session.auth = (username or "", password or "")

    preflight_files = None
    if update_only:
        preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if preflight_files is not None:
            keep_ids = _select_update_file_indices(
                preflight_files,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian
            )
            if not keep_ids:
                return False, "No matching update version found in torrent.", None

    payload = {"method": "torrent-add", "arguments": {"filename": download_url}}
    if update_only:
        payload["arguments"]["paused"] = True
    if category:
        payload["arguments"]["labels"] = [category]
    if download_path:
        payload["arguments"]["download-dir"] = download_path

    def _request(payload_body):
        resp = session.post(f"{base}/transmission/rpc", json=payload_body, timeout=timeout_seconds)
        if resp.status_code == 409:
            session_id = resp.headers.get("X-Transmission-Session-Id")
            if session_id:
                session.headers.update({"X-Transmission-Session-Id": session_id})
                resp = session.post(f"{base}/transmission/rpc", json=payload_body, timeout=timeout_seconds)
        return resp

    resp = _request(payload)
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}.", None
    data = resp.json().get("arguments", {})
    torrent = data.get("torrent-added") or data.get("torrent-duplicate") or {}
    torrent_hash = torrent.get("hashString") or _extract_magnet_hash(download_url)
    torrent_id = torrent.get("id") or torrent.get("hashString")

    if update_only and not torrent_id:
        return False, "Unable to resolve torrent id for file selection.", None
    if update_only and torrent_id:
        file_indices = None
        file_names = None
        for _ in range(10):
            info_payload = {
                "method": "torrent-get",
                "arguments": {"fields": ["id", "files", "name"], "ids": [torrent_id]}
            }
            info_resp = _request(info_payload)
            if info_resp.status_code == 200:
                torrents = info_resp.json().get("arguments", {}).get("torrents", []) or []
                if torrents and torrents[0].get("files"):
                    files = torrents[0].get("files") or []
                    file_names = [f.get("name") for f in files]
                    file_indices = _select_update_file_indices(
                        file_names,
                        expected_update_number=expected_update_number,
                        expected_version=expected_version,
                        exclude_russian=exclude_russian
                    )
                    break
            time.sleep(1)
        if not file_indices:
            if torrent_hash:
                _remove_transmission(url, username, password, torrent_hash, timeout_seconds)
            return False, "No matching update version found in torrent.", None
        all_indices = list(range(len(file_names))) if file_names else []
        unwanted = [i for i in all_indices if i not in file_indices]
        set_payload = {
            "method": "torrent-set",
            "arguments": {
                "ids": [torrent_id],
                "files-wanted": file_indices,
                "files-unwanted": unwanted
            }
        }
        _request(set_payload)
        _request({"method": "torrent-start", "arguments": {"ids": [torrent_id]}})
    return True, "Transmission accepted torrent.", torrent_hash




def _list_completed_qbittorrent(url, username, password, category, timeout_seconds):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        login_resp = session.post(
            f"{base}/api/v2/auth/login",
            data={"username": username or "", "password": password or ""},
            timeout=timeout_seconds,
        )
        if login_resp.status_code != 200 or login_resp.text.strip() not in ("Ok.", ""):
            return []

    def fetch_with_params(extra_params=None):
        params = extra_params or {}
        params["status"] = "completed"
        resp = session.get(f"{base}/api/v2/torrents/info", params=params, timeout=timeout_seconds)
        if resp.status_code != 200:
            return []
        return resp.json() or []

    items = []
    if category:
        items = fetch_with_params({"category": category})
        if not items:
            items = fetch_with_params({"tag": category})
    if not items:
        items = fetch_with_params({})
    if not items:
        return []
    completed = []
    for item in items:
        if item.get("progress") == 1:
            torrent_hash = item.get("hash")
            content_path = item.get("content_path")
            save_path = item.get("save_path")
            name = item.get("name")
            if not content_path and save_path and name:
                content_path = os.path.join(save_path.rstrip("/\\"), name)
            if torrent_hash:
                completed.append({
                    "hash": torrent_hash,
                    "path": content_path,
                    "name": name
                })
    return completed


def _list_completed_transmission(url, username, password, category, timeout_seconds):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        session.auth = (username or "", password or "")

    payload = {
        "method": "torrent-get",
        "arguments": {"fields": ["id", "hashString", "percentDone", "labels", "downloadDir", "name"]},
    }
    resp = session.post(f"{base}/transmission/rpc", json=payload, timeout=timeout_seconds)
    if resp.status_code == 409:
        session_id = resp.headers.get("X-Transmission-Session-Id")
        if session_id:
            session.headers.update({"X-Transmission-Session-Id": session_id})
            resp = session.post(f"{base}/transmission/rpc", json=payload, timeout=timeout_seconds)
    if resp.status_code != 200:
        return []
    torrents = resp.json().get("arguments", {}).get("torrents", []) or []
    completed = []
    for torrent in torrents:
        if torrent.get("percentDone") != 1:
            continue
        labels = torrent.get("labels") or []
        if category and category not in labels:
            continue
        torrent_hash = torrent.get("hashString")
        download_dir = torrent.get("downloadDir")
        name = torrent.get("name")
        content_path = None
        if download_dir and name:
            content_path = os.path.join(download_dir.rstrip("/\\"), name)
        if torrent_hash:
            completed.append({
                "hash": torrent_hash,
                "path": content_path,
                "name": name
            })
    return completed


def _list_completed_deluge(url, password, category, timeout_seconds):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return []

    state_filter = {"state": "Seeding"}
    fields = ["hash", "name", "save_path", "download_location", "state", "label"]
    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.get_torrents_status",
        [state_filter, fields],
        timeout_seconds=timeout_seconds
    )
    if not ok or not isinstance(result, dict):
        return []
    completed = []
    for torrent_hash, data in result.items():
        if not isinstance(data, dict):
            continue
        label = data.get("label")
        if category and category != label:
            continue
        path = data.get("download_location") or data.get("save_path")
        name = data.get("name")
        content_path = None
        if path and name:
            content_path = os.path.join(path.rstrip("/\\"), name)
        completed.append({
            "hash": torrent_hash,
            "path": content_path,
            "name": name
        })
    return completed


def _remove_qbittorrent(url, username, password, torrent_hash, timeout_seconds):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        login_resp = session.post(
            f"{base}/api/v2/auth/login",
            data={"username": username or "", "password": password or ""},
            timeout=timeout_seconds,
        )
        if login_resp.status_code != 200 or login_resp.text.strip() not in ("Ok.", ""):
            return False, "qBittorrent login failed."
    resp = session.post(
        f"{base}/api/v2/torrents/delete",
        data={"hashes": torrent_hash, "deleteFiles": "false"},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return False, f"qBittorrent returned {resp.status_code}."
    return True, "qBittorrent removed torrent."


def _remove_qbittorrent_with_session(session, base, torrent_hash, timeout_seconds):
    if not torrent_hash:
        return False
    resp = session.post(
        f"{base}/api/v2/torrents/delete",
        data={"hashes": torrent_hash, "deleteFiles": "false"},
        timeout=timeout_seconds,
    )
    return resp.status_code == 200


def _remove_transmission(url, username, password, torrent_hash, timeout_seconds):
    base = url.rstrip("/")
    session = requests.Session()
    session.headers.update({"User-Agent": "Ownfoil/Downloads"})
    if username or password:
        session.auth = (username or "", password or "")

    payload = {
        "method": "torrent-remove",
        "arguments": {"ids": [torrent_hash], "delete-local-data": False},
    }
    resp = session.post(f"{base}/transmission/rpc", json=payload, timeout=timeout_seconds)
    if resp.status_code == 409:
        session_id = resp.headers.get("X-Transmission-Session-Id")
        if session_id:
            session.headers.update({"X-Transmission-Session-Id": session_id})
            resp = session.post(f"{base}/transmission/rpc", json=payload, timeout=timeout_seconds)
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}."
    return True, "Transmission removed torrent."


def _remove_deluge(url, password, torrent_hash, timeout_seconds):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return False, "Deluge login failed."

    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.remove_torrent",
        [torrent_hash, False],
        timeout_seconds=timeout_seconds
    )
    if not ok:
        return False, "Deluge returned an error."
    return True, "Deluge removed torrent."


def _extract_magnet_hash(magnet_url):
    if not magnet_url:
        return None
    match = re.search(r"xt=urn:btih:([A-Fa-f0-9]+)", magnet_url)
    if match:
        return match.group(1).lower()
    match = re.search(r"xt=urn:btih:([A-Z2-7]+)", magnet_url)
    if match:
        return match.group(1).lower()
    return None


def _resume_qbittorrent(session, base, torrent_hash, timeout_seconds):
    data = {"hashes": torrent_hash} if torrent_hash else {"hashes": "all"}
    session.post(f"{base}/api/v2/torrents/resume", data=data, timeout=timeout_seconds)


def _compute_torrent_infohash(download_url, timeout_seconds):
    if not download_url or download_url.lower().startswith("magnet:"):
        return None
    try:
        resp = requests.get(download_url, timeout=timeout_seconds)
        if resp.status_code != 200:
            return None
        data = resp.content
        info_slice = _extract_info_bencode_slice(data)
        if not info_slice:
            return None
        return hashlib.sha1(info_slice).hexdigest()
    except Exception:
        return None


def _bdecode_value(data, idx):
    if idx >= len(data):
        return None, idx
    token = data[idx:idx + 1]
    if token == b"i":
        idx += 1
        end = data.find(b"e", idx)
        if end == -1:
            return None, idx
        try:
            value = int(data[idx:end])
        except Exception:
            value = None
        return value, end + 1
    if token == b"l":
        idx += 1
        items = []
        while idx < len(data) and data[idx:idx + 1] != b"e":
            item, idx = _bdecode_value(data, idx)
            items.append(item)
        return items, idx + 1
    if token == b"d":
        idx += 1
        items = {}
        while idx < len(data) and data[idx:idx + 1] != b"e":
            key, idx = _bdecode_bytes(data, idx)
            if key is None:
                return None, idx
            value, idx = _bdecode_value(data, idx)
            items[key] = value
        return items, idx + 1
    if token.isdigit():
        value, next_idx = _bdecode_bytes(data, idx)
        return value, next_idx
    return None, idx


def _decode_torrent_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("latin-1", errors="ignore")
    return str(value)


def _get_torrent_file_list(download_url, timeout_seconds):
    if not download_url or download_url.lower().startswith("magnet:"):
        return None
    try:
        resp = requests.get(download_url, timeout=timeout_seconds)
        if resp.status_code != 200:
            return None
        data = resp.content
        metadata, _ = _bdecode_value(data, 0)
        if not isinstance(metadata, dict):
            return None
        info = metadata.get(b"info")
        if not isinstance(info, dict):
            return None
        files = info.get(b"files")
        if isinstance(files, list):
            file_list = []
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                path_parts = entry.get(b"path.utf-8") or entry.get(b"path") or []
                if not isinstance(path_parts, list):
                    continue
                decoded_parts = [_decode_torrent_text(p) for p in path_parts if p is not None]
                if decoded_parts:
                    file_list.append("/".join(decoded_parts))
            return file_list
        name = info.get(b"name.utf-8") or info.get(b"name")
        if name:
            return [_decode_torrent_text(name)]
    except Exception:
        return None
    return None


def _select_update_file_indices(file_names, expected_update_number=None, expected_version=None, exclude_russian=False):
    if not file_names:
        return []
    version_map = []
    for idx, name in enumerate(file_names):
        name = name or ""
        lowered = name.lower()
        if exclude_russian and ("russian" in lowered or "rus" in lowered):
            continue
        match = re.search(r"\[v(\d+)\]", name, re.IGNORECASE)
        if match:
            try:
                version_map.append((int(match.group(1)), idx))
            except ValueError:
                continue
    if not version_map:
        return []
    expected_value = None
    if expected_version is not None:
        try:
            expected_value = int(expected_version)
        except (TypeError, ValueError):
            expected_value = None
    if expected_value and expected_value > 0:
        return [idx for version, idx in version_map if version == expected_value]
    if expected_update_number is not None and expected_update_number > 0:
        return [idx for version, idx in version_map if version == expected_update_number]
    highest = max(version_map, key=lambda pair: pair[0])[0]
    return [idx for version, idx in version_map if version == highest]


def _extract_info_bencode_slice(data):
    if not data:
        return None
    idx = 0
    if data[idx:idx + 1] != b"d":
        return None
    idx += 1
    while idx < len(data) and data[idx:idx + 1] != b"e":
        key, idx = _bdecode_bytes(data, idx)
        if key is None:
            return None
        if key == b"info":
            start = idx
            idx = _bdecode_skip(data, idx)
            return data[start:idx]
        idx = _bdecode_skip(data, idx)
    return None


def _bdecode_bytes(data, idx):
    if idx >= len(data) or data[idx:idx + 1].isdigit() is False:
        return None, idx
    length = 0
    while idx < len(data) and data[idx:idx + 1].isdigit():
        length = length * 10 + (data[idx] - 48)
        idx += 1
    if idx >= len(data) or data[idx:idx + 1] != b":":
        return None, idx
    idx += 1
    end = idx + length
    if end > len(data):
        return None, idx
    return data[idx:end], end


def _bdecode_skip(data, idx):
    if idx >= len(data):
        return idx
    token = data[idx:idx + 1]
    if token == b"i":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            idx += 1
        return idx + 1
    if token == b"l":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            idx = _bdecode_skip(data, idx)
        return idx + 1
    if token == b"d":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            _, idx = _bdecode_bytes(data, idx)
            idx = _bdecode_skip(data, idx)
        return idx + 1
    if token.isdigit():
        _, idx = _bdecode_bytes(data, idx)
        return idx
    return idx


def _find_qbittorrent_hash_by_infohash(session, base, infohash_v1, category, added_after, timeout_seconds):
    resp = session.get(
        f"{base}/api/v2/torrents/info",
        params={"sort": "added_on", "reverse": "true"},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return None
    items = resp.json() or []
    matches = []
    candidates = []
    for item in items:
        if category:
            if item.get("category") != category and category not in (item.get("tags") or "").split(","):
                continue
        added_on = int(item.get("added_on") or 0)
        if added_after and added_on < added_after:
            continue
        candidates.append({
            "hash": item.get("hash"),
            "infohash_v1": item.get("infohash_v1"),
            "name": item.get("name"),
            "added_on": added_on
        })
        if (item.get("infohash_v1") or "").lower() == infohash_v1.lower():
            matches.append(item)
        elif (item.get("hash") or "").lower() == infohash_v1.lower():
            matches.append(item)
    if matches:
        matches.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return matches[0].get("hash")
    if candidates:
        logger.info("No hash match for infohash_v1. Candidates: %s", candidates)
    return None


def _normalize_hash(session, base, torrent_hash, timeout_seconds):
    if not torrent_hash:
        return None
    resp = session.get(
        f"{base}/api/v2/torrents/info",
        params={"hashes": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return None
    items = resp.json() or []
    if not items:
        return None
    return items[0].get("hash") or torrent_hash


def _build_qbittorrent_tags(category, temp_tag):
    tags = []
    if category:
        tags.append(category)
    if temp_tag:
        tags.append(temp_tag)
    return ",".join(tags)


def _find_qbittorrent_hash_by_tag(session, base, tag, timeout_seconds):
    if not tag:
        return None
    resp = session.get(
        f"{base}/api/v2/torrents/info",
        params={"tag": tag},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return None
    items = resp.json() or []
    if not items:
        return None
    items.sort(key=lambda item: item.get("added_on", 0), reverse=True)
    return items[0].get("hash")


def _remove_qbittorrent_tag(session, base, torrent_hash, tag, timeout_seconds):
    if not torrent_hash or not tag:
        return
    session.post(
        f"{base}/api/v2/torrents/removeTags",
        data={"hashes": torrent_hash, "tags": tag},
        timeout=timeout_seconds,
    )


def _find_qbittorrent_hash_by_tag_and_infohash(session, base, tag, infohash_v1, timeout_seconds):
    if not tag or not infohash_v1:
        return None
    deadline = time.time() + 6
    infohash_lower = infohash_v1.lower()
    while time.time() < deadline:
        resp = session.get(
            f"{base}/api/v2/torrents/info",
            params={"tag": tag, "sort": "added_on", "reverse": "true"},
            timeout=timeout_seconds,
        )
        if resp.status_code == 200:
            items = resp.json() or []
            for item in items:
                if (item.get("infohash_v1") or "").lower() == infohash_lower:
                    return item.get("hash")
        time.sleep(1)
    return None


def _find_recent_qbittorrent_hash(session, base, expected_name, category, timeout_seconds, added_after=None):
    expected = (expected_name or "").lower()
    expected_terms = [term for term in re.split(r"\s+", expected) if len(term) > 2]
    candidates = []

    def fetch(params):
        resp = session.get(f"{base}/api/v2/torrents/info", params=params, timeout=timeout_seconds)
        if resp.status_code != 200:
            return []
        return resp.json() or []

    if category:
        candidates = fetch({"category": category, "sort": "added_on", "reverse": "true", "limit": 5})
        if not candidates:
            candidates = fetch({"tag": category, "sort": "added_on", "reverse": "true", "limit": 5})
    if not candidates:
        candidates = fetch({"sort": "added_on", "reverse": "true", "limit": 5})

    matches = []
    for item in candidates:
        name = (item.get("name") or "").lower()
        added_on = int(item.get("added_on") or 0)
        if added_after and added_on < added_after:
            continue
        if expected and expected in name:
            matches.append(item)
        elif expected_terms and all(term in name for term in expected_terms):
            matches.append(item)
    if matches:
        matches.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return matches[0].get("hash")
    if candidates:
        candidates.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return candidates[0].get("hash")
    return None


def _select_qbittorrent_highest_version(session, base, torrent_hash, timeout_seconds, exclude_russian, expected_update_number=None, expected_version=None):
    resp = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        logger.warning("Failed to fetch torrent files for %s: %s", torrent_hash, resp.status_code)
        return
    files = resp.json() or []
    logger.info("Torrent %s file list entries: %s", torrent_hash, len(files))
    version_map = []
    all_ids = []
    for file in files:
        name = file.get("name") or ""
        lowered = name.lower()
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        all_ids.append(str(file_id))
        if exclude_russian and ("russian" in lowered or "rus" in lowered):
            continue
        match = re.search(r"\[v(\d+)\]", name, re.IGNORECASE)
        if match:
            try:
                version_map.append((int(match.group(1)), file_id))
            except ValueError:
                continue
    if not version_map:
        logger.warning("No version tags found in torrent %s file list.", torrent_hash)
        return False
    expected_version_value = None
    if expected_version is not None:
        try:
            expected_version_value = int(expected_version)
        except (TypeError, ValueError):
            expected_version_value = None
    if expected_version_value and expected_version_value > 0:
        keep_ids = [str(file_id) for version, file_id in version_map if version == expected_version_value]
        if not keep_ids:
            logger.warning(
                "No update files found for expected version v%s in torrent %s.",
                expected_version_value,
                torrent_hash
            )
            return False
    elif expected_update_number is not None and expected_update_number > 0:
        keep_ids = [str(file_id) for version, file_id in version_map if version == expected_update_number]
        if not keep_ids:
            logger.warning(
                "No update files found for expected update number v%s in torrent %s.",
                expected_update_number,
                torrent_hash
            )
            return False
    else:
        keep_ids = [str(file_id) for version, file_id in version_map if version > 0]
    if not keep_ids:
        logger.warning("No update files found (v>0) in torrent %s.", torrent_hash)
        return False
    keep_set = set(keep_ids)
    if all_ids:
        disable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, all_ids, 0, timeout_seconds)
        logger.info("Disable all files response: %s", disable_resp)
    if keep_ids:
        enable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, keep_ids, 1, timeout_seconds)
        logger.info("Enable file ids %s response: %s", "|".join(keep_ids), enable_resp)

    verify = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if verify.status_code != 200:
        logger.warning("Failed to verify file priorities for %s: %s", torrent_hash, verify.status_code)
        return
    files_after = verify.json() or []
    retry_disable = []
    retry_enable = []
    for file in files_after:
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        file_id_str = str(file_id)
        priority = file.get("priority")
        if file_id_str in keep_set:
            if priority != 1:
                retry_enable.append(file_id_str)
        else:
            if priority != 0:
                retry_disable.append(file_id_str)
    if retry_disable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_disable, 0, timeout_seconds, per_file=True)
    if retry_enable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_enable, 1, timeout_seconds, per_file=True)
    return True


def _set_qbittorrent_file_priority(session, base, torrent_hash, ids, priority, timeout_seconds, per_file=False):
    if not ids:
        return None
    if per_file:
        statuses = []
        for file_id in ids:
            resp = session.post(
                f"{base}/api/v2/torrents/filePrio",
                data={"hash": torrent_hash, "id": str(file_id), "priority": priority},
                timeout=timeout_seconds,
            )
            statuses.append(resp.status_code)
        return statuses
    resp = session.post(
        f"{base}/api/v2/torrents/filePrio",
        data={"hash": torrent_hash, "id": "|".join(ids), "priority": priority},
        timeout=timeout_seconds,
    )
    return resp.status_code


def _exclude_qbittorrent_russian(session, base, torrent_hash, timeout_seconds):
    resp = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return
    files = resp.json() or []
    drop_ids = []
    for file in files:
        name = (file.get("name") or "").lower()
        if "russian" in name or "rus" in name:
            file_id = file.get("index")
            if file_id is None:
                file_id = file.get("id")
            if file_id is None:
                continue
            drop_ids.append(str(file_id))
    if drop_ids:
        _set_qbittorrent_file_priority(session, base, torrent_hash, drop_ids, 0, timeout_seconds, per_file=True)
