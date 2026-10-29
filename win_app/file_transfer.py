"""Files dragged across the edge, on a connection of their own.

They never share the input link: a large file there would hold every keystroke
and pointer move queued behind it. The sender opens a second connection to the
same port with the same preamble and keys, and says hello with channel "files".
Then, for each file, one `file` message naming it and giving its size, followed
by its contents as raw sealed frames of up to CHUNK_BYTES, and a `files_end`
once all are sent. The frame counter already rejects a chunk dropped, replayed
or reordered, and the tag a chunk altered, so there is no checksum on top.

A dragged folder goes as the files inside it, each named by its path from the
folder's parent with "/" between the parts. Empty folders are not sent.

The hello's `then` says what the receiver does once they have landed: THEN_REVEAL
shows them, moved first into the folder window the button came up over if there
is one; THEN_OPEN opens them, for a document whose window was carried across.

This file is byte-identical in mac_app and win_app, like protocol.py.
"""

import os
import re
import shutil

import protocol


CHANNEL = "files"
THEN_REVEAL = "reveal"
THEN_OPEN = "open"
MSG_FILE = "file"
MSG_END = "files_end"
CHUNK_BYTES = 1024 * 1024
# A sealed chunk is the chunk, its 4-byte counter and its 16-byte tag.
CHUNK_FRAME_LIMIT = CHUNK_BYTES + 64
NAME_MAX_CHARS = 4096
# Characters Windows will not have in a name. A Mac name may hold any of them.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{n}" for n in range(1, 10)} | {f"LPT{n}" for n in range(1, 10)}


def entries(paths):
    """(item, name, path) for every file to send: `item` the index of the dragged
    path it belongs to, `name` its path from that one's parent, "/"-separated."""
    for item, path in enumerate(paths):
        path = os.path.abspath(path)
        parent = os.path.dirname(path)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for file_name in sorted(files):
                    full = os.path.join(root, file_name)
                    if os.path.isfile(full):
                        yield item, os.path.relpath(full, parent).replace(os.sep, "/"), full
        elif os.path.isfile(path):
            yield item, os.path.basename(path), path


def send(sock, session, paths) -> int:
    """Sends every file under `paths` and answers how many went."""
    count = 0
    for item, name, full in entries(paths):
        with open(full, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            protocol.send_msg(sock, session, {"type": MSG_FILE, "data": {"item": item, "name": name, "size": size}})
            left = size
            while left > 0:
                chunk = handle.read(min(CHUNK_BYTES, left))
                if not chunk:
                    raise OSError(f"{full} got shorter while it was being sent")
                protocol.send_bytes(sock, session, chunk)
                left -= len(chunk)
        count += 1
    protocol.send_msg(sock, session, {"type": MSG_END, "data": {"count": count}})
    return count


# The types a carried document is opened as on arrival: everyday documents, pictures and media,
# none of which runs code when opened. Anything else, an unknown type or none, is shown in
# Explorer or Finder instead, whatever the other machine asked.
_OPENS_INERT = {
    ".pdf", ".txt", ".md", ".rtf", ".csv", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    ".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".tif", ".tiff", ".bmp",
    ".mp3", ".m4a", ".wav", ".mp4", ".mov",
}


def safe_to_open(path: str) -> bool:
    """Whether a received file may be opened rather than shown: a file of an inert type."""
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in _OPENS_INERT


def deliver(socket_factory, endpoint, paths, then=THEN_REVEAL, connect_timeout=1.0, io_timeout=30.0) -> int:
    """Opens a file connection to `endpoint` (host, port, token), sends `paths` and closes it.
    Answers how many files went; raises OSError or ProtocolError when they did not."""
    host, port, token = endpoint
    sock = socket_factory((host, port), timeout=connect_timeout)
    try:
        sock.settimeout(io_timeout)
        session = protocol.SecureSession(token)
        sock.sendall(session.preamble())
        protocol.recv_preamble(sock, session)
        hello = protocol.hello_msg(channel=CHANNEL)
        hello["data"]["then"] = then
        protocol.send_msg(sock, session, hello)
        reply = protocol.recv_msg(sock, session)
        if reply.get("type") != protocol.MSG_WELCOME or not (reply.get("data") or {}).get("files"):
            raise protocol.ProtocolError("the other machine did not take the files")
        return send(sock, session, paths)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def move_into(paths, folder: str) -> list:
    """Moves each of `paths` into `folder` under a name nothing there has, and answers where
    each went. One that cannot be moved stays where it was."""
    moved = []
    for path in paths:
        target = os.path.join(folder, unique_name(folder, os.path.basename(path), set()))
        try:
            shutil.move(path, target)
            moved.append(target)
        except OSError:
            moved.append(path)
    return moved


def clean_part(part: str) -> str:
    """One part of a sent name as a name both systems take, or "" for none."""
    part = _UNSAFE.sub("_", part).rstrip(" .")
    if part.split(".")[0].upper() in _RESERVED:
        part = "_" + part
    return part


def clean_name(name) -> list:
    """The parts of a sent name, each safe to put under the folder files land in.
    Raises ProtocolError for a name that would leave it: absolute, "..", empty."""
    if not isinstance(name, str) or not name or len(name) > NAME_MAX_CHARS:
        raise protocol.ProtocolError("file name missing or too long")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise protocol.ProtocolError(f"file name {name!r} does not stay in its folder")
    cleaned = [clean_part(part) for part in parts]
    if not all(cleaned):
        raise protocol.ProtocolError(f"file name {name!r} has an empty part")
    return cleaned


def unique_name(folder: str, name: str, taken) -> str:
    """`name`, or "name (2).ext" and on, the first that is neither in `folder` nor `taken`."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while candidate in taken or os.path.lexists(os.path.join(folder, candidate)):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    return candidate


def receive(sock, session, folder: str) -> list:
    """Takes one transfer into `folder` and answers the paths of what landed there, one per
    dragged item. A file is written beside its final name as ".part" and renamed once whole;
    a transfer that breaks off leaves the files already whole and removes the one in flight."""
    os.makedirs(folder, exist_ok=True)
    tops = {}
    landed = []
    while True:
        message = protocol.recv_msg(sock, session)
        kind = message.get("type")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if kind == MSG_END:
            return landed
        if kind != MSG_FILE:
            raise protocol.ProtocolError(f"unexpected {kind!r} on the file channel")
        size, item = data.get("size"), data.get("item")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise protocol.ProtocolError("file size missing or negative")
        if isinstance(item, bool) or not isinstance(item, int):
            raise protocol.ProtocolError("file item missing")
        parts = clean_name(data.get("name"))
        top = tops.get(item)
        if top is None:
            top = tops[item] = unique_name(folder, parts[0], set(tops.values()))
            landed.append(os.path.join(folder, top))
        target = os.path.join(folder, top, *parts[1:])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        partial = target + ".part"
        try:
            with open(partial, "wb") as out:
                left = size
                while left > 0:
                    chunk = protocol.recv_bytes(sock, session, limit=CHUNK_FRAME_LIMIT)
                    if len(chunk) > left:
                        raise protocol.ProtocolError("file is longer than it said")
                    out.write(chunk)
                    left -= len(chunk)
            os.replace(partial, target)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise
