from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import Dict, Any
from database import get_db
from models import Config, PortKg, Employe

router = APIRouter(prefix="/api/config", tags=["config"])

PAYS_LIST = ["Burkina Faso","Guinée","Cameroun","Bénin","Togo","Niger","Congo","Gabon"]
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

def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config(); db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg

def init_port(db):
    for pays, info in DEFAULT_PORT.items():
        if not db.query(PortKg).filter(PortKg.pays == pays).first():
            db.add(PortKg(pays=pays, prix=info["prix"], delai=info["delai"]))
    db.commit()

@router.get("/public")
def config_public(db: Session = Depends(get_db)):
    cfg = get_config(db)
    ports = {p.pays: {"prix": p.prix, "delai": p.delai} for p in db.query(PortKg).all()}
    return {
        "taux_change": cfg.taux_change,
        "commission": cfg.commission,
        "taux_gnf": cfg.taux_gnf,
        "wa_number": cfg.wa_number,
        "port_kg": ports,
    }

@router.put("/")
def update_config(body: Dict[str, Any], db: Session = Depends(get_db)):
    cfg = get_config(db)
    if "taux_change" in body: cfg.taux_change = float(body["taux_change"])
    if "commission"  in body: cfg.commission  = float(body["commission"])
    if "taux_gnf"    in body: cfg.taux_gnf    = float(body["taux_gnf"])
    if "wa_number"   in body: cfg.wa_number   = str(body["wa_number"])
    db.commit()
    return {"ok": True}

@router.put("/port")
def update_port(body: Dict[str, Any], db: Session = Depends(get_db)):
    pays = str(body.get("pays", ""))
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    if not p:
        p = PortKg(pays=pays); db.add(p)
    p.prix = float(body.get("prix", 7000))
    p.delai = str(body.get("delai", "—"))
    db.commit()
    return {"ok": True}

@router.get("/employes")
def list_employes(db: Session = Depends(get_db)):
    return [{"id": e.id, "nom": e.nom, "actif": e.actif}
            for e in db.query(Employe).filter(Employe.actif == True).all()]

@router.post("/employes", status_code=201)
def create_employe(body: Dict[str, Any], db: Session = Depends(get_db)):
    e = Employe(nom=str(body.get("nom","")), pwd=str(body.get("pwd","")))
    db.add(e); db.commit(); db.refresh(e)
    return {"id": e.id, "nom": e.nom}

@router.delete("/employes/{emp_id}")
def delete_employe(emp_id: int, db: Session = Depends(get_db)):
    e = db.query(Employe).filter(Employe.id == emp_id).first()
    if e: e.actif = False; db.commit()
    return {"ok": True}
