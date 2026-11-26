#!/usr/bin/env python3
"""Rétablit l'ordre des photos gallery-dl (extracteur Facebook) pour Immich.

Pour chaque photo, le script :
  1. reconstruit l'ordre réel de l'album (chaîne "next_photo_id", sinon "num"),
  2. calcule une date cible (date du JSON + décalage pour les lots publiés à la
     même seconde),
  3. écrit cette date dans les métadonnées du fichier (EXIF pour les
     images, XMP pour les GIF, QuickTime pour les vidéos) via exiftool, ce qui
     est la source que lit Immich en priorité ; les sous-secondes laissées par
     un passage précédent sont effacées quand la cible tombe sur une seconde
     ronde,
  4. relit les fichiers écrits pour vérifier que la date y est bien,
  5. pose ensuite cette même date comme mtime du fichier. En DERNIER :
     exiftool -P ne restitue le mtime qu'à la seconde près, ce qui faisait
     perdre les fractions de seconde (--step < 1) au premier passage.

Idempotent : la date cible vient toujours des JSON, jamais du mtime courant.
Ne modifie ni les noms ni les pixels des fichiers.

Prérequis : exiftool (Debian/Ubuntu : sudo apt install libimage-exiftool-perl)

Usage :
    python3 fix_order.py [options] DOSSIER...

  Écriture
    --dry-run        affiche le plan sans rien écrire
    --only-report    synonyme de --dry-run (conservé pour compatibilité)
    --step N         décalage en secondes entre deux photos d'un même lot
                     (défaut 1 ; fractions acceptées, ex. 0.5)
    --tz ZONE        fuseau pour l'heure écrite dans l'EXIF (ex. Europe/Berlin).
                     Défaut : fuseau local de la machine.
    --no-exif        ne touche qu'au mtime
    --exif-missing   n'écrit l'EXIF que si DateTimeOriginal est absent
    --force-exif     réécrit l'EXIF même si déjà à jour (par défaut, les
                     fichiers déjà corrects sont ignorés)
    --no-verify      ne relit pas les fichiers après écriture

  Journal
    -v, --verbose    détail fichier par fichier sur la console (niveau DEBUG)
    -q, --quiet      uniquement les avertissements et erreurs
    --log-file FICHIER
                     écrit un journal complet (toujours niveau DEBUG, avec
                     horodatage) dans FICHIER, quel que soit -v / -q

Code de retour : 0 si tout va bien, 1 si au moins une vraie erreur est
survenue (écriture exiftool refusée, vérification échouée, mtime impossible).
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".m4v", ".mov"}
EXTENSIONS = IMAGES | VIDEOS | {".gif"}
LOT_EXIFTOOL = 200          # fichiers par appel d'exiftool (écriture)
LOT_LECTURE = 1000          # fichiers par appel d'exiftool (lecture)
TOLERANCE_MTIME = 0.001     # secondes

log = logging.getLogger("fix_order")
STATS = Counter()
TOTAUX = Counter()          # pour le résumé final (tous albums confondus)

# clé -> description affichée dans le résumé final (ordre d'affichage)
CAS = {
    "sans_json": "images sans fichier JSON (ignorées : ni mtime ni EXIF)",
    "json_illisible": "JSON illisibles ou invalides (ignorés)",
    "extension_non_geree": "fichiers avec JSON mais extension non gérée",
    "sans_set_id": "JSON sans set_id (regroupés dans un set '?')",
    "sans_id": "JSON sans id de photo (identifiant de repli utilisé)",
    "sans_date": "photos sans date exploitable (ignorées)",
    "date_suspecte": "dates suspectes (avant 2004 ou dans le futur)",
    "dates_differentes_copies": "copies d'une même photo avec dates différentes",
    "mode_num": "albums ordonnés par 'num' (pas de next_photo_id)",
    "num_collisions": "albums avec numéros 'num' dupliqués",
    "next_absent": "albums avec plusieurs ruptures de chaîne (incomplets ?)",
    "chaine_fusion": "chaînes qui rejoignent une photo déjà placée",
    "chaine_cycle": "photos prises dans un cycle (ordre estimé)",
    "album_en_segments": "albums en plusieurs segments",
    "lots_decales": "lots à la même seconde étalés (information)",
    "lot_deborde": "lots étalés qui débordent sur le lot suivant",
    "fichier_disparu": "fichiers disparus pendant l'exécution",
    "mtime_echec": "échecs d'écriture du mtime",
    "exif_non_lus": "fichiers que exiftool n'a pas pu lire",
    "exiftool_warning": "avertissements d'exiftool",
    "exiftool_erreur": "erreurs d'écriture d'exiftool",
    "exiftool_decompte": "lots où exiftool a rapporté moins de fichiers que prévu",
    "verif_echec": "fichiers dont la vérification après écriture a échoué",
}
ERREURS = {"exiftool_erreur", "verif_echec", "mtime_echec"}

RE_MAJ = re.compile(r"^\s*(\d+) (?:image )?files updated")
RE_ECHEC = re.compile(r"^\s*(\d+) files weren't updated due to errors")
RE_COND = re.compile(r"^\s*(\d+) (?:image )?files failed condition")
RE_INCH = re.compile(r"^\s*(\d+) (?:image )?files unchanged")


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------
class FormatConsole(logging.Formatter):
    def format(self, rec):
        msg = rec.getMessage()
        if rec.levelno >= logging.ERROR:
            return f"ERREUR : {msg}"
        if rec.levelno >= logging.WARNING:
            return f"ATTENTION : {msg}"
        if rec.levelno < logging.INFO:
            return f"    {msg}"
        return msg


def setup_logging(verbose, quiet, fichier):
    for flux in (sys.stdout, sys.stderr):
        try:  # noms de fichiers exotiques : ne jamais planter à l'affichage
            flux.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    log.setLevel(logging.DEBUG)
    log.propagate = False
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose
                else logging.WARNING if quiet else logging.INFO)
    ch.setFormatter(FormatConsole())
    log.addHandler(ch)
    if fichier:
        fh = logging.FileHandler(fichier, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)


def evt(cle, msg, niveau=logging.WARNING):
    """Journalise un cas particulier et l'ajoute au résumé final."""
    STATS[cle] += 1
    if cle in ERREURS and niveau < logging.ERROR:
        niveau = logging.ERROR
    log.log(niveau, msg)


