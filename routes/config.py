from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Config, PortKg, Employe

router = APIRouter(prefix="/api/config", tags=["config"])

PAYS_LIST = [
    "Burkina Faso","Guinée","Cameroun","Bénin",
    "Togo","Niger","Congo","Gabon"
]

DEFAULT_PORT = {
    "Burkina Faso": {"prix": 7000, "delai": "10-14 jours"},
    "Guinée":       {"prix": 7000, "delai": "10-15 jours"},
    "Cameroun":     {"prix": 7000, "delai": "10-15 jours"},
    "Bénin":        {"prix": 7000, "delai": "8-12 jours"},
    "Togo":         {"prix": 7000, "delai": "8-12 jours"},
    "Niger":        {"prix": 7000, "delai": "12-18 jours"},
    "Congo":        {"prix": 8000, "delai": "14-21 jours"},
    "Gabon":        {"prix": 8000, "delai": "14-21 jours"},
}

def get_config(db: Session) -> Config:
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config()
        db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

def init_port(db: Session):
    for pays, info in DEFAULT_PORT.items():
        if not db.query(PortKg).filter(PortKg.pays == pays).first():
            db.add(PortKg(pays=pays, prix=info["prix"], delai=info["delai"]))
    db.commit()

@router.get("/public")
def config_public(db: Session = Depends(get_db)):
    """Config publique accessible sans auth (taux, port, WA)"""
    cfg = get_config(db)
    ports = {p.pays: {"prix": p.prix, "delai": p.delai}
             for p in db.query(PortKg).all()}
    return {
        "taux_change": cfg.taux_change,
        "commission": cfg.commission,
        "taux_gnf": cfg.taux_gnf,
        "wa_number": cfg.wa_number,
        "port_kg": ports,
    }

class ConfigUpdate(BaseModel):
    taux_change: Optional[float] = None
    commission:  Optional[float] = None
    taux_gnf:    Optional[float] = None
    wa_number:   Optional[str]   = None

@router.put("/")
def update_config(body: ConfigUpdate, db: Session = Depends(get_db)):
    cfg = get_config(db)
    if body.taux_change is not None: cfg.taux_change = body.taux_change
    if body.commission  is not None: cfg.commission  = body.commission
    if body.taux_gnf    is not None: cfg.taux_gnf    = body.taux_gnf
    if body.wa_number   is not None: cfg.wa_number   = body.wa_number
    db.commit()
    return {"ok": True}

class PortUpdate(BaseModel):
    pays:  str
    prix:  float
    delai: str

@router.put("/port")
def update_port(body: PortUpdate, db: Session = Depends(get_db)):
    p = db.query(PortKg).filter(PortKg.pays == body.pays).first()
    if not p:
        p = PortKg(pays=body.pays)
        db.add(p)
    p.prix = body.prix
    p.delai = body.delai
    db.commit()
    return {"ok": True}

# ── Employés ──────────────────────────────────────────────────
@router.get("/employes")
def list_employes(db: Session = Depends(get_db)):
    return [{"id": e.id, "nom": e.nom, "actif": e.actif}
            for e in db.query(Employe).filter(Employe.actif == True).all()]

class EmployeCreate(BaseModel):
    nom: str
    pwd: str

@router.post("/employes", status_code=201)
def create_employe(body: EmployeCreate, db: Session = Depends(get_db)):
    e = Employe(nom=body.nom, pwd=body.pwd)
    db.add(e); db.commit(); db.refresh(e)
    return {"id": e.id, "nom": e.nom}

@router.delete("/employes/{emp_id}")
def delete_employe(emp_id: int, db: Session = Depends(get_db)):
    e = db.query(Employe).filter(Employe.id == emp_id).first()
    if e: e.actif = False; db.commit()
    return {"ok": True}
