"""
date_estimee.py — calcul de la date de livraison estimée
Utilisé par admin.py et commandes.py
"""
from datetime import datetime, timedelta
import re

# Jours ouvrés (lun-sam, pas dimanche)
JOURS_OUVRES = {0,1,2,3,4,5}  # 0=lundi ... 5=samedi

def _ajouter_jours_ouvres(debut: datetime, nb_jours: int) -> datetime:
    """Ajoute nb_jours ouvrés à une date."""
    d = debut
    ajoutes = 0
    while ajoutes < nb_jours:
        d += timedelta(days=1)
        if d.weekday() in JOURS_OUVRES:
            ajoutes += 1
    return d

def _extraire_bornes(delai_str: str) -> tuple[int, int] | None:
    """
    Extrait (min, max) jours depuis une chaîne de délai.
    Exemples : "15-25j", "15–25 jours", "7-10 jours", "3 semaines"
    """
    if not delai_str or delai_str == "—":
        return None

    s = delai_str.lower().replace("–", "-").replace("—", "-")

    # Cas "X semaines" ou "X-Y semaines"
    sem = re.search(r'(\d+)\s*[-à]\s*(\d+)\s*sem', s)
    if sem:
        return int(sem.group(1)) * 7, int(sem.group(2)) * 7
    sem1 = re.search(r'(\d+)\s*sem', s)
    if sem1:
        n = int(sem1.group(1)) * 7
        return n, n

    # Cas "X-Y j" ou "X-Y jours"
    rng = re.search(r'(\d+)\s*[-à]\s*(\d+)', s)
    if rng:
        return int(rng.group(1)), int(rng.group(2))

    # Cas "X j" ou "X jours"
    single = re.search(r'(\d+)', s)
    if single:
        n = int(single.group(1))
        return n, n

    return None


MOIS_FR = ["janv","févr","mars","avr","mai","juin",
           "juil","août","sept","oct","nov","déc"]

def _fmt(d: datetime) -> str:
    return f"{d.day} {MOIS_FR[d.month-1]} {d.year}"


def calculer_date_estimee(created_at: datetime, delai_str: str) -> str:
    """
    Retourne une chaîne lisible : "entre le 3 mai et le 18 mai 2025"
    ou "vers le 10 mai 2025" si min==max.
    Retourne "" si impossible à calculer.
    """
    bornes = _extraire_bornes(delai_str)
    if not bornes:
        return ""

    debut = created_at or datetime.now()
    dmin, dmax = bornes

    date_min = _ajouter_jours_ouvres(debut, dmin)
    date_max = _ajouter_jours_ouvres(debut, dmax)

    if dmin == dmax:
        return f"vers le {_fmt(date_min)}"
    return f"entre le {_fmt(date_min)} et le {_fmt(date_max)}"