def fmt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------
# Lecture des JSON
# --------------------------------------------------------------------------
def parse_date(valeur):
    """Convertit le champ 'date' du JSON en datetime UTC, ou None."""
    if isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        try:
            return datetime.fromtimestamp(valeur, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(valeur, str) or not valeur:
        return None
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
              "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
              "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(valeur, f)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def collecte(racine):
    """Parcourt récursivement et regroupe les JSON par set_id."""
    sets = defaultdict(list)
    ignores = Counter()
    nb_media = 0
    for dossier, _, noms in os.walk(racine):
        presents = set(noms)
        for nom in sorted(noms):
            chemin = os.path.join(dossier, nom)
            base, ext = os.path.splitext(nom)
            ext_l = ext.lower()
            if ext_l == ".json":
                if base not in presents:
                    log.debug(f"{chemin} : JSON sans média associé "
                              f"(JSON d'album, ou média absent)")
                continue
            if ext_l not in EXTENSIONS:
                if nom + ".json" in presents:
                    evt("extension_non_geree",
                        f"{chemin} : extension '{ext}' non gérée alors "
                        f"qu'un JSON existe -> ignoré (ni mtime ni EXIF)")
                else:
                    ignores[ext_l or "(sans extension)"] += 1
                continue
            nb_media += 1
            j = chemin + ".json"
            if nom + ".json" not in presents:
                evt("sans_json",
                    f"{chemin} : pas de JSON associé -> ignoré "
                    f"(ni mtime ni EXIF)")
                continue
            try:
                with open(j, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                evt("json_illisible", f"{j} : JSON illisible ({e}) -> ignoré")
                continue
            if not isinstance(data, dict):
                evt("json_illisible",
                    f"{j} : le JSON n'est pas un objet "
                    f"({type(data).__name__}) -> ignoré")
                continue
            sid = data.get("set_id")
            if sid in (None, ""):
                evt("sans_set_id",
                    f"{j} : pas de 'set_id' -> rangé dans le set '?' "
                    f"(des albums différents peuvent se mélanger)")
                sid = "?"
            sets[str(sid)].append({"image": chemin, "json": j, "data": data})
    log.debug(f"{racine} : {nb_media} médias, {len(sets)} set(s)")
    if ignores:
        log.info(f"{racine} : fichiers ignorés (extension non gérée) : "
                 f"{dict(ignores)}")
    return sets


# --------------------------------------------------------------------------
# Ordre de l'album
# --------------------------------------------------------------------------
def chaine(photos, tag):
    """Reconstruit l'ordre de l'album depuis next_photo_id.

    Retourne une liste de composantes (listes ordonnées d'ids).
    """
    suivants, absents = {}, []
    for pid, p in photos.items():
        nxt = p["data"].get("next_photo_id")
        if nxt in (None, ""):
            continue
        nxt = str(nxt)
        if nxt == pid:
            log.debug(f"{tag} photo {pid} : next_photo_id pointe sur "
                      f"elle-même, ignoré")
            continue
        if nxt not in photos:
            absents.append((pid, nxt))
            continue
        suivants[pid] = nxt

    # Une chaîne complète a au plus une photo finale dont la suivante est
    # absente (la dernière) ; au-delà, des photos manquent au milieu.
    if len(absents) > 1:
        evt("next_absent",
            f"{tag} {len(absents)} photos pointent vers une suivante absente "
            f"(album incomplet ?), p.ex. {absents[0][0]} -> {absents[0][1]}")
        for pid, nxt in absents:
            log.debug(f"{tag}   {pid} -> {nxt} (absent)")
    elif absents:
        log.debug(f"{tag} fin de chaîne : {absents[0][0]} pointe vers "
                  f"{absents[0][1]} qui n'est pas dans les fichiers")

    pointes = set(suivants.values())
    tetes = sorted(pid for pid in photos if pid not in pointes)
    composantes, vus = [], set()
    for tete in tetes:
        seq, cur = [], tete
        while cur is not None:
            if cur in vus:
                evt("chaine_fusion",
                    f"{tag} la chaîne partant de {tete} rejoint la photo "
                    f"{cur} déjà placée (boucle, ou deux photos pointant vers "
                    f"la même suivante) -> chaîne coupée ici")
                break
            seq.append(cur)
            vus.add(cur)
            cur = suivants.get(cur)
        composantes.append(seq)

    reste = sorted(pid for pid in photos if pid not in vus)
    if reste:
        evt("chaine_cycle",
            f"{tag} {len(reste)} photo(s) sans tête de chaîne (cycle) -> "
            f"ordre estimé par identifiant")
        composantes.append(reste)

    log.debug(f"{tag} chaîne : {len(tetes)} tête(s), {len(composantes)} "
              f"segment(s), longueurs {[len(c) for c in composantes]}")
    if len(composantes) > 1:
        evt("album_en_segments",
            f"{tag} album en {len(composantes)} segments "
            f"(longueurs {[len(c) for c in composantes]}) : ordre entre "
            f"segments estimé. Si l'album a été complété depuis, relancer "
            f"gallery-dl puis ce script.")
    return composantes


def ordre_num(photos, tag):
    """Repli : ordre par 'num' quand la chaîne est indisponible."""
    par_num = defaultdict(list)
    sans_num = [pid for pid, p in photos.items()
                if not p["data"].get("num")]
    for pid, p in photos.items():
        par_num[p["data"].get("num")].append(pid)
    collisions = {n: ids for n, ids in par_num.items()
                  if len(ids) > 1 and n}
    if collisions:
        exemples = list(collisions.items())[:3]
        evt("num_collisions",
            f"{tag} numéros 'num' dupliqués pour {len(collisions)} valeur(s) "
            f"(reprise de téléchargement ?) -> ordre incertain, p.ex. "
            f"{exemples}")
    if sans_num:
        log.warning(f"{tag} {len(sans_num)} photo(s) sans 'num' "
                    f"(traitées comme 0)")

    def cle(pid):
        p = photos[pid]
        return (p["data"].get("num") or 0, str(p["data"].get("id")))
    return [sorted(photos, key=cle)]


# --------------------------------------------------------------------------
# Plan de dates
# --------------------------------------------------------------------------
def plan_mtimes(composantes, photos, pas, tag):
    """Calcule (image, date_cible_utc) pour TOUTES les photos.

    Les photos publiées à la même seconde sont étalées selon l'ordre de
    l'album ; les photos seules gardent leur date (à la seconde).
    """
    ordre = [pid for comp in composantes for pid in comp]
    par_date = defaultdict(list)
    for pid in ordre:
        par_date[photos[pid]["date"].replace(microsecond=0)].append(pid)
    bases = sorted(par_date)
    plan = []
    for i, d in enumerate(bases):
        groupe = par_date[d]
        n = len(groupe)
        decalage_max = (n - 1) * pas
        if n > 1:
            STATS["lots_decales"] += 1
            log.info(f"{tag} lot de {n} photos publiées à {fmt(d)} : "
                     f"étalées jusqu'à +{decalage_max:g}s")
            if (i + 1 < len(bases) and
                    d + timedelta(seconds=decalage_max) >= bases[i + 1]):
                evt("lot_deborde",
                    f"{tag} le lot de {fmt(d)} (jusqu'à "
                    f"{fmt(d + timedelta(seconds=decalage_max))}) déborde sur "
                    f"le lot suivant à {fmt(bases[i + 1])} : les photos des "
                    f"deux lots peuvent s'entremêler. Essayer un --step plus "
                    f"petit.")
        for rang, pid in enumerate(groupe):
            cible = d + timedelta(seconds=rang * pas)
            for img in photos[pid]["images"]:
                log.debug(f"{tag} {os.path.basename(img)} : JSON "
                          f"{fmt(photos[pid]['date'])} -> cible {fmt(cible)} "
                          f"(rang {rang + 1}/{n})")
                plan.append((img, cible))
    return plan


def applique_mtime(plan, dry_run):
    modifies = inchanges = 0
    for image, cible in plan:
        ts = cible.timestamp()
        try:
            courant = os.stat(image).st_mtime
        except FileNotFoundError:
            evt("fichier_disparu",
                f"{image} : introuvable au moment d'écrire le mtime")
            continue
        except OSError as e:
            evt("mtime_echec", f"{image} : stat impossible ({e})")
            continue
        if abs(courant - ts) < TOLERANCE_MTIME:
            inchanges += 1
            log.debug(f"{image} : mtime déjà correct")
            continue
        avant = datetime.fromtimestamp(courant, tz=timezone.utc)
        log.debug(f"{image} : mtime {fmt(avant)} -> {fmt(cible)}"
                  f"{' [dry-run]' if dry_run else ''}")
        if not dry_run:
            try:
                os.utime(image, (ts, ts))
            except OSError as e:
                evt("mtime_echec", f"{image} : écriture du mtime impossible "
                                   f"({e})")
                continue
        modifies += 1
    return modifies, inchanges


# --------------------------------------------------------------------------
# EXIF via exiftool
# --------------------------------------------------------------------------
def tags_pour(image, cible_utc, tz):
    """Liste d'arguments exiftool pour un fichier."""
    ext = os.path.splitext(image)[1].lower()
    local = cible_utc.astimezone(tz)
    date = local.strftime("%Y:%m:%d %H:%M:%S")
    off = local.strftime("%z")
    off = f"{off[:3]}:{off[3:]}"
    if ext in VIDEOS:
        utc = cible_utc.strftime("%Y:%m:%d %H:%M:%S")
        return [f"-QuickTime:CreateDate={utc}",
                f"-QuickTime:ModifyDate={utc}",
                f"-QuickTime:TrackCreateDate={utc}",
                f"-QuickTime:MediaCreateDate={utc}"]
    if ext == ".gif":
        return [f"-XMP-photoshop:DateCreated={date}{off}"]
    tags = [f"-DateTimeOriginal={date}", f"-CreateDate={date}",
            f"-OffsetTimeOriginal={off}", f"-OffsetTimeDigitized={off}",
            f"-OffsetTime={off}"]
    if local.microsecond:
        ms = f"{local.microsecond // 1000:03d}"
        tags += [f"-SubSecTimeOriginal={ms}", f"-SubSecTimeDigitized={ms}"]
    else:
        # effacer les sous-secondes d'un passage précédent : sinon la photo
        # reste décalée (ex. +0,9 s), la vérification échoue et le fichier
        # est réécrit à chaque passage
        tags += ["-SubSecTimeOriginal=", "-SubSecTimeDigitized="]
    return tags


def valeurs_attendues(cible_utc, tz):
    """(date, sous-seconde, décalage) tels que exiftool les affiche."""
    local = cible_utc.astimezone(tz)
    off = local.strftime("%z")
    ms = f"{local.microsecond // 1000:03d}" if local.microsecond else "-"
    return (local.strftime("%Y:%m:%d %H:%M:%S"), ms, f"{off[:3]}:{off[3:]}")


def cle_chemin(chemin):
    return os.path.normpath(os.path.abspath(chemin))


def lance_exiftool(lignes):
    """Écrit un argfile, lance exiftool, retourne le CompletedProcess."""
    with tempfile.NamedTemporaryFile(
            "w", suffix=".args", delete=False, encoding="utf-8") as tf:
        tf.write("\n".join(lignes) + "\n")
        argfile = tf.name
    try:
        return subprocess.run(
            ["exiftool", "-@", argfile], capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    finally:
        os.unlink(argfile)


def lit_existant(chemins):
    """Lit en un seul passage rapide les dates EXIF déjà présentes.

    Retourne {chemin_absolu: (date, sous-seconde, décalage)} ; '-' = absent.
    """
    res = {}
    for debut in range(0, len(chemins), LOT_LECTURE):
        lot = chemins[debut:debut + LOT_LECTURE]
        r = lance_exiftool(
            ["-charset", "filename=utf8", "-charset", "utf8", "-f", "-p",
             "$Directory/$FileName||$DateTimeOriginal||"
             "$SubSecTimeOriginal||$OffsetTimeOriginal"] + lot)
        log.debug(f"exiftool lecture : code retour {r.returncode}, "
                  f"{len(lot)} fichier(s) demandé(s)")
        lus = 0
        for ligne in r.stdout.splitlines():
            m = ligne.split("||")
            if len(m) == 4:
                res[os.path.normpath(m[0])] = tuple(m[1:])
                lus += 1
        for ligne in r.stderr.splitlines():
            if ligne.startswith(("Error", "File not found")):
                log.warning(f"exiftool (lecture) : {ligne}")
            else:
                log.debug(f"exiftool (lecture) : {ligne}")
        if lus < len(lot):
            evt("exif_non_lus",
                f"exiftool n'a pas pu lire {len(lot) - lus} fichier(s) sur "
                f"{len(lot)} (fichier corrompu ou format inattendu ?) -> "
                f"ils seront traités comme sans EXIF")
    return res


def filtre_a_ecrire(plan, tz, force):
    """Ne garde que les fichiers dont l'EXIF diffère de la date cible."""
    fichiers = []
    for i, c in plan:
        if os.path.exists(i):
            fichiers.append((i, c))
        else:
            log.debug(f"{i} : introuvable, EXIF ignoré")
    if force:
        log.debug("--force-exif : tous les fichiers seront réécrits")
        return fichiers, 0
    images = [cle_chemin(i) for i, _ in fichiers
              if os.path.splitext(i)[1].lower() in IMAGES]
    existant = lit_existant(images)
    a_ecrire, deja_ok = [], 0
    for image, cible in fichiers:
        ext = os.path.splitext(image)[1].lower()
        attendu = valeurs_attendues(cible, tz)
        if ext not in IMAGES:
            log.debug(f"{image} : vidéo/GIF, toujours écrit")
            a_ecrire.append((image, cible))
            continue
        actuel = existant.get(cle_chemin(image))
        if actuel == attendu:
            deja_ok += 1
            log.debug(f"{image} : EXIF déjà à jour {attendu}")
        else:
            log.debug(f"{image} : EXIF {actuel or 'non lu'} != attendu "
                      f"{attendu} -> à écrire")
            a_ecrire.append((image, cible))
    return a_ecrire, deja_ok


def ecrit_exif(fichiers, tz, seulement_manquant):
    """Écrit les dates dans les fichiers, par lots. Retourne le nb écrit."""
    ecrits = 0
    nb_lots = (len(fichiers) + LOT_EXIFTOOL - 1) // LOT_EXIFTOOL
    for n, debut in enumerate(range(0, len(fichiers), LOT_EXIFTOOL), 1):
        lot = fichiers[debut:debut + LOT_EXIFTOOL]
        lignes = []
        for image, cible in lot:
            lignes += ["-P", "-overwrite_original_in_place",
                       "-api", "QuickTimeUTC=1",
                       "-charset", "filename=utf8"]
            if seulement_manquant and \
                    os.path.splitext(image)[1].lower() in IMAGES:
                lignes += ["-if", "not $DateTimeOriginal"]
            lignes += tags_pour(image, cible, tz)
            lignes += [cle_chemin(image), "-execute"]
        t0 = time.monotonic()
        r = lance_exiftool(lignes)
        compte = Counter()
        for ligne in (r.stdout + "\n" + r.stderr).splitlines():
            if not ligne.strip() or ligne.strip() == "{ready}":
                continue
            for cle, rx in (("maj", RE_MAJ), ("echec", RE_ECHEC),
                            ("cond", RE_COND), ("inch", RE_INCH)):
                m = rx.match(ligne)
                if m:
                    compte[cle] += int(m.group(1))
                    break
            else:
                if "not defined" in ligne:
                    log.debug(f"exiftool : {ligne}")
                elif ligne.startswith("Error"):
                    evt("exiftool_erreur", f"exiftool : {ligne}")
                elif ligne.startswith("Warning"):
                    evt("exiftool_warning", f"exiftool : {ligne}")
                else:
                    log.debug(f"exiftool : {ligne}")
        total = sum(compte.values())
        log.debug(f"exiftool écriture lot {n}/{nb_lots} : {len(lot)} "
                  f"fichier(s) en {time.monotonic() - t0:.1f}s, code "
                  f"{r.returncode}, {dict(compte)}")
        if total < len(lot):
            evt("exiftool_decompte",
                f"exiftool a rapporté {total} fichier(s) traité(s) pour "
                f"{len(lot)} envoyé(s) (lot {n}/{nb_lots}) : voir les "
                f"erreurs ci-dessus ou le journal")
        ecrits += compte["maj"]
    return ecrits


def verifie(fichiers, tz):
    """Relit les images écrites et compare avec la date cible."""
    images = [(i, c) for i, c in fichiers
              if os.path.splitext(i)[1].lower() in IMAGES]
    if not images:
        return 0
    lu = lit_existant([cle_chemin(i) for i, _ in images])
    ko = 0
    for image, cible in images:
        attendu = valeurs_attendues(cible, tz)
        actuel = lu.get(cle_chemin(image))
        if actuel != attendu:
            ko += 1
            evt("verif_echec",
                f"{image} : après écriture, EXIF lu = {actuel or 'illisible'}"
                f", attendu = {attendu}")
    return ko


# --------------------------------------------------------------------------
def traite_set(set_id, entries, args, tz):
    tag = f"[set {set_id}]"
    photos, sans_date, dossiers = {}, [], set()
    maintenant = datetime.now(timezone.utc)
    for e in entries:
        data, img = e["data"], e["image"]
        brut_id = data.get("id")
        if brut_id in (None, ""):
            pid = "fichier:" + os.path.basename(img)
            evt("sans_id", f"{tag} {img} : pas de champ 'id' -> identifiant "
                           f"de repli '{pid}'")
        else:
            pid = str(brut_id)
        brut = data.get("date")
        d = parse_date(brut)
        if d is None:
            sans_date.append(img)
            evt("sans_date", f"{tag} {img} : date absente ou illisible "
                             f"(valeur : {brut!r}) -> ignoré")
            continue
        if d.year < 2004 or d > maintenant + timedelta(days=1):
            evt("date_suspecte", f"{tag} {img} : date suspecte {fmt(d)} "
                                 f"(valeur brute : {brut!r})")
        dossiers.add(os.path.dirname(img))
        if pid in photos:
            photos[pid]["images"].append(img)
            log.debug(f"{tag} photo {pid} : copie supplémentaire {img}")
            if photos[pid]["date"] != d:
                evt("dates_differentes_copies",
                    f"{tag} photo {pid} : dates différentes entre copies "
                    f"({fmt(photos[pid]['date'])} vs {fmt(d)}) dans {img} -> "
                    f"la première date est conservée")
            continue
        photos[pid] = {"images": [img], "data": data, "date": d}

    if not photos:
        log.warning(f"{tag} aucune photo exploitable, ignoré")
        return
    if len(dossiers) > 1:
        log.info(f"{tag} set réparti sur {len(dossiers)} dossiers "
                 f"(même album extrait plusieurs fois ?)")
        for dos in sorted(dossiers):
            log.debug(f"{tag}   {dos}")

    nb_next = sum(1 for p in photos.values()
                  if p["data"].get("next_photo_id"))
    log.debug(f"{tag} {len(entries)} fichier(s), {len(photos)} photo(s) "
              f"unique(s), {nb_next} avec next_photo_id")
    if nb_next == 0:
        if len(photos) > 1:
            evt("mode_num", f"{tag} pas de next_photo_id dans les JSON -> "
                            f"ordre par 'num' (repli)")
        else:
            log.debug(f"{tag} une seule photo : ordre sans objet")
        composantes = ordre_num(photos, tag)
    else:
        composantes = chaine(photos, tag)

    plan = plan_mtimes(composantes, photos, args.step, tag)
    dry = args.dry_run or args.only_report
    TOTAUX["albums"] += 1
    TOTAUX["photos"] += len(photos)
    TOTAUX["fichiers"] += len(plan)
    exif_change = 0

    exif_txt = ""
    if not args.no_exif:
        if dry and shutil.which("exiftool") is None:
            exif_txt = (f", EXIF : exiftool absent, impossible de comparer "
                        f"({len(plan)} fichier(s) au maximum)")
            TOTAUX["exif_inconnu"] += len(plan)
        else:
            a_ecrire, deja_ok = filtre_a_ecrire(plan, tz, args.force_exif)
            TOTAUX["exif_ok"] += deja_ok
            if dry:
                exif_txt = (f", EXIF à écrire sur {len(a_ecrire)} "
                            f"fichier(s), {deja_ok} déjà à jour")
                TOTAUX["exif_a_ecrire"] += len(a_ecrire)
                exif_change = len(a_ecrire)
            else:
                n = ecrit_exif(a_ecrire, tz, args.exif_missing)
                exif_txt = (f", EXIF écrit sur {n}/{len(a_ecrire)} "
                            f"fichier(s), {deja_ok} déjà à jour")
                TOTAUX["exif_ecrits"] += n
                TOTAUX["exif_a_ecrire"] += len(a_ecrire)
                exif_change = n
                if a_ecrire and not args.no_verify \
                        and not args.exif_missing:
                    ko = verifie(a_ecrire, tz)
                    TOTAUX["verifies"] += len(a_ecrire)
                    TOTAUX["verif_ko"] += ko
                    exif_txt += (", vérification OK" if ko == 0
                                 else f", vérification : {ko} échec(s)")

    # mtime en DERNIER : exiftool -P (écriture EXIF ci-dessus) restitue le
    # mtime à la seconde près ; le poser après garde les fractions de seconde
    modifies, inchanges = applique_mtime(plan, dry)
    TOTAUX["mtime_maj"] += modifies
    TOTAUX["mtime_ok"] += inchanges
    if modifies or exif_change:
        TOTAUX["albums_modifies"] += 1

    log.info(f"{tag} {len(photos)} photos, {modifies} mtime mis à jour, "
             f"{inchanges} déjà corrects{exif_txt}"
             f"{' [dry-run]' if dry else ''}")


def resume(dry, args):
    """Résumé final, tous albums confondus."""
    t = TOTAUX
    if not t["albums"]:
        log.info("Résumé : aucun album traité.")
        return
    a_jour = t["albums"] - t["albums_modifies"]
    titre = "Résumé (DRY-RUN : rien n'a été écrit) :" if dry else "Résumé :"
    lignes = [
        ("albums traités", f"{t['albums']} ({t['albums_modifies']} "
                           f"{'à modifier' if dry else 'modifiés'}, {a_jour} déjà à jour)"),
        ("photos", f"{t['photos']} ({t['fichiers']} fichiers)"),
        ("mtime à mettre à jour" if dry else "mtime mis à jour",
         f"{t['mtime_maj']} ({t['mtime_ok']} déjà corrects)"),
    ]
    if args.no_exif:
        lignes.append(("EXIF", "non traité (--no-exif)"))
    elif t["exif_inconnu"]:
        lignes.append(("EXIF", f"exiftool absent : {t['exif_inconnu']} fichier(s) non comparés"))
    elif dry:
        lignes.append(("EXIF à écrire", f"{t['exif_a_ecrire']} ({t['exif_ok']} déjà à jour)"))
    else:
        ecart = t["exif_a_ecrire"] - t["exif_ecrits"]
        lignes.append(("EXIF écrit", f"{t['exif_ecrits']} ({t['exif_ok']} déjà à jour"
                                     f"{f', {ecart} non écrit(s)' if ecart else ''})"))
        if t["verifies"]:
            lignes.append(("vérification", "OK" if not t["verif_ko"]
                           else f"{t['verif_ko']} échec(s) sur {t['verifies']}"))
        elif t["exif_ecrits"] and (args.no_verify or args.exif_missing):
            lignes.append(("vérification", "non faite (--no-verify / --exif-missing)"))
    log.info(titre)
    for libelle, valeur in lignes:
        log.info(f"  {libelle:<22}: {valeur}")


def version_exiftool():
    if shutil.which("exiftool") is None:
        return None
    try:
        r = subprocess.run(["exiftool", "-ver"], capture_output=True,
                           text=True)
        return r.stdout.strip() or None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(
        description="Ordonne mtime + EXIF des photos gallery-dl pour Immich.")
    ap.add_argument("dossiers", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-report", action="store_true")
    ap.add_argument("--step", type=float, default=1.0,
                    help="secondes entre deux photos d'un même lot (défaut 1)")
    ap.add_argument("--tz", default=None,
                    help="fuseau de l'heure EXIF, ex. Europe/Berlin "
                         "(défaut : fuseau local)")
    ap.add_argument("--no-exif", action="store_true",
                    help="ne modifier que le mtime")
    ap.add_argument("--force-exif", action="store_true",
                    help="réécrire l'EXIF même s'il est déjà à jour")
    ap.add_argument("--exif-missing", action="store_true",
                    help="n'écrire l'EXIF que si DateTimeOriginal est absent")
    ap.add_argument("--no-verify", action="store_true",
                    help="ne pas relire les fichiers après écriture")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="détail fichier par fichier")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="uniquement avertissements et erreurs")
    ap.add_argument("--log-file", default=None,
                    help="journal complet (niveau DEBUG) dans ce fichier")
    args = ap.parse_args()

    setup_logging(args.verbose, args.quiet, args.log_file)
    debut = time.monotonic()
    dry = args.dry_run or args.only_report

    vexif = version_exiftool()
    if not args.no_exif and not dry and vexif is None:
        sys.exit("exiftool introuvable (sudo apt install "
                 "libimage-exiftool-perl) ou utiliser --no-exif")

    if args.tz:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(args.tz)
        except Exception as e:
            sys.exit(f"fuseau invalide '{args.tz}' : {e}")
    else:
        tz = datetime.now().astimezone().tzinfo

    log.info(f"fix_order : fuseau EXIF {tz}, step {args.step:g}s, "
             f"exiftool {vexif or 'absent'}"
             f"{', DRY-RUN' if dry else ''}")
    log.debug(f"arguments : {vars(args)}")

    for racine in args.dossiers:
        if not os.path.isdir(racine):
            sys.exit(f"dossier introuvable : {racine}")
        log.info(f"== {racine}")
        sets = collecte(racine)
        if not sets:
            log.warning(f"{racine} : aucun JSON MetadataPP trouvé")
        for set_id in sorted(sets):
            traite_set(set_id, sets[set_id], args, tz)

    log.info("")
    resume(dry, args)
    log.info(f"Terminé en {time.monotonic() - debut:.1f}s")
    cas = [(CAS[k], STATS[k]) for k in CAS if STATS[k]]
    if cas:
        log.info("Cas particuliers rencontrés :")
        for libelle, n in cas:
            log.info(f"  {n:6d}  {libelle}")
        if not args.verbose and not args.log_file:
            log.info("Relancer avec -v ou --log-file FICHIER pour le détail.")
    else:
        log.info("Aucun cas particulier.")
    sys.exit(1 if any(STATS[k] for k in ERREURS) else 0)


if __name__ == "__main__":
    main()
