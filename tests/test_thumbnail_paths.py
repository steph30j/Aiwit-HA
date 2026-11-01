"""Thumbnail directory modes (no Home Assistant needed). Run: python3 tests/test_thumbnail_paths.py"""
import ast, re, sys, tempfile, textwrap
from datetime import datetime
from pathlib import Path

src_path = Path(__file__).resolve().parents[1] / "custom_components" / "aiwit" / "coordinator.py"
src = src_path.read_text()
fn = [n for c in ast.parse(src).body if isinstance(c, ast.ClassDef) for n in c.body
      if isinstance(n, ast.FunctionDef) and n.name == "_save_thumbnail_file"][0]
g = {"Path": Path, "re": re, "datetime": datetime, "THUMBNAIL_WWW_DIR": "aiwit"}
exec(textwrap.dedent(ast.get_source_segment(src, fn)), g)
save = g["_save_thumbnail_file"]

class Cfg:
    def __init__(self, root): self.root = root
    def path(self, *a): return str(Path(self.root, *a))
class Hass:
    def __init__(self, root): self.config = Cfg(root)
class Coord:
    def __init__(self, root, thumb): self.hass = Hass(root); self.thumbnail_path = thumb or None

img = b"\xff\xd8\xff" + b"x" * 600
UDID, CLIP = "EKDB_367B888D-D2C7-BA30-45C3-62C2389EC354", "20261031145156.ts2"
fails = 0
def check(label, cond):
    global fails; fails += not cond; print(("PASS " if cond else "FAIL ") + label)
def tree(root): return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())

# 1) mode {date}, disque present
cfg = Path(tempfile.mkdtemp()); disk = Path(tempfile.mkdtemp()); (disk / "aiwit").mkdir()
path, url = save(Coord(cfg, f"{disk}/aiwit/{{date}}"), UDID, CLIP, img)
check("{date}: ecrit dans <disque>/aiwit/20261031/<clip>.jpg", path == f"{disk}/aiwit/20261031/20261031145156.jpg" and Path(path).read_bytes() == img)
check("{date}: image_url None", url is None)
check("{date}: aucun fichier sous www / config", tree(cfg) == [])
check("{date}: un seul fichier (pas de last, pas de .part)", tree(disk) == ["aiwit/20261031/20261031145156.jpg"])
# meme nom que l'archive (<stem>.jpg dans le dossier du jour)
check("{date}: meme nom que le jpg de l'archive", Path(path).name == re.sub(r"[^A-Za-z0-9._-]+", "_", Path(CLIP).stem) + ".jpg")

# 1bis) la date vient du NOM du clip, pas de l'horloge
cfg1 = Path(tempfile.mkdtemp()); disk1 = Path(tempfile.mkdtemp()); (disk1 / "aiwit").mkdir()
path, _ = save(Coord(cfg1, f"{disk1}/aiwit/{{date}}"), UDID, "20260715083012.ts2", img)
check("{date}: date prise dans le nom du clip (15/07/2026)", path == f"{disk1}/aiwit/20260715/20260715083012.jpg")

# 2) mode {date}, disque absent : rien n'est cree
cfg2 = Path(tempfile.mkdtemp()); gone = Path(tempfile.mkdtemp()) / "Crucial_X9"
try:
    save(Coord(cfg2, f"{gone}/aiwit/{{date}}"), UDID, CLIP, img); check("{date}: disque absent -> erreur", False)
except RuntimeError as e:
    check("{date}: disque absent -> erreur claire", "disque non monte" in str(e))
check("{date}: disque absent -> aucun dossier cree", not gone.exists() and tree(cfg2) == [])

# 3) comportements historiques inchanges
cfg3 = Path(tempfile.mkdtemp())
path, url = save(Coord(cfg3, ""), UDID, CLIP, img)
check("defaut: www/aiwit + last + /local/", path.endswith("www/aiwit/62C2389EC354_last.jpg") and url == "/local/aiwit/62C2389EC354_last.jpg" and len(tree(cfg3)) == 2)
flat = Path(tempfile.mkdtemp())
path, url = save(Coord(cfg3, str(flat / "thumbs")), UDID, CLIP, img)
check("chemin absolu simple: inchange (last + clip, pas d'URL)", url is None and len(tree(flat)) == 2)
sys.exit(1 if fails else 0)
