#!/usr/bin/env python3
"""
Décode un clip Aiwit/EKEN .ts2 en H.264 + AAC / MP4.

Algo (reverse ff_ts2_demuxer / libEZMediaPlayer) :
  - Conteneur : trames magic D1 1D, header 28 octets, marqueur CD AB
  - Payload aligné 16 octets : (size + 15) & ~15
  - AES-128-CBC, clé = pk, IV = pk (même chaîne 16 chars)
  - Type 0x01 / 0x05 → NAL H.264
  - Type 0x02 → trames AAC ADTS (souvent 16 kHz mono)

Usage :
  python aiwit_ts2_decode.py clip.ts2 zRg0cwXQZthVdjVZ
  python aiwit_ts2_decode.py clip.ts2 zRg0cwXQZthVdjVZ -o out.mp4
  python aiwit_ts2_decode.py clip.ts2 --pk-file clip.ts2.pk

Dépendances : pip install cryptography
  ffmpeg recommandé (remux MP4 avec son)
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HEADER_LEN = 28
MAGIC = b"\xd1\x1d"

# Types de trames (octet type du header)
TYPE_VIDEO_P = 0x01
TYPE_AUDIO = 0x02
TYPE_VIDEO_IDR = 0x05


def align16(n: int) -> int:
    return (n + 15) & ~15


def aes_cbc_decrypt(blob: bytes, key: bytes, iv: bytes) -> bytes:
    if len(blob) % 16:
        raise ValueError(f"ciphertext length {len(blob)} not multiple of 16")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
    return dec.update(blob) + dec.finalize()


def parse_and_decrypt(data: bytes, pk: bytes) -> tuple[bytes, bytes]:
    """Retourne (h264_annexb, aac_adts)."""
    if len(pk) != 16:
        raise ValueError(f"pk must be 16 bytes, got {len(pk)}: {pk!r}")

    video = bytearray()
    audio = bytearray()
    i = 0
    n_frames = n_video = n_audio = 0

    while i < len(data) - HEADER_LEN:
        if data[i : i + 2] != MAGIC or data[i + 3] != 0x1C:
            i += 1
            continue

        typ = data[i + 2]
        seq = struct.unpack_from("<I", data, i + 4)[0]
        size = struct.unpack_from("<I", data, i + 8)[0]
        al = align16(size)

        if i + HEADER_LEN + al > len(data):
            print(f"[!] truncate at off={i} type={typ} size={size} al={al}", file=sys.stderr)
            break

        payload = data[i + HEADER_LEN : i + HEADER_LEN + al]
        i += HEADER_LEN + al
        n_frames += 1

        if al < 16:
            continue

        try:
            dec = aes_cbc_decrypt(payload, pk, pk)[:size]
        except Exception as e:
            print(f"[!] AES fail type={typ} seq={seq}: {e}", file=sys.stderr)
            continue

        if typ in (TYPE_VIDEO_P, TYPE_VIDEO_IDR):
            n_video += 1
            j = dec.find(b"\x00\x00\x00\x01")
            if j < 0:
                j = dec.find(b"\x00\x00\x01")
            if j >= 0:
                video += dec[j:]
            elif dec and (dec[0] & 0x1F) in (1, 5, 7, 8):
                video += b"\x00\x00\x00\x01" + dec

        elif typ == TYPE_AUDIO:
            n_audio += 1
            # ADTS commence en général par 0xFFF1 / 0xFFF9
            audio += dec

    print(
        f"[*] frames={n_frames} video={n_video} audio={n_audio} "
        f"h264={len(video)}B aac={len(audio)}B"
    )
    return bytes(video), bytes(audio)


def remux_mp4(
    h264_path: Path,
    mp4_path: Path,
    aac_path: Path | None = None,
    fps: int = 15,
) -> bool:
    """Remux H.264 (+ AAC ADTS optionnel) → MP4 avec durée correcte."""
    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts",
        "-r",
        str(fps),
        "-i",
        str(h264_path),
    ]
    if aac_path is not None and aac_path.exists() and aac_path.stat().st_size > 0:
        cmd += ["-i", str(aac_path), "-c:v", "copy", "-c:a", "copy"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-movflags", "+faststart", str(mp4_path)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("[!] ffmpeg introuvable — flux bruts uniquement", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(r.stderr[-500:], file=sys.stderr)
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Decode Aiwit/EKEN .ts2 → H.264+AAC/MP4")
    ap.add_argument("ts2", type=Path, help="fichier .ts2")
    ap.add_argument("pk", nargs="?", help="clé 16 caractères (API field pk)")
    ap.add_argument("--pk-file", type=Path, help="fichier texte contenant le pk")
    ap.add_argument("-o", "--output", type=Path, help="sortie .mp4 (défaut: <ts2>.mp4)")
    ap.add_argument("--h264", type=Path, help="chemin .h264 brut")
    ap.add_argument("--aac", type=Path, help="chemin .aac brut")
    ap.add_argument("--fps", type=int, default=15, help="framerate conteneur vidéo")
    ap.add_argument("--no-audio", action="store_true", help="ignorer l'audio")
    args = ap.parse_args()

    if args.pk_file:
        pk = args.pk_file.read_text(encoding="utf-8").strip().encode("ascii")
    elif args.pk:
        pk = args.pk.strip().encode("ascii")
    else:
        side = args.ts2.with_suffix(args.ts2.suffix + ".pk")
        if not side.exists():
            side = args.ts2.with_suffix(".pk")
        if side.exists():
            pk = side.read_text(encoding="utf-8").strip().encode("ascii")
            print(f"[*] pk lu depuis {side}")
        else:
            ap.error("fournir pk en argument, --pk-file, ou fichier .pk à côté")

    if len(pk) != 16:
        ap.error(f"pk doit faire 16 caractères, got {len(pk)}: {pk!r}")

    data = args.ts2.read_bytes()
    print(f"[*] {args.ts2} ({len(data)} bytes) pk={pk.decode()}")

    h264, aac = parse_and_decrypt(data, pk)
    if len(h264) < 100:
        print("[!] peu ou pas de H.264 — pk incorrect ?", file=sys.stderr)
        sys.exit(2)

    h264_path = args.h264 or args.ts2.with_suffix(".h264")
    h264_path.write_bytes(h264)
    print(f"[*] H.264 → {h264_path}")

    aac_path = None
    if not args.no_audio and len(aac) > 0:
        aac_path = args.aac or args.ts2.with_suffix(".aac")
        aac_path.write_bytes(aac)
        print(f"[*] AAC  → {aac_path} ({len(aac)} bytes)")
    elif args.no_audio:
        print("[*] audio ignoré (--no-audio)")
    else:
        print("[*] pas d'audio dans ce clip")

    mp4_path = args.output or args.ts2.with_suffix(".mp4")
    if remux_mp4(h264_path, mp4_path, aac_path=aac_path, fps=args.fps):
        print(f"[*] MP4  → {mp4_path} ({mp4_path.stat().st_size} bytes)")
        try:
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height,sample_rate,channels,duration,nb_frames",
                    "-of",
                    "csv=p=0",
                    str(mp4_path),
                ],
                capture_output=True,
                text=True,
            )
            for line in r.stdout.strip().splitlines():
                if line.strip():
                    print(f"[*] stream: {line.strip()}")
        except FileNotFoundError:
            pass
    else:
        print(f"[*] Ouvre les flux bruts : {h264_path}" + (f" + {aac_path}" if aac_path else ""), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
