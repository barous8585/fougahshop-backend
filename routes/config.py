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
    try:
        db.execute(text(
            "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarifs_unite TEXT DEFAULT NULL"
        ))
        db.commit()
    except Exception:
        db.rollback()
    try:
        db.execute(text(
            "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarif_poids_kg FLOAT DEFAULT 12.0"
        ))
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

    tarifs_unite = None
    raw = getattr(cfg, "tarifs_unite", None)
    if raw:
        try:
            tarifs_unite = json.loads(raw)
        except Exception:
            tarifs_unite = None

    tarif_poids_kg = getattr(cfg, "tarif_poids_kg", None) or 12.0

    return {
        "taux_change":    cfg.taux_change,
        "commission":     cfg.commission,
        "taux_gnf":       cfg.taux_gnf,
        "wa_number":      cfg.wa_number,
        "port_kg":        ports,
        "tarifs_unite":   tarifs_unite,
        "tarif_poids_kg": tarif_poids_kg,
    }

# ── Mise à jour config globale ✅ PROTÉGÉ ─────────────────────
@router.put("/")
def update_config(body: Dict[str, Any], request: Request,
                  db: Session = Depends(get_db),
                  role: str = Depends(require_patron)):
    ensure_tarifs_columns(db)
    cfg = get_config(db)

    if "taux_change"  in body: cfg.taux_change = float(body["taux_change"])
    if "commission"   in body: cfg.commission  = float(body["commission"])
    if "taux_gnf"     in body: cfg.taux_gnf    = float(body["taux_gnf"])
    if "wa_number"    in body: cfg.wa_number   = str(body["wa_number"])
    if "admin_pwd"    in body: cfg.admin_pwd   = str(body["admin_pwd"])

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
