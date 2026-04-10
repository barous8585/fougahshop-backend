from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import json
from database import get_db
from models import Config, PortKg, Employe
from routes.auth import require_patron

router = APIRouter(prefix="/api/config", tags=["config"])

PAYS_LIST = [
    "Burkina Faso","Guinée","Cameroun","Bénin","Togo",
    "Niger","Congo","Gabon","Sénégal","Mali","Côte d'Ivoire"
]

DEFAULT_PORT = {
    "Burkina Faso":  {"prix": 8500,  "delai": "10-14 jours", "actif": False},
    "Guinée":        {"prix": 9000,  "delai": "10-15 jours", "actif": True},
    "Cameroun":      {"prix": 9500,  "delai": "10-15 jours", "actif": False},
    "Bénin":         {"prix": 7500,  "delai": "8-12 jours",  "actif": True},
    "Togo":          {"prix": 7500,  "delai": "8-12 jours",  "actif": False},
    "Niger":         {"prix": 9000,  "delai": "12-18 jours", "actif": False},
    "Congo":         {"prix": 10500, "delai": "14-21 jours", "actif": False},
    "Gabon":         {"prix": 10500, "delai": "14-21 jours", "actif": False},
    "Sénégal":       {"prix": 8000,  "delai": "8-12 jours",  "actif": True},
    "Mali":          {"prix": 8500,  "delai": "10-14 jours", "actif": False},
    "Côte d'Ivoire": {"prix": 7000,  "delai": "7-10 jours",  "actif": False},
}

ROLES_AUTORISES = ("employe", "logisticien")

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config(); db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

def init_port(db):
    for pays, info in DEFAULT_PORT.items():
        existing = db.query(PortKg).filter(PortKg.pays == pays).first()
        if not existing:
            db.add(PortKg(pays=pays, prix=info["prix"], delai=info["delai"], actif=info["actif"]))
        elif existing.actif is None:
            existing.actif = info["actif"]
    db.commit()

def ensure_role_column(db):
    try:
        db.execute(text(
            "ALTER TABLE employes ADD COLUMN IF NOT EXISTS role VARCHAR DEFAULT 'employe'"
        ))
        db.commit()
    except Exception:
        db.rollback()

def ensure_tarifs_columns(db):
    """Migration automatique — toutes les colonnes optionnelles."""
    migrations = [
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarifs_unite TEXT DEFAULT NULL",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarif_poids_kg FLOAT DEFAULT 12.0",
        # ✅ Opérateurs Mobile Money par pays (JSON)
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS operateurs_pays TEXT DEFAULT NULL",
        # ✅ Numéros de paiement par opérateur (JSON)
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS numeros_paiement TEXT DEFAULT NULL",
    ]
    for sql in migrations:
        try:
            db.execute(text(sql))
            db.commit()
        except Exception:
            db.rollback()

    # Migration table commandes
    commandes_migrations = [
        "ALTER TABLE commandes ADD COLUMN IF NOT EXISTS suivi_num VARCHAR DEFAULT NULL",
        "ALTER TABLE commandes ADD COLUMN IF NOT EXISTS motif_refus TEXT DEFAULT NULL",
    ]
    for sql in commandes_migrations:
        try:
            db.execute(text(sql))
            db.commit()
        except Exception:
            db.rollback()

# ── Config publique (pas d'auth — lecture seule) ──────────────
@router.get("/public")
def config_public(db: Session = Depends(get_db)):
    ensure_tarifs_columns(db)
    cfg = get_config(db)
    ports = {
        p.pays: {
            "prix":  p.prix,
            "delai": p.delai,
            "actif": p.actif if p.actif is not None else True
        }
        for p in db.query(PortKg).all()
    }

    def parse_json_col(col_name):
        raw = getattr(cfg, col_name, None)
        if raw:
            try: return json.loads(raw)
            except Exception: pass
        return None

    tarifs_unite    = parse_json_col("tarifs_unite")
    tarif_poids_kg  = getattr(cfg, "tarif_poids_kg", None) or 12.0
    # ✅ Opérateurs par pays — ex: {"Bénin": ["MTN MoMo", "Moov Money"]}
    operateurs_pays = parse_json_col("operateurs_pays") or {}
    # ✅ Numéros de paiement — ex: {"Orange Money": "+224 620 762 815"}
    numeros_paiement = parse_json_col("numeros_paiement") or {}

    return {
        "taux_change":     cfg.taux_change,
        "commission":      cfg.commission,
        "taux_gnf":        cfg.taux_gnf,
        "wa_number":       cfg.wa_number,
        "port_kg":         ports,
        "tarifs_unite":    tarifs_unite,
        "tarif_poids_kg":  tarif_poids_kg,
        "operateurs_pays": operateurs_pays,    # ✅ nouveau
        "numeros_paiement": numeros_paiement,  # ✅ nouveau
    }

