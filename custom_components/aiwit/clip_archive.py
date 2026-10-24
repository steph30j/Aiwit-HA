"""Aiwit cloud clip download and MP4 conversion helpers."""

from __future__ import annotations

import logging

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import AiwitClient
from . import aiwit_ts2_decode as ts2dec

_LOGGER = logging.getLogger(__name__)

# Un verrou par MP4 cible : plusieurs evenements PIR rapproches designent le
# meme clip, on ne le telecharge/convertit qu'une fois (les autres attendent
# puis constatent que le fichier existe).
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


CLIP_NAME_RE = re.compile(r"^(?P<stamp>\d{14})\.ts2$", re.IGNORECASE)


def _event_datetime(event_timestamp: str) -> datetime:
    """Parse an ISO-8601 event timestamp into an aware UTC datetime."""
    value = event_timestamp.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clip_datetime(file_name: str) -> datetime | None:
    """Extract the UTC-naive timestamp encoded in an Aiwit TS2 filename."""
    name = Path(file_name).name
    match = CLIP_NAME_RE.fullmatch(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _clip_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    content = data.get("content")
    items = content.get("items") if isinstance(content, dict) else content
    return [c for c in (items or []) if isinstance(c, dict) and c.get("fileName")]


def _find_event_clip(
    client: AiwitClient,
    device_udid: str,
    event_dt: datetime,
    event_type: str,
) -> dict[str, Any] | None:
    """Same selector as thumbnails: prefer typed filter, fall back to all.

    Cloud sometimes stores PIR/RING clips under another category (e.g. wifi).
    Thumbnail fetch already uses type_filter=0 successfully; archive must not
    be stricter or the folder stays empty while Telegram gets an image.
    """
    preferred = {"pir": 2, "ring": 3}.get(event_type.lower())
    filters = [preferred, 0] if preferred is not None else [0]
    seen: set[int] = set()
    for type_filter in filters:
        if type_filter in seen:
            continue
        seen.add(type_filter)
        clip = client.find_event_clip(
            device_udid,
            event_dt,
            type_filter=type_filter,
            max_delta_seconds=15 * 60,
        )
        if clip:
            return clip
    return None


def archive_event_clip(
    client: AiwitClient,
    device_udid: str,
    output_root: str,
    event_type: str,
    event_timestamp: str,
    fps: int = 15,
    retries: int = 8,
    initial_delay: float = 20.0,
    retry_delay: float = 15.0,
) -> Path | None:
    """Download and convert the cloud clip corresponding to one event.

    Le cloud Aiwit publie le clip APRES la fin de l'enregistrement (souvent
    30 a 90 s apres l'evenement) : 3 essais a 5 s (ancien defaut) abandonnaient
    trop tot et laissaient des trous dans l'archive.
    """
    event_dt = _event_datetime(event_timestamp)
    if initial_delay > 0:
        time.sleep(initial_delay)

    last_error: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            clip = _find_event_clip(client, device_udid, event_dt, event_type)
            if clip:
                return _download_and_convert(client, clip, Path(output_root), fps)
            _LOGGER.debug(
                "Aiwit: clip de l'event %s/%s pas encore publie (essai %d/%d)",
                device_udid, event_type, attempt + 1, attempts,
            )
        except Exception as err:  # noqa: BLE001
            last_error = err
            _LOGGER.debug("Aiwit: archive essai %d/%d: %s", attempt + 1, attempts, err)
        if attempt + 1 < attempts:
            time.sleep(retry_delay)

    if last_error:
        raise last_error
    return None


def _download_and_convert(
    client: AiwitClient,
    clip: dict[str, Any],
    output_root: Path,
    fps: int,
) -> Path:
    clip_dt = _clip_datetime(str(clip.get("fileName") or ""))
    key = str(output_root / (clip_dt.strftime("%Y%m%d") if clip_dt else "?") / str(clip.get("fileName")))
    with _lock_for(key):
        return _do_download_and_convert(client, clip, output_root, fps)


def _do_download_and_convert(
    client: AiwitClient,
    clip: dict[str, Any],
    output_root: Path,
    fps: int,
) -> Path:
    file_name = str(clip["fileName"])
    bucket = str(clip.get("bucket") or "")
    endpoint = str(clip.get("endpoint") or "")
    pk = str(clip.get("pk") or "")
    if not bucket or not endpoint or len(pk) != 16:
        raise RuntimeError("Aiwit clip is missing bucket, endpoint or 16-char pk")

    clip_dt = _clip_datetime(file_name)
    if clip_dt is None:
        raise RuntimeError(f"Unsupported Aiwit TS2 filename: {file_name}")

    day_dir = output_root / clip_dt.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(file_name).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem) or "clip"
    mp4_path = day_dir / f"{safe_stem}.mp4"
    err_path = day_dir / f"{safe_stem}.error.txt"
    if mp4_path.exists() and mp4_path.stat().st_size > 0:
        return mp4_path

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        msg = "ffmpeg is required to create the MP4 archive"
        err_path.write_text(msg, encoding="utf-8")
        raise RuntimeError(msg)

    try:
        _LOGGER.info(
            "Aiwit: download clip %s bucket=%s endpoint=%s pk=%s…",
            file_name,
            bucket,
            endpoint,
            pk[:4] + "…",
        )
        raw = client.fetch_oss_object(bucket, endpoint, file_name)
        _LOGGER.info("Aiwit: downloaded %d bytes, decrypt…", len(raw))

        if len(raw) < 200:
            raise RuntimeError(f"OSS returned tiny payload ({len(raw)} bytes)")

        pk_b = pk.encode("ascii")
        h264, aac = ts2dec.parse_and_decrypt(raw, pk_b)
        if len(h264) < 100:
            # Keep raw for offline debug
            ts2_debug = day_dir / f"{safe_stem}.ts2"
            ts2_debug.write_bytes(raw)
            (day_dir / f"{safe_stem}.ts2.pk").write_text(pk, encoding="ascii")
            raise RuntimeError(
                f"Decrypt produced little H.264 ({len(h264)} B) — ts2 saved for debug"
            )

        with tempfile.TemporaryDirectory(prefix="aiwit-clip-") as tmp:
            tmp_dir = Path(tmp)
            h264_path = tmp_dir / f"{safe_stem}.h264"
            aac_path = tmp_dir / f"{safe_stem}.aac"
            raw_mp4 = tmp_dir / f"{safe_stem}.mp4"
            h264_path.write_bytes(h264)
            aac_file = None
            if aac:
                aac_path.write_bytes(aac)
                aac_file = aac_path

            if not ts2dec.remux_mp4(h264_path, raw_mp4, aac_path=aac_file, fps=fps):
                raise RuntimeError("ffmpeg remux failed (see HA logs for ffmpeg stderr)")

            if not raw_mp4.exists() or raw_mp4.stat().st_size == 0:
                raise RuntimeError("ffmpeg produced empty MP4")

            try:
                raw_mp4.replace(mp4_path)
            except OSError:
                mp4_path.write_bytes(raw_mp4.read_bytes())

        if err_path.exists():
            err_path.unlink(missing_ok=True)
        _LOGGER.info("Aiwit: archived %s (%d bytes)", mp4_path, mp4_path.stat().st_size)
        _save_thumbnail_next_to_mp4(client, clip, mp4_path)
        return mp4_path

    except Exception as err:
        err_path.write_text(
            f"{type(err).__name__}: {err}\nfileName={file_name}\npk={pk}\n",
            encoding="utf-8",
        )
        _LOGGER.exception("Aiwit: archive failed for %s", file_name)
        raise




def _save_thumbnail_next_to_mp4(
    client: AiwitClient, clip: dict[str, Any], mp4_path: Path
) -> None:
    """Best effort: <stem>.jpg next to <stem>.mp4 (never fails the archive)."""
    jpg_path = mp4_path.with_suffix(".jpg")
    if jpg_path.exists() and jpg_path.stat().st_size > 0:
        return
    try:
        jpg_key = client._thumbnail_name(str(clip["fileName"]))  # noqa: SLF001
        data = client.fetch_oss_object(str(clip["bucket"]), str(clip["endpoint"]), jpg_key)
        tmp = jpg_path.with_suffix(".jpg.tmp")
        tmp.write_bytes(data)
        tmp.replace(jpg_path)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Aiwit: vignette a cote du mp4 impossible (%s): %s", mp4_path.name, err)
