#!/usr/bin/env python3
"""Extract the two Aiwit/EKEN client keys from an official app package.

The Home Assistant integration does not ship these vendor values. You
extract them yourself from the app package you downloaded (.xapk, .apk or a
bare classes.dex) and paste them in the integration's setup form.

Usage:
    python3 tools/extract_keys.py Aiwit_2_6_2.xapk
    python3 tools/extract_keys.py Aiwit_2_6_2.xapk --json

No third-party dependency (Python 3.8+). Nothing is sent over the network.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
import zipfile

# A "secret-looking" string: no spaces, 8..40 chars, at least 2 symbols
# among a typical set, at least one letter.
_SYMBOLS = set("&^%$#@!*+=~?|")
_SECRET_RE = re.compile(r"^[A-Za-z0-9&^%$#@!*+=~?|_\-]{8,40}$")


def _looks_secret(s: str) -> bool:
    return (
        bool(_SECRET_RE.match(s))
        and sum(c in _SYMBOLS for c in s) >= 2
        and any(c.isalpha() for c in s)
        and not s.startswith(("%s", "android", "androidx"))
    )


# --------------------------------------------------------------------------
# Package handling
# --------------------------------------------------------------------------

def iter_dex(path: str):
    """Yield (name, bytes) for every classes*.dex found in path."""
    if path.lower().endswith(".dex"):
        with open(path, "rb") as f:
            yield path, f.read()
        return

    def from_zip(zf: zipfile.ZipFile, prefix: str = ""):
        for info in zf.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1]
            if re.fullmatch(r"classes\d*\.dex", base):
                yield prefix + name, zf.read(info)
            elif name.lower().endswith(".apk") and "/" not in name and not base.startswith("config."):
                # XAPK: the base APK is nested inside the archive
                inner = zipfile.ZipFile(io.BytesIO(zf.read(info)))
                yield from from_zip(inner, prefix + name + "!")

    with zipfile.ZipFile(path) as zf:
        yield from from_zip(zf)


# --------------------------------------------------------------------------
# Minimal DEX reader
# --------------------------------------------------------------------------

class Dex:
    def __init__(self, data: bytes) -> None:
        if data[:4] != b"dex\n":
            raise ValueError("not a DEX file")
        self.d = data
        self.n_str, self.str_off = struct.unpack_from("<II", data, 0x38)
        self.n_cls, self.cls_off = struct.unpack_from("<II", data, 0x60)
        self.strings = [self._string(i) for i in range(self.n_str)]
        self.index = {}
        for i, s in enumerate(self.strings):
            self.index.setdefault(s, i)

    def _uleb(self, o: int):
        r = s = 0
        while True:
            b = self.d[o]
            o += 1
            r |= (b & 0x7F) << s
            s += 7
            if not b & 0x80:
                return r, o

    def _string(self, i: int) -> str:
        off = struct.unpack_from("<I", self.d, self.str_off + 4 * i)[0]
        _, o = self._uleb(off)
        e = self.d.index(b"\0", o)
        return self.d[o:e].decode("utf-8", "replace")

    def static_strings(self):
        """Yield (class_descriptor, [string index, ...]) from static final values."""
        d = self.d
        n_type, type_off = struct.unpack_from("<II", d, 0x40)
        for c in range(self.n_cls):
            cls_idx, _, _, _, _, _, _, sv_off = struct.unpack_from(
                "<8I", d, self.cls_off + 32 * c
            )
            if not sv_off or cls_idx >= n_type:
                continue
            count, o = self._uleb(sv_off)
            out = []
            try:
                for _ in range(count):
                    hdr = d[o]
                    o += 1
                    typ, arg = hdr & 0x1F, hdr >> 5
                    if typ == 0x17:  # VALUE_STRING
                        out.append(int.from_bytes(d[o:o + arg + 1], "little"))
                        o += arg + 1
                    elif typ in (0x00, 0x02, 0x03, 0x04, 0x06, 0x10, 0x11,
                                 0x18, 0x19, 0x1A, 0x1B):
                        o += arg + 1 if typ != 0x00 else 1
                    elif typ in (0x1E, 0x1F):
                        pass
                    else:  # arrays/annotations/etc.: stop parsing this class
                        break
            except IndexError:
                pass
            name_idx = struct.unpack_from("<I", d, type_off + 4 * cls_idx)[0]
            yield self.strings[name_idx], [i for i in out if i < self.n_str]

    def methods(self):
        """Yield the set of string indexes loaded by const-string in each method."""
        d = self.d
        for c in range(self.n_cls):
            cdo = struct.unpack_from("<I", d, self.cls_off + 32 * c + 24)[0]
            if not cdo:
                continue
            sf, o = self._uleb(cdo)
            ifl, o = self._uleb(o)
            dm, o = self._uleb(o)
            vm, o = self._uleb(o)
            for _ in range(sf + ifl):
                _, o = self._uleb(o)
                _, o = self._uleb(o)
            for count in (dm, vm):
                for _ in range(count):
                    _, o = self._uleb(o)          # method_idx_diff
                    _, o = self._uleb(o)          # access_flags
                    code_off, o = self._uleb(o)
                    if code_off:
                        yield self._const_strings(code_off)

    def _const_strings(self, code_off: int) -> set:
        d = self.d
        size = struct.unpack_from("<I", d, code_off + 12)[0]
        base = code_off + 16
        found = set()
        # const-string (0x1a, 2 units) / const-string/jumbo (0x1b, 3 units)
        for u in range(size):
            if d[base + 2 * u] == 0x1A and u + 1 < size:
                idx = struct.unpack_from("<H", d, base + 2 * (u + 1))[0]
                if idx < self.n_str:
                    found.add(idx)
            elif d[base + 2 * u] == 0x1B and u + 2 < size:
                idx = struct.unpack_from("<I", d, base + 2 * (u + 1))[0]
                if idx < self.n_str:
                    found.add(idx)
        return found


# --------------------------------------------------------------------------
# Key detection
# --------------------------------------------------------------------------

def find_login_sign_key(dex: Dex):
    """Login signature key: 'EKDB_...' string, also seen as '%s/%s/%s<key>'."""
    pool = set(dex.strings)
    for s in dex.strings:
        m = re.fullmatch(r"%s/%s/%s(EKDB_\S{6,40})", s)
        if m and m.group(1) in pool:
            return m.group(1)
    for s in dex.strings:
        if re.fullmatch(r"EKDB_\S{8,40}", s) and _looks_secret(s):
            return s
    return None


def find_socket_key(dex: Dex):
    """Socket login key candidates (dict secret -> score).

    1. static final strings of the class that also holds the app name
       "LiveHome" (where the app keeps its constants);
    2. fallback: secret-looking constant loaded in the same method as "login".
    """
    candidates: dict[str, int] = {}
    for _cls, idxs in dex.static_strings():
        strs = [dex.strings[i] for i in idxs]
        if "LiveHome" not in strs:
            continue
        for st in strs:
            if _looks_secret(st) and not st.startswith("EKDB_"):
                candidates[st] = candidates.get(st, 0) + 2
    if candidates:
        return candidates

    login_idx = dex.index.get("login")
    if login_idx is None:
        return candidates
    for idxs in dex.methods():
        if login_idx not in idxs:
            continue
        for i in idxs:
            st = dex.strings[i]
            if _looks_secret(st) and not st.startswith("EKDB_"):
                candidates[st] = candidates.get(st, 0) + 1
    return candidates


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("package", help="Aiwit .xapk / .apk / classes.dex")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    login_key = None
    socket_cands: dict[str, int] = {}
    n_dex = 0
    try:
        for name, data in iter_dex(args.package):
            n_dex += 1
            dex = Dex(data)
            login_key = login_key or find_login_sign_key(dex)
            for k, v in (find_socket_key(dex) or {}).items():
                socket_cands[k] = socket_cands.get(k, 0) + v
    except (OSError, zipfile.BadZipFile, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    if not n_dex:
        print("Error: no classes.dex found in this file.", file=sys.stderr)
        return 2

    ranked = sorted(socket_cands, key=lambda k: (-socket_cands[k], k))
    socket_key = ranked[0] if len(ranked) == 1 else None

    if args.json:
        print(json.dumps({"login_sign_key": login_key, "socket_key": socket_key,
                          "socket_key_candidates": ranked}, indent=2))
    else:
        print(f"Login signature key : {login_key or 'NOT FOUND'}")
        if socket_key:
            print(f"Socket login key    : {socket_key}")
        elif ranked:
            print("Socket login key    : several candidates, try them in order:")
            for k in ranked:
                print(f"    {k}")
        else:
            print("Socket login key    : NOT FOUND")
    return 0 if login_key and ranked else 1


if __name__ == "__main__":
    sys.exit(main())
