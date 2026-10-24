"""Aiwit / EKEN cloud + realtime socket client (sync, for HA executor / threads)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
import uuid
from email.utils import formatdate
from typing import Any, Callable, Optional

import requests

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.v2.gdxp.com/"
APP_NAME = "LiveHome"
APP_VERSION = "2.6.2"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def decode_battery_level(raw: int) -> tuple[Optional[int], bool]:
    low = raw & 0xFF
    high = (raw >> 8) & 0xFF
    charging = high != 0
    if low == 255:
        return None, charging
    return min(low, 100), charging



def _ha_local_timezone():
    """Home Assistant configured timezone, fallback UTC."""
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.DEFAULT_TIME_ZONE
    except Exception:  # noqa: BLE001
        return timezone.utc


class AiwitClient:

    """Sync client: REST login + persistent TCP message socket."""

    def __init__(
        self,
        username: str,
        password: str,
        udid: Optional[str] = None,
        *,
        login_sign_key: str = "",
        socket_key: str = "",
        heartbeat_interval: float = 10.0,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.username = username
        self.password = password
        self.udid = udid or str(uuid.uuid4())
        self.login_sign_key = login_sign_key
        self.socket_key = socket_key
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay

        self.session_id: Optional[str] = None
        self.msg_ip: Optional[str] = None
        self.msg_port: Optional[int] = None
        self.known_devices: list[str] = []

        # Callbacks (set by coordinator)
        self.on_ring: Optional[Callable[[str, dict], None]] = None
        self.on_pir: Optional[Callable[[str, dict], None]] = None
        self.on_device_info: Optional[Callable[[str, dict], None]] = None
        self.on_device_properties: Optional[Callable[[str, dict], None]] = None
        self.on_state: Optional[Callable[[str, dict], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        self._sock: Optional[socket.socket] = None
        self._running = False
        self._want_run = False
        self._hb_counter = 0
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._main_thread: Optional[threading.Thread] = None

    def _base_headers(self) -> dict[str, str]:
        return {
            "AppName": APP_NAME,
            "AppLang": "en",
            "AppVersion": APP_VERSION,
            "AppID": self.udid,
            "OS": "2",
            "User-Agent": "okhttp/4.9.0",
        }

    def login(self) -> dict:
        if not self.login_sign_key:
            raise RuntimeError(
                "Login signature key is missing (see tools/extract_keys.py)"
            )
        sign = _md5(f"{self.username}/{self.password}{self.login_sign_key}")
        data = {
            "username": self.username,
            "password": self.password,
            "pushToken": "pushToken",
            "sign": sign,
            "appSn": self.udid,
        }
        r = requests.post(
            API_BASE + "app_group/login",
            headers=self._base_headers(),
            data=data,
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("resultCode") != 0:
            raise RuntimeError(payload.get("msg") or "Login failed")
        self.session_id = payload["content"]["session_id"]
        _LOGGER.info("Aiwit REST login OK")
        return payload

    def fetch_servers(self) -> tuple[str, int]:
        if not self.session_id:
            raise RuntimeError("Not logged in")
        url = f"{API_BASE}check_session/{self.session_id}/{self.udid}"
        r = requests.get(url, headers=self._base_headers(), timeout=15)
        r.raise_for_status()
        payload = r.json()
        if payload.get("resultCode") != 0:
            raise RuntimeError("check_session failed")
        cmd_servers = payload.get("content", {}).get("cmd_servers") or []
        if not cmd_servers:
            raise RuntimeError("No cmd_servers")
        ip = cmd_servers[0]["ip"]
        port = int(cmd_servers[0]["port"])
        self.msg_ip, self.msg_port = ip, port
        return ip, port

    def _send(self, obj: dict) -> None:
        if not self._sock:
            return
        raw = json.dumps(obj, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._sock.sendall(raw.encode("utf-8"))
            except OSError as err:
                _LOGGER.debug("send failed: %s", err)

    def _send_login(self) -> None:
        if not self.socket_key:
            _LOGGER.error("Socket login key is missing (see tools/extract_keys.py)")
            return
        k = "0" + _md5(self.socket_key + self.udid + "login")
        self._send(
            {
                "cmd": "login",
                "udid": self.udid,
                "username": self.username,
                "pushToken": "pushToken",
                "lang": "en",
                "AppName": APP_NAME,
                "k": k,
            }
        )

    def wakeup_device(self, device_udid: str) -> None:
        """Wake device so it pushes device-info / device-properties."""
        self._send({"cmd": "wakeup", "udid": self.udid, "peer": device_udid})

    def request_devices_state(self) -> None:
        self._send({"cmd": "devices-state", "udid": self.udid})

    def set_properties(self, device_udid: str, properties: dict) -> None:
        """
        Set device properties (PIR, ring_volume, ...).

        Confirmed from decompiled APK (s1/a.java méthodes m()/n()/o()) :
        cmd = "save-property" (PAS "set-properties", corrigé après vérif
        du code -- notre première version utilisait un nom de commande
        jamais confirmé). Le payload confirmé est bien de cette forme :
            {"cmd":"save-property","udid":<app_udid>,"peer":<device_udid>,
             "properties":{<key>: <value>, ...}}

        ⚠️ Ce qui reste NON confirmé : l'encodage exact de la valeur pour
        chaque propriété. Pour "PIR" au moins un fragment de l'app (parmi
        plusieurs variantes selon le modèle de device) envoie la valeur
        décalée de 8 bits (value << 8) -- possiblement spécifique à cette
        variante UI, pas garanti universel. A valider empiriquement en
        comparant la valeur lue avant/après un changement fait depuis
        l'app officielle (cf. discussion projet).
        """
        self._send(
            {
                "cmd": "save-property",
                "udid": self.udid,
                "peer": device_udid,
                "properties": properties,
            }
        )
        # Follow-up wakeup so the device reports the new values
        self.wakeup_device(device_udid)

    def _rest_get(self, build_url: Callable[[], str]) -> dict:
        """GET JSON; if the cloud rejects the session, log in once and retry.

        ``build_url`` is re-evaluated after the relogin because the session id
        is part of the URL path.
        """
        for attempt in (1, 2):
            r = requests.get(build_url(), headers=self._base_headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            code = data.get("resultCode") if isinstance(data, dict) else None
            if code in (0, None) or attempt == 2:
                if code not in (0, None):
                    _LOGGER.warning(
                        "Aiwit REST resultCode=%s msg=%s (apres relogin)",
                        code,
                        data.get("msg"),
                    )
                return data
            _LOGGER.warning(
                "Aiwit REST resultCode=%s msg=%s -> relogin puis nouvel essai",
                code,
                data.get("msg"),
            )
            try:
                self.login()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Aiwit relogin impossible: %s", err)
                return data
        return {}

    # ------------------------------------------------------------------
    # Historique des clips / vignette (image.py)

    # ------------------------------------------------------------------
    def list_clips(
        self, device_udid: str, date_str: str, type_filter: int = 0, page: int = 1
    ) -> dict:
        """
        Liste les clips enregistrés côté cloud pour une date donnée.
        type_filter : 0=Tous, 1=Phone, 2=PIR, 3=Doorbell.
        Champs par clip : deviceSn, fileName, length, type, duration, pk,
        bucket, endpoint... (cf. reverse de HistoricalVideosForPlayOnline).
        """
        if not self.session_id:
            raise RuntimeError("Not logged in")
        return self._rest_get(
            lambda: (
                f"{API_BASE}oss_get_files_v8/"
                f"{self.session_id}/{date_str}/{device_udid}/{page}/{type_filter}/0"
            )
        )

    def update_properties_rest(self, device_udid: str, properties: dict) -> dict:
        """
        Change des propriétés device via l'endpoint REST dédié (au lieu de
        la commande socket "save-property") -- cf. p2/e.java méthode B0(),
        endpoint c2.v.f3794k1 = "app_update_property".

        Clés confirmées acceptées par cet endpoint (cf. décompilation) :
            PIR (10=Faible/5=Standard/1=Haut, calibré empiriquement),
            c_night_mode, nn_sens (niveau détection humanoïde, valeurs
            exactes non calibrées), bs_track_mode, nn_label (probablement
            un bitmask des classes détectées -- non confirmé),
            reverse_video, guard_position, motion_enable (0/1).

        ⚠️ Comme pour la commande socket équivalente, l'écriture nécessite
        probablement un compte "master" (admin) sur le device.
        """
        if not self.session_id:
            raise RuntimeError("Not logged in")
        data = {"session_id": self.session_id, "device_sn": device_udid}
        for key in (
            "PIR",
            "c_night_mode",
            "nn_sens",
            "bs_track_mode",
            "nn_label",
            "reverse_video",
            "guard_position",
            "motion_enable",
        ):
            if key in properties:
                data[key] = str(properties[key])
        r = requests.post(
            API_BASE + "app_update_property",
            headers=self._base_headers(),
            data=data,
            timeout=15,
        )
        r.raise_for_status()
        result = r.json()
        _LOGGER.debug("app_update_property(%s, %s) -> %s", device_udid, properties, result)
        if isinstance(result, dict) and result.get("resultCode") not in (0, None):
            raise RuntimeError(
                f"app_update_property refuse: resultCode={result.get('resultCode')} "
                f"msg={result.get('msg')}"
            )
        return result

    @staticmethod
    def _thumbnail_name(file_name: str) -> str:
        for ext in (".v6ts", ".ts2", ".ts4", ".js2", ".mAudioTS"):
            if file_name.endswith(ext):
                return file_name[: -len(ext)] + ".jpg"
        return file_name.rsplit(".", 1)[0] + ".jpg"

    def fetch_sts_token(self, oss_endpoint: str) -> dict:
        """
        Récupère des identifiants STS temporaires pour signer les requêtes
        OSS. URL confirmée par capture mitmproxy (2026-09-20) :
            sts/token/{session_id}/{oss_endpoint}/9
        (PAS de device_udid dans l'URL, contrairement à une hypothèse
        précédente -- le "9" est OssFromId, une constante).
        """
        if not self.session_id:
            raise RuntimeError("Not logged in")
        data = self._rest_get(
            lambda: f"{API_BASE}sts/token/{self.session_id}/{oss_endpoint}/9"
        )
        if data.get("resultCode") != 0:
            raise RuntimeError(f"sts/token failed: {data}")
        return data["content"]

    @staticmethod
    def _sign_oss_get(bucket: str, object_key: str, ak_secret: str, security_token: str):
        """
        Signature Aliyun OSS v1 (HMAC-SHA1), confirmée octet pour octet par
        capture mitmproxy d'une vraie requête de l'app officielle
        (aliyun-sdk-android/2.9.5). Documentation publique Alibaba Cloud :
        StringToSign = VERB + "\\n" + Content-MD5 + "\\n" + Content-Type +
                       "\\n" + Date + "\\n" + CanonicalizedOSSHeaders +
                       CanonicalizedResource
        """
        date_str = formatdate(timeval=None, localtime=False, usegmt=True)
        canonicalized_headers = f"x-oss-security-token:{security_token}\n"
        canonicalized_resource = f"/{bucket}/{object_key}"
        string_to_sign = (
            f"GET\n\n\n{date_str}\n{canonicalized_headers}{canonicalized_resource}"
        )
        signature = base64.b64encode(
            hmac.new(
                ak_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")
        return date_str, signature

    def fetch_oss_object(self, bucket: str, endpoint: str, object_key: str) -> bytes:
        """Télécharge un objet OSS privé en signant la requête avec un token STS frais.

        Timeout large (90s) : les .ts2 font souvent 1–3 Mo, contrairement aux vignettes.
        Essaie https puis http (comme le script PC qui fonctionne).
        """
        sts = self.fetch_sts_token(endpoint)
        ak_id = sts["AccessKeyId"]
        ak_secret = sts["AccessKeySecret"]
        token = sts["SecurityToken"]

        date_str, signature = self._sign_oss_get(bucket, object_key, ak_secret, token)
        headers = {
            "Authorization": f"OSS {ak_id}:{signature}",
            "x-oss-security-token": token,
            "Date": date_str,
            "User-Agent": "aliyun-sdk-android/2.9.5",
        }
        last_err: Exception | None = None
        for scheme in ("https", "http"):
            url = f"{scheme}://{bucket}.{endpoint}/{object_key}"
            try:
                r = requests.get(url, headers=headers, timeout=90)
            except requests.RequestException as err:
                last_err = err
                _LOGGER.debug("Aiwit OSS GET %s failed: %s", url, err)
                continue
            _LOGGER.debug(
                "Aiwit OSS GET %s -> HTTP %s, %d bytes, content-type=%s",
                url, r.status_code, len(r.content), r.headers.get("Content-Type"),
            )
            if r.status_code == 200 and len(r.content) > 100:
                return r.content
            last_err = RuntimeError(
                f"OSS GET HTTP {r.status_code} for {object_key} ({len(r.content)} bytes)"
            )
        if last_err:
            raise last_err
        raise RuntimeError(f"OSS download failed for {object_key}")

    @staticmethod
    def _parse_clip_timestamp_naive(file_name: str) -> Optional[datetime]:
        """Extract naive YYYYMMDDHHMMSS from an Aiwit TS2 path/name."""
        name = file_name.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        if len(stem) != 14 or not stem.isdigit():
            return None
        try:
            return datetime.strptime(stem, "%Y%m%d%H%M%S")
        except ValueError:
            return None

    @staticmethod
    def _parse_clip_timestamp(file_name: str) -> Optional[datetime]:
        """Best single UTC estimate for a clip filename (local TZ preferred)."""
        naive = AiwitClient._parse_clip_timestamp_naive(file_name)
        if naive is None:
            return None
        # Aiwit filenames are wall-clock on the account/device, not pure UTC.
        local_tz = _ha_local_timezone()
        return naive.replace(tzinfo=local_tz).astimezone(timezone.utc)

    @staticmethod
    def _clip_timestamp_candidates(file_name: str) -> list[datetime]:
        """Possible UTC instants for a filename (local TZ and UTC)."""
        naive = AiwitClient._parse_clip_timestamp_naive(file_name)
        if naive is None:
            return []
        local_tz = _ha_local_timezone()
        out: list[datetime] = [
            naive.replace(tzinfo=local_tz).astimezone(timezone.utc),
            # Nuit du passage heure d'ete -> hiver : l'heure 02:00-03:00 existe
            # deux fois ; on propose aussi la 2e occurrence (fold=1), le plus
            # proche de l'evenement sera retenu.
            naive.replace(tzinfo=local_tz, fold=1).astimezone(timezone.utc),
            naive.replace(tzinfo=timezone.utc),
        ]
        # De-duplicate while preserving order
        seen: set[datetime] = set()
        uniq: list[datetime] = []
        for dt in out:
            if dt not in seen:
                seen.add(dt)
                uniq.append(dt)
        return uniq

    @staticmethod
    def _parse_metadata_timestamp(value: Any) -> Optional[datetime]:
        """Best-effort parse of a cloud clip timestamp field."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            # Aiwit APIs may expose Unix seconds or milliseconds.
            seconds = float(value)
            if seconds > 10_000_000_000:
                seconds /= 1000.0
            if 946684800 <= seconds <= 4102444800:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return AiwitClient._parse_metadata_timestamp(int(text))
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            # Support "YYYY-MM-DD HH:MM:SS"
            if " " in text and "T" not in text:
                text = text.replace(" ", "T", 1)
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                # Naive cloud "time" is wall-clock in HA local TZ, not UTC.
                dt = dt.replace(tzinfo=_ha_local_timezone())
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    @classmethod
    def _clip_metadata_datetime(cls, clip: dict[str, Any]) -> Optional[datetime]:
        """Find a usable timestamp if the cloud exposes one separately."""
        for key in (
            "timestamp",
            "startTime",
            "start_time",
            "createTime",
            "create_time",
            "fileTime",
            "file_time",
            "time",
        ):
            dt = cls._parse_metadata_timestamp(clip.get(key))
            if dt is not None:
                return dt
        return None

    def find_event_clip(
        self,
        device_udid: str,
        event_timestamp: datetime,
        *,
        type_filter: int = 0,
        max_delta_seconds: int = 15 * 60,
    ) -> Optional[dict[str, Any]]:
        """Find the cloud clip matching an event timestamp.

        The filename timestamp is the primary discriminator. If the cloud
        also exposes a parseable timestamp field, it is used as a secondary
        cross-check. A stale "latest clip" is never accepted merely because
        it is the newest clip currently visible to the API.
        """
        if event_timestamp.tzinfo is None:
            event_timestamp = event_timestamp.replace(tzinfo=timezone.utc)
        event_timestamp = event_timestamp.astimezone(timezone.utc)

        # Normally the event and clip share a date. Check adjacent dates only
        # around midnight to handle delayed cloud publication safely.
        # Search cloud by UTC day and by HA-local day (filename is wall-clock).
        local_tz = _ha_local_timezone()
        event_local = event_timestamp.astimezone(local_tz)
        dates = {
            event_timestamp.strftime("%Y%m%d"),
            event_local.strftime("%Y%m%d"),
        }
        for base in (event_timestamp, event_local):
            if base.hour == 0 and base.minute < 15:
                dates.add((base - timedelta(days=1)).strftime("%Y%m%d"))
            if base.hour == 23 and base.minute >= 45:
                dates.add((base + timedelta(days=1)).strftime("%Y%m%d"))

        candidates: list[tuple[float, float, dict[str, Any]]] = []
        for date_str in dates:
            try:
                data = self.list_clips(device_udid, date_str, type_filter=type_filter)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Aiwit list_clips(%s) failed: %s", date_str, err)
                continue

            content = data.get("content")
            clips = content.get("items") if isinstance(content, dict) else content
            for clip in clips or []:
                if not isinstance(clip, dict):
                    continue
                file_name = str(clip.get("fileName") or "")
                clip_dts = self._clip_timestamp_candidates(file_name)
                if not clip_dts:
                    continue

                # Prefer the interpretation (local TZ vs UTC) closest to the event.
                name_delta = min(
                    abs((dt - event_timestamp).total_seconds()) for dt in clip_dts
                )
                if name_delta > max_delta_seconds:
                    continue

                meta_dt = self._clip_metadata_datetime(clip)
                meta_delta = (
                    abs((meta_dt - event_timestamp).total_seconds())
                    if meta_dt is not None
                    else 0.0
                )

                # Meta is advisory only: naive cloud "time" used to be forced to
                # UTC and wrongly rejected valid local-filename matches.
                if meta_dt is not None and meta_delta > max_delta_seconds:
                    _LOGGER.debug(
                        "Aiwit: meta eloignee pour %s (meta=%+.1fs) — nom conserve (%.1fs)",
                        file_name,
                        (meta_dt - event_timestamp).total_seconds(),
                        name_delta,
                    )
                    meta_delta = name_delta

                candidates.append((name_delta, meta_delta, clip))

        if not candidates:
            _LOGGER.debug(
                "Aiwit: aucun clip correspondant a l'event %s (fenetre ±%ss)",
                event_timestamp.isoformat(),
                max_delta_seconds,
            )
            return None

        candidates.sort(key=lambda item: (item[0], item[1], str(item[2].get("fileName", ""))))
        name_delta, meta_delta, clip = candidates[0]
        _LOGGER.debug(
            "Aiwit: clip selectionne %s (ecart nom=%.1fs, meta=%s)",
            clip.get("fileName"),
            name_delta,
            f"{meta_delta:.1f}s" if self._clip_metadata_datetime(clip) else "n/a",
        )
        return clip

    def fetch_event_thumbnail(
        self, device_udid: str, event_timestamp: datetime
    ) -> Optional[bytes]:
        """Fetch only the thumbnail belonging to the specified event.

        Window intentionally tight (±120s): a wider match caused wrong images
        (e.g. previous clip) to be attached to Telegram notifications.
        """
        clip = self.find_event_clip(
            device_udid,
            event_timestamp,
            type_filter=0,
            max_delta_seconds=120,
        )
        if not clip:
            return None

        file_name = str(clip.get("fileName") or "")
        bucket = str(clip.get("bucket") or "")
        endpoint = str(clip.get("endpoint") or "")
        if not (file_name and bucket and endpoint):
            _LOGGER.debug(
                "Aiwit: champs manquants pour la vignette (fileName=%r bucket=%r endpoint=%r)",
                file_name,
                bucket,
                endpoint,
            )
            return None

        jpg_key = self._thumbnail_name(file_name)
        try:
            return self.fetch_oss_object(bucket, endpoint, jpg_key)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Aiwit: echec telechargement vignette %s: %s", jpg_key, err)
            return None

    def fetch_event_thumbnail_ex(
        self, device_udid: str, event_timestamp: datetime
    ) -> Optional[tuple[bytes, str]]:
        """Like fetch_event_thumbnail() but also returns the clip file name."""
        clip = self.find_event_clip(
            device_udid, event_timestamp, type_filter=0, max_delta_seconds=120
        )
        if not clip:
            return None
        file_name = str(clip.get("fileName") or "")
        bucket = str(clip.get("bucket") or "")
        endpoint = str(clip.get("endpoint") or "")
        if not (file_name and bucket and endpoint):
            return None
        try:
            data = self.fetch_oss_object(bucket, endpoint, self._thumbnail_name(file_name))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Aiwit: echec telechargement vignette %s: %s", file_name, err)
            return None
        return data, file_name

    def fetch_latest_thumbnail(self, device_udid: str) -> Optional[bytes]:
        """Compatibility fallback for manual requests without an event timestamp.

        Event-driven code must use fetch_event_thumbnail() so an old cloud clip
        can never be returned for a new event.
        """
        try:
            today = time.strftime("%Y%m%d")
            data = self.list_clips(device_udid, today, type_filter=0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Aiwit list_clips failed: %s", err)
            return None

        content = data.get("content")
        clips = content.get("items") if isinstance(content, dict) else content
        if not clips:
            return None
        clips = sorted(clips, key=lambda c: c.get("fileName", ""), reverse=True)
        clip = clips[0]
        file_name = str(clip.get("fileName") or "")
        bucket = str(clip.get("bucket") or "")
        endpoint = str(clip.get("endpoint") or "")
        if not (file_name and bucket and endpoint):
            return None
        try:
            return self.fetch_oss_object(bucket, endpoint, self._thumbnail_name(file_name))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Aiwit: echec telechargement vignette %s: %s", file_name, err)
            return None

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(self.heartbeat_interval)
            if not self._running:
                break
            self._hb_counter += 1
            self._send(
                {"cmd": "heartbeat", "udid": self.udid, "sn": self._hb_counter}
            )

    def _read_loop(self) -> None:
        buf = b""
        while self._running and self._sock:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while buf:
                try:
                    text = buf.decode("utf-8", errors="ignore")
                    obj, idx = json.JSONDecoder().raw_decode(text)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
                self._handle_message(obj)
                consumed = text[:idx].encode("utf-8")
                buf = buf[len(consumed) :].lstrip(b"\n\r\x00 ")

        self._running = False
        if self.on_disconnected:
            try:
                self.on_disconnected()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("on_disconnected error")

    def _handle_message(self, obj: dict) -> None:
        cmd = obj.get("cmd") or ""

        if cmd == "login":
            if obj.get("err_no", -1) == 0:
                _LOGGER.info("Aiwit socket login OK")
                self._send({"cmd": "devices-state", "udid": self.udid})
                if self.on_connected:
                    try:
                        self.on_connected()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("on_connected error")
            for d in obj.get("devices") or []:
                udid = d.get("udid")
                if udid and udid not in self.known_devices:
                    self.known_devices.append(udid)
                    self.wakeup_device(udid)
            return

        if cmd in ("preview-start", "wakeup"):
            wtype = (obj.get("wakeup_type") or "").upper()
            device = (
                obj.get("udid")
                or obj.get("peer")
                or (self.known_devices[0] if self.known_devices else None)
            )
            if not device:
                return
            if wtype == "RING" and self.on_ring:
                try:
                    self.on_ring(device, obj)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_ring error")
            elif wtype == "PIR" and self.on_pir:
                try:
                    self.on_pir(device, obj)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_pir error")
            return

        if cmd == "device-info":
            device = obj.get("udid")
            info = dict(obj.get("info") or obj)
            if device and self.on_device_info:
                if "battery_level" in info:
                    pct, charging = decode_battery_level(info["battery_level"])
                    info["battery_percent"] = pct
                    info["battery_charging"] = charging
                try:
                    self.on_device_info(device, info)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_device_info error")
            return

        if cmd == "device-properties":
            device = obj.get("udid")
            props = obj.get("properties") or obj
            if device and self.on_device_properties:
                try:
                    self.on_device_properties(device, props)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_device_properties error")
            return

        if cmd == "kickout-app":
            _LOGGER.warning(
                "Aiwit: session kickee par le cloud (kickout-app) -- une "
                "autre session (app mobile ?) a pris la main sur ce compte. "
                "Verifie qu'aucun autre client n'utilise ce meme compte."
            )
            return

        if cmd in ("device-state", "devices-state", "device-heartbeat"):
            device = obj.get("udid")
            if device and self.on_state:
                try:
                    self.on_state(device, obj)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_state error")
            for d in obj.get("devices") or []:
                udid = d.get("udid")
                if udid and self.on_state:
                    try:
                        self.on_state(udid, d)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("on_state error")

    def connect_once(self) -> None:
        self.login()
        ip, port = self.fetch_servers()
        self._sock = socket.create_connection((ip, port), timeout=10)
        self._sock.settimeout(None)
        self._running = True
        self._send_login()
        t_hb = threading.Thread(
            target=self._heartbeat_loop, name="aiwit-hb", daemon=True
        )
        t_rd = threading.Thread(
            target=self._read_loop, name="aiwit-rd", daemon=True
        )
        self._threads = [t_hb, t_rd]
        t_hb.start()
        t_rd.start()
        for t in self._threads:
            t.join()

    def start_background(self) -> None:
        """Start reconnect loop in a daemon thread (for Home Assistant)."""
        if self._main_thread and self._main_thread.is_alive():
            return
        self._want_run = True

        def _loop() -> None:
            backoff = self.reconnect_delay
            max_backoff = 300.0  # 5 minutes plafond
            while self._want_run:
                try:
                    self.connect_once()
                    backoff = self.reconnect_delay  # connexion OK -> reset backoff
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Aiwit connection error: %s", err)
                finally:
                    self._cleanup_socket()
                if not self._want_run:
                    break
                _LOGGER.debug("Aiwit reconnecting in %.1fs", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        self._main_thread = threading.Thread(
            target=_loop, name="aiwit-main", daemon=True
        )
        self._main_thread.start()

    def stop(self) -> None:
        self._want_run = False
        self._running = False
        self._cleanup_socket()

    def _cleanup_socket(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # déjà fermé/non connecté, sans importance
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