# ── Mise à jour config globale ✅ PROTÉGÉ ─────────────────────
@router.put("/")
def update_config(body: Dict[str, Any], request: Request,
                  db: Session = Depends(get_db),
                  role: str = Depends(require_patron)):
    ensure_tarifs_columns(db)
    cfg = get_config(db)

    # Champs simples
    if "taux_change"  in body: cfg.taux_change = float(body["taux_change"])
    if "commission"   in body: cfg.commission  = float(body["commission"])
    if "taux_gnf"     in body: cfg.taux_gnf    = float(body["taux_gnf"])
    if "wa_number"    in body: cfg.wa_number   = str(body["wa_number"])
    if "admin_pwd"    in body: cfg.admin_pwd   = str(body["admin_pwd"])

    # Tarifs à l'unité
    if "tarifs_unite" in body:
        tu = body["tarifs_unite"]
        if isinstance(tu, list):
            try:
                db.execute(
                    text("UPDATE configs SET tarifs_unite = :v WHERE id = :id"),
                    {"v": json.dumps(tu, ensure_ascii=False), "id": cfg.id}
                )
            except Exception:
                pass

    if "tarif_poids_kg" in body:
        try:
            db.execute(
                text("UPDATE configs SET tarif_poids_kg = :v WHERE id = :id"),
                {"v": float(body["tarif_poids_kg"]), "id": cfg.id}
            )
        except Exception:
            pass

    # ✅ Opérateurs Mobile Money par pays
    # Le frontend envoie des clés de la forme "ops_Bénin", "ops_Sénégal", etc.
    ops_updates = {k[4:]: v for k, v in body.items() if k.startswith("ops_")}
    if ops_updates:
        # Fusionner avec les données existantes
        existing_raw = getattr(cfg, "operateurs_pays", None)
        existing = {}
        if existing_raw:
            try: existing = json.loads(existing_raw)
            except Exception: pass
        existing.update(ops_updates)
        try:
            db.execute(
                text("UPDATE configs SET operateurs_pays = :v WHERE id = :id"),
                {"v": json.dumps(existing, ensure_ascii=False), "id": cfg.id}
            )
        except Exception:
            pass

    # ✅ Numéros de paiement par opérateur
    # Le frontend envoie des clés de la forme "num_Orange-Money", "num_MTN-MoMo", etc.
    num_updates = {}
    for k, v in body.items():
        if k.startswith("num_") and v:
            # Reconvertir "num_Orange-Money" → "Orange Money"
            op_key = k[4:].replace("-", " ")
            num_updates[op_key] = str(v).strip()

    if num_updates:
        existing_raw = getattr(cfg, "numeros_paiement", None)
        existing = {}
        if existing_raw:
            try: existing = json.loads(existing_raw)
            except Exception: pass
        existing.update(num_updates)
        try:
            db.execute(
                text("UPDATE configs SET numeros_paiement = :v WHERE id = :id"),
                {"v": json.dumps(existing, ensure_ascii=False), "id": cfg.id}
            )
        except Exception:
            pass

    db.commit()
    return {"ok": True}

# ── Mise à jour frais de port ✅ PROTÉGÉ ──────────────────────
@router.put("/port")
def update_port(body: Dict[str, Any], request: Request,
                db: Session = Depends(get_db),
                role: str = Depends(require_patron)):
    pays = str(body.get("pays", ""))
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    if not p:
        p = PortKg(pays=pays); db.add(p)
    p.prix  = float(body.get("prix", 7000))
    p.delai = str(body.get("delai", "—"))
    db.commit()
    return {"ok": True}

# ── Toggle actif/inactif d'un pays ────────────────────────────
@router.patch("/pays/{pays}/toggle")
def toggle_pays(pays: str, request: Request, db: Session = Depends(get_db),
                role: str = Depends(require_patron)):
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    if not p:
        raise HTTPException(404, "Pays introuvable")
    p.actif = not (p.actif if p.actif is not None else True)
    db.commit()
    return {"ok": True, "pays": pays, "actif": p.actif}

# ── Liste pays avec statut ────────────────────────────────────
@router.get("/pays")
def list_pays(request: Request, db: Session = Depends(get_db),
              role: str = Depends(require_patron)):
    return [
        {
            "pays":  p.pays,
            "prix":  p.prix,
            "delai": p.delai,
            "actif": p.actif if p.actif is not None else True
        }
        for p in db.query(PortKg).order_by(PortKg.pays).all()
    ]

# ── Employés ──────────────────────────────────────────────────
@router.get("/employes")
def list_employes(db: Session = Depends(get_db)):
    ensure_role_column(db)
    employes = db.query(Employe).filter(Employe.actif == True).all()
    return [
        {
            "id":    e.id,
            "nom":   e.nom,
            "actif": e.actif,
            "role":  getattr(e, "role", None) or "employe"
        }
        for e in employes
    ]

@router.post("/employes", status_code=201)
def create_employe(body: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_role_column(db)

    nom  = str(body.get("nom", "")).strip()
    pwd  = str(body.get("pwd", ""))
    role = str(body.get("role", "employe"))

    if not nom or not pwd:
        raise HTTPException(400, "Nom et mot de passe requis")
    if len(pwd) < 4:
        raise HTTPException(400, "Mot de passe trop court (min 4 caractères)")
    if role not in ROLES_AUTORISES:
        role = "employe"

    e = Employe(nom=nom, pwd=pwd)
    db.add(e)
    db.commit()
    db.refresh(e)

    try:
        db.execute(
            text("UPDATE employes SET role = :role WHERE id = :id"),
            {"role": role, "id": e.id}
        )
        db.commit()
    except Exception:
        db.rollback()

    return {"id": e.id, "nom": e.nom, "role": role}

# ── Suppression employé ✅ PROTÉGÉ ────────────────────────────
@router.delete("/employes/{emp_id}")
def delete_employe(emp_id: int, request: Request,
                   db: Session = Depends(get_db),
                   role: str = Depends(require_patron)):
    e = db.query(Employe).filter(Employe.id == emp_id).first()
    if e:
        e.actif = False
        db.commit()
    return {"ok": True}
