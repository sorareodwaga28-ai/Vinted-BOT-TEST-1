"""Lien entre le repo et la page « Vérification batterie » (outil pour Claude).

  python page_sync.py vignettes <slug>
      Liste les vignettes à envoyer comme fichiers de la page (Artifact, asset: true,
      25 par envoi). Noter ensuite « <id annonce> <id fichier> » par ligne dans un fichier.

  python page_sync.py annonces <slug> <dossier> <fichier_correspondance>
      Prépare les documents à écrire dans la base de la page :
      <dossier>/batteries/<slug>.json et <dossier>/annonces/<slug>__<id>.json
      (img = /_blob/<id fichier>), puis <dossier>/lots.json (lots de 25 écritures).

  python page_sync.py verdicts <dossier_export>
      Lit un export de la collection « verdicts » (fichiers <slug>__<id>.json,
      tels qu'écrits par ArtifactData list avec out_dir) et écrit
      verdicts/<slug>.json pour chaque modèle. Affiche ce qui a changé.
"""
import json
import sys
from pathlib import Path

ICI = Path(__file__).parent


def vignettes(slug):
    fs = sorted(str(p.resolve()) for p in (ICI / "batteries" / slug / "img").glob("*.jpg"))
    for i in range(0, len(fs), 25):
        print(json.dumps(fs[i:i + 25]))


def annonces(slug, out, corr):
    blobs = dict(l.split() for l in Path(corr).read_text().splitlines() if len(l.split()) == 2)
    res = json.loads((ICI / "batteries" / slug / "resultats.json").read_text())
    out = Path(out)
    (out / "batteries").mkdir(parents=True, exist_ok=True)
    (out / "annonces").mkdir(parents=True, exist_ok=True)
    meta = {k: res[k] for k in ("modele", "slug", "batterie", "seuil_depart", "prix_max", "codes", "nb_lues")}
    (out / "batteries" / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False))
    for a in res["annonces"]:
        doc = dict(a, slug=slug, modele=res["modele"], batterie=res["batterie"])
        doc["img"] = f"/_blob/{blobs[a['id']]}" if a["id"] in blobs else ""
        (out / "annonces" / f"{slug}__{a['id']}.json").write_text(json.dumps(doc, ensure_ascii=False))
    w = [{"op": "set", "collection": "annonces", "doc_id": f"{slug}__{a['id']}",
          "file_path": str((out / "annonces" / f"{slug}__{a['id']}.json").resolve())} for a in res["annonces"]]
    w.append({"op": "set", "collection": "batteries", "doc_id": slug,
              "file_path": str((out / "batteries" / f"{slug}.json").resolve())})
    (out / "lots.json").write_text(json.dumps([w[i:i + 25] for i in range(0, len(w), 25)]))
    print(f"batterie {res['batterie']} · {len(res['annonces'])} annonces · {sum(1 for a in res['annonces'] if a['id'] in blobs)} vignettes -> {out}")


def verdicts(dossier):
    par = {}
    for f in sorted(Path(dossier).rglob("*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        d = d.get("data", d)          # selon le format de l'export
        slug, rep = d.get("slug"), str(d.get("rep", "")).lower()
        if slug and rep in ("oui", "non"):
            par.setdefault(slug, {})[str(d["id"])] = rep
    (ICI / "verdicts").mkdir(exist_ok=True)
    for slug, v in par.items():
        rf = ICI / "batteries" / slug / "resultats.json"
        if not rf.exists():
            continue
        res = json.loads(rf.read_text())
        ids = {a["id"] for a in res["annonces"]}
        v = {k: x for k, x in sorted(v.items()) if k in ids}
        new = {"modele": res["modele"], "batterie": res["batterie"], "verdicts": v}
        f = ICI / "verdicts" / f"{slug}.json"
        old = json.loads(f.read_text()) if f.exists() else None
        if old != new:
            f.write_text(json.dumps(new, ensure_ascii=False, indent=1))
            print(f"{slug} : {len(v)} réponses écrites")
        else:
            print(f"{slug} : rien de nouveau")


if __name__ == "__main__":
    if sys.argv[1] == "vignettes":
        vignettes(sys.argv[2])
    elif sys.argv[1] == "annonces":
        annonces(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == "verdicts":
        verdicts(sys.argv[2])
