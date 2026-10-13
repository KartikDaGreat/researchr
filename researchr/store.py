"""Tiny JSON key-value store: local files for dev, private Vercel Blob when deployed.

Keys look like "runs/abc123/state.json". Blob reads bypass the cache (?cache=0) so a value
written by one function invocation is visible to the next.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from . import config


class LocalStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get(self, key: str):
        p = self.root / key
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, value) -> None:
        p = self._p(key)
        data = json.dumps(value, ensure_ascii=False)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        for attempt in range(8):  # OneDrive/antivirus can briefly lock the target on Windows
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.15 * (attempt + 1))
        p.write_text(data, encoding="utf-8")
        tmp.unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        base = self.root / prefix
        if base.is_dir():
            for f in sorted(base.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            base.rmdir()
        elif base.exists():
            base.unlink()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [str(p.relative_to(self.root)).replace("\\", "/") for p in base.rglob("*.json")]


class BlobStore:
    API = "https://vercel.com/api/blob"

    def __init__(self, token: str, prefix: str = ""):
        self.token = token
        self.store_id = token.split("_")[3] if token.count("_") >= 3 else ""
        self.prefix = prefix.strip("/") + "/" if prefix else ""
        self.http = httpx.Client(timeout=30)

    def _h(self, extra: dict | None = None) -> dict:
        h = {"authorization": f"Bearer {self.token}", "x-api-version": "12"}
        if self.store_id:
            h["x-vercel-blob-store-id"] = self.store_id
        return {**h, **(extra or {})}

    def get(self, key: str):
        url = f"https://{self.store_id}.private.blob.vercel-storage.com/{quote(self.prefix + key)}?cache=0"
        for attempt in range(3):
            try:
                r = self.http.get(url, headers={"authorization": f"Bearer {self.token}"})
            except httpx.HTTPError:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return None
            time.sleep(0.5 * (attempt + 1))
        return None

    def put(self, key: str, value) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        headers = self._h({
            "x-vercel-blob-access": "private", "x-add-random-suffix": "0", "x-allow-overwrite": "1",
            "x-content-type": "application/json", "x-cache-control-max-age": "60",
        })
        last = ""
        for attempt in range(3):
            try:
                r = self.http.put(f"{self.API}/?pathname={quote(self.prefix + key)}", content=body, headers=headers)
                if r.status_code < 300:
                    return
                last = f"{r.status_code} {r.text[:200]}"
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"blob put failed for {key}: {last}")

    def list(self, prefix: str) -> list[str]:
        out, cursor = [], None
        while True:
            params = {"prefix": self.prefix + prefix, "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            r = self.http.get(f"{self.API}/", params=params, headers=self._h())
            r.raise_for_status()
            data = r.json()
            out += [b["pathname"][len(self.prefix):] for b in data.get("blobs", [])]
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return out

    def delete_prefix(self, prefix: str) -> None:
        r = self.http.get(f"{self.API}/", params={"prefix": self.prefix + prefix, "limit": "1000"}, headers=self._h())
        urls = [b["url"] for b in r.json().get("blobs", [])] if r.status_code == 200 else []
        if urls:
            self.http.post(f"{self.API}/delete", json={"urls": urls},
                           headers=self._h({"content-type": "application/json"}))


def _make():
    # Deployed: always Blob. Locally: files, unless USE_BLOB=1 (then local runs show up on the hosted site too).
    tok = os.getenv("BLOB_READ_WRITE_TOKEN")
    if tok and (config.ON_VERCEL or os.getenv("USE_BLOB") == "1"):
        return BlobStore(tok, os.getenv("BLOB_PREFIX", "researchr"))
    return LocalStore(config.DATA_DIR / "store")


store = _make()
