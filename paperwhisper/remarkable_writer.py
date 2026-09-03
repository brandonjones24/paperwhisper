"""Write a reading position back into rmfakecloud via its sync 1.5 API.

This implements just enough of the reMarkable "sync15" client to change one
document's ``lastOpenedPage``:

  1. exchange the device token for a user token
  2. GET /sync/v3/root                     -> {hash, generation}
  3. walk root index -> doc index -> .metadata / .content leaf blobs
  4. rewrite the leaf(s) with the new lastOpenedPage, recompute the Merkle
     rollup up to a new root, PUT every new blob
  5. PUT /sync/v3/root with a compare-and-swap on the generation (+Broadcast)

The rollup hash algorithm (verified to reproduce the live root exactly):
  directory_hash = sha256( concat of hex-decoded child hashes, children
                           sorted by entry name )
  leaf_hash      = sha256( file bytes )

Line format:  ``hash:type:name:subfiles:size``   (type 0=file, 80000000=doc)
Index blob:   ``"3\\n"`` followed by one line per child.

This module ONLY touches the single document you target. It is the one part of
paperwhisper that writes to rmfakecloud; everything else is read-only.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import logging
import time

import requests

log = logging.getLogger("paperwhisper.rmwriter")

SCHEMA_VERSION = "3"
DOC_TYPE = "80000000"
FILE_TYPE = "0"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rollup(children: list[tuple[str, str]]) -> str:
    """children: list of (hash, name). Sort by name; sha256 of concatenated
    hex-decoded hashes."""
    h = hashlib.sha256()
    for chash, _name in sorted(children, key=lambda e: e[1]):
        h.update(binascii.unhexlify(chash))
    return h.hexdigest()


def _index_hash(schema: str, body: bytes, entries: list["Entry"]) -> str:
    """Hash for an index blob.

    Verified empirically against a live store:
      * schema 3 (document index) -> Merkle rollup of the child hashes
      * schema 4 (root index)     -> sha256 of the raw index content
    """
    if schema == "4":
        return _sha256_hex(body)
    return _rollup([(e.hash, e.name) for e in entries])


class Entry:
    __slots__ = ("hash", "type", "name", "subfiles", "size")

    def __init__(self, hash_, type_, name, subfiles, size):
        self.hash = hash_
        self.type = type_
        self.name = name
        self.subfiles = int(subfiles)
        self.size = int(size)

    def line(self) -> str:
        return f"{self.hash}:{self.type}:{self.name}:{self.subfiles}:{self.size}"


def _parse_index(text: str) -> tuple[str, list[Entry]]:
    """Return ``(schema_version, entries)``.

    Schema 3 (document index):  "3\\n" then file lines.
    Schema 4 (root index):      "4\\n" then a summary line ``0:.:<count>:<size>``
                                then document lines.
    The summary line (4 colon-fields, name ".") is skipped here and regenerated
    on serialize.
    """
    lines = text.splitlines()
    schema = lines[0].strip() if lines and lines[0].strip().isdigit() else SCHEMA_VERSION
    entries = []
    for ln in lines[1:]:
        ln = ln.strip()
        if not ln:
            continue
        p = ln.split(":")
        if len(p) == 4 and p[1] == ".":
            continue  # schema-4 summary line, regenerated on serialize
        if len(p) >= 5:
            entries.append(Entry(p[0], p[1], p[2], p[3], p[4]))
    return schema, entries


def _serialize_index(schema: str, entries: list[Entry]) -> bytes:
    """Serialize preserving the source schema. Schema 4 gets the regenerated
    ``0:.:<count>:<total_size>`` summary line; schema 3 does not."""
    entries = sorted(entries, key=lambda e: e.name)
    if schema == "4":
        total = sum(e.size for e in entries)
        header = f"4\n0:.:{len(entries)}:{total}\n"
    else:
        header = schema + "\n"
    body = header + "".join(e.line() + "\n" for e in entries)
    return body.encode()


class RemarkableSyncWriter:
    def __init__(self, base_url: str, device_token: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.device_token = device_token
        self.timeout = timeout
        self.session = requests.Session()
        self.user_token: str | None = None

    # -- auth ------------------------------------------------------------------

    def authenticate(self) -> None:
        r = self.session.post(
            f"{self.base}/token/json/2/user/new",
            headers={"Authorization": f"Bearer {self.device_token}", "Content-Length": "0"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        self.user_token = r.text.strip()
        if not self.user_token:
            raise RuntimeError("empty user token from rmfakecloud")

    def _auth_headers(self) -> dict:
        if not self.user_token:
            self.authenticate()
        return {"Authorization": f"Bearer {self.user_token}"}

    # -- blob / root primitives ------------------------------------------------

    def get_root(self) -> tuple[str, int]:
        r = self.session.get(f"{self.base}/sync/v3/root", headers=self._auth_headers(), timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        return d["hash"], int(d["generation"])

    def get_blob(self, blob_hash: str) -> bytes:
        r = self.session.get(
            f"{self.base}/sync/v3/files/{blob_hash}", headers=self._auth_headers(), timeout=self.timeout
        )
        r.raise_for_status()
        return r.content

    def put_blob(self, blob_hash: str, content: bytes, filename: str = "") -> None:
        headers = dict(self._auth_headers())
        if filename:
            headers["rm-filename"] = filename
        headers["x-goog-hash"] = f"crc32c=;md5="  # server only logs this
        r = self.session.put(
            f"{self.base}/sync/v3/files/{blob_hash}", headers=headers, data=content, timeout=self.timeout
        )
        r.raise_for_status()

    def update_root(self, new_hash: str, generation: int, broadcast: bool = True) -> int:
        r = self.session.put(
            f"{self.base}/sync/v3/root",
            headers=self._auth_headers(),
            json={"hash": new_hash, "generation": generation, "broadcast": broadcast},
            timeout=self.timeout,
        )
        if r.status_code == 412 or r.status_code == 409:
            raise ConflictError(f"generation conflict updating root (gen={generation})")
        r.raise_for_status()
        return int(r.json().get("generation", generation + 1))

    # -- high level: set reading position --------------------------------------

    def set_reading_position(self, doc_id: str, page: int, also_content: bool = True) -> bool:
        """Set ``lastOpenedPage`` for the document with the given uuid.

        Returns True if a write was performed. Raises ConflictError on a
        generation clash (caller should re-read and retry)."""
        root_hash, generation = self.get_root()
        root_schema, root_entries = _parse_index(self.get_blob(root_hash).decode(errors="replace"))

        doc_entry = next((e for e in root_entries if e.name == doc_id), None)
        if doc_entry is None:
            log.warning("doc %s not found in root index", doc_id)
            return False

        doc_schema, file_entries = _parse_index(self.get_blob(doc_entry.hash).decode(errors="replace"))
        targets = [".metadata"] + ([".content"] if also_content else [])
        changed = False

        for ext in targets:
            fe = next((e for e in file_entries if e.name.endswith(ext)), None)
            if fe is None:
                continue
            data = json.loads(self.get_blob(fe.hash).decode())
            if int(data.get("lastOpenedPage", -1)) == page:
                continue
            data["lastOpenedPage"] = page
            if ext == ".metadata":
                data["lastModified"] = str(int(time.time() * 1000))
            new_bytes = json.dumps(data).encode()
            new_hash = _sha256_hex(new_bytes)
            self.put_blob(new_hash, new_bytes, filename=fe.name)
            fe.hash = new_hash
            fe.size = len(new_bytes)
            changed = True

        if not changed:
            log.info("doc %s already at page %d; nothing to write", doc_id, page)
            return False

        # rebuild the doc index blob (schema 3 -> hashed by Merkle rollup of files)
        doc_body = _serialize_index(doc_schema, file_entries)
        new_doc_hash = _index_hash(doc_schema, doc_body, file_entries)
        self.put_blob(new_doc_hash, doc_body, filename=doc_id + ".docSchema")
        doc_entry.hash = new_doc_hash
        doc_entry.size = sum(e.size for e in file_entries)

        # rebuild the root index blob (schema 4 -> keeps summary line, hashed by content)
        root_body = _serialize_index(root_schema, root_entries)
        new_root_hash = _index_hash(root_schema, root_body, root_entries)
        self.put_blob(new_root_hash, root_body, filename="root.docSchema")

        new_gen = self.update_root(new_root_hash, generation, broadcast=True)
        log.info("wrote lastOpenedPage=%d to doc %s; root %s->%s gen %d->%d",
                 page, doc_id, root_hash[:8], new_root_hash[:8], generation, new_gen)
        return True


class ConflictError(RuntimeError):
    pass
