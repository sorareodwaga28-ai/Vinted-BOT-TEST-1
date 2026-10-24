"""Lien entre le repo et la page « Vérification batterie » (outil pour Claude).

  python page_sync.py etat
      Pour chaque modèle : manche en cours dans le repo (batteries/<slug>/etat.json + <V>.json).

  python page_sync.py vignettes <slug> <V>
      Listes (25 max) de vignettes à envoyer comme fichiers de la page
      (Artifact publish, url de la page, asset: true, file_paths).
      Noter ensuite « <id annonce> <id fichier> » par ligne dans un fichier.

  python page_sync.py annonces <slug> <V> <dossier> <fichier_correspondance>
      Prépare les documents de la base de la page : batteries/<slug> (manche affichée)
      et annonces/<slug>__<V>__<id> (img = /_blob/<id fichier>), puis <dossier>/lots.json
      (lots de 25 écritures pour ArtifactData batch).

  python page_sync.py verdicts <dossier_export>
      Lit un export de la collection « verdicts » de la page (ArtifactData list avec out_dir)
      et écrit verdicts/<slug>.json ({"verdicts": {"<V>:<id>": "oui"|"non"}}). Affiche ce qui a changé.
"""
import json
import sys
from pathlib import Path

ICI = Path(__file__).parent


def etat():
    for d in sorted((ICI / "batteries").glob("*/etat.json")):
        e = json.loads(d.read_text())
        f = d.parent / f"{e['etape']}.json"
        b = json.loads(f.read_text())["batterie"] if f.exists() else "pas encore générée"
        print(f"{d.parent.name} : manche {e['etape']} · batterie {b} · terminées {e.get('terminees', [])}"
              + (" · FINI" if e.get("fini") else ""))


def vignettes(slug, v):
    fs = sorted(str(p.resolve()) for p in (ICI / "batteries" / slug / "img" / v).glob("*.jpg"))
    for i in range(0, len(fs), 25):
        print(json.dumps(fs[i:i + 25]))


def annonces(slug, v, out, corr):
    blobs = dict(l.split() for l in Path(corr).read_text().splitlines() if len(l.split()) == 2)
    res = json.loads((ICI / "batteries" / slug / f"{v}.json").read_text())
    et = json.loads((ICI / "batteries" / slug / "etat.json").read_text())
    out = Path(out)
    (out / "batteries").mkdir(parents=True, exist_ok=True)
    (out / "annonces").mkdir(parents=True, exist_ok=True)
    meta = {k: res[k] for k in ("modele", "slug", "etape", "nom_etape", "batterie", "seuil", "prix_max", "codes",
                                "nb_references", "nb_pieges", "recherches_tirees_des_oui")}
    meta["terminees"] = et.get("terminees", [])
    (out / "batteries" / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False))
    w = []
    for a in res["annonces"]:
        doc = dict(a, slug=slug, modele=res["modele"], etape=v, batterie=res["batterie"])
        doc["img"] = f"/_blob/{blobs[a['id']]}" if a["id"] in blobs else ""
        k = f"{slug}__{v}__{a['id']}"
        (out / "annonces" / f"{k}.json").write_text(json.dumps(doc, ensure_ascii=False))
        w.append({"op": "set", "collection": "annonces", "doc_id": k, "file_path": str((out / "annonces" / f"{k}.json").resolve())})
    w.append({"op": "set", "collection": "batteries", "doc_id": slug, "file_path": str((out / "batteries" / f"{slug}.json").resolve())})
    (out / "lots.json").write_text(json.dumps([w[i:i + 25] for i in range(0, len(w), 25)]))
    print(f"{slug} {v} · batterie {res['batterie']} · {len(res['annonces'])} annonces · "
          f"{sum(1 for a in res['annonces'] if a['id'] in blobs)} vignettes -> {out / 'lots.json'}")


def verdicts(dossier):
    par = {}
    for f in sorted(Path(dossier).rglob("*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        d = d.get("data", d)
        slug, rep = d.get("slug"), str(d.get("rep", "")).lower()
        if slug and rep in ("oui", "non") and d.get("id"):
            par.setdefault(slug, {})[f"{d.get('etape') or 'V1'}:{d['id']}"] = rep
    (ICI / "verdicts").mkdir(exist_ok=True)
    for slug, v in par.items():
        ef = ICI / "batteries" / slug / "etat.json"
        if not ef.exists():
            continue
        f = ICI / "verdicts" / f"{slug}.json"
        old = json.loads(f.read_text()) if f.exists() else {}
        new = {"slug": slug, "verdicts": dict(sorted(v.items()))}
        if old.get("verdicts") != new["verdicts"]:
            f.write_text(json.dumps(new, ensure_ascii=False, indent=1))
            print(f"{slug} : {len(v)} réponses écrites")
        else:
            print(f"{slug} : rien de nouveau")


if __name__ == "__main__":
    a = sys.argv[1:]
    {"etat": lambda: etat(), "vignettes": lambda: vignettes(a[1], a[2]),
     "annonces": lambda: annonces(a[1], a[2], a[3], a[4]), "verdicts": lambda: verdicts(a[1])}[a[0]]()
