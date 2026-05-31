from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse          # ✅ ajout
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, Any
import json
import httpx
import asyncio
from datetime import datetime
from database import get_db, SessionLocal
from models import Config, PortKg, Employe
from routes.auth import require_patron, PWD_MIN_LENGTH, hash_password, verify_password

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

STAT_FIELDS_MAP = {
    "stat_delai":  "stat_delai",
    "stat_badge1": "stat_badge1",
    "stat_badge2": "stat_badge2",
    "stat_label1": "stat_label1",
    "stat_label2": "stat_label2",
    "stat_label3": "stat_label3",
}

_taux_gnf_last_update: datetime = datetime.min


def get_config(db):
    cfg = db.query(Config).first()
    if not cfg:
        cfg = Config()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def init_port(db):
    for pays, info in DEFAULT_PORT.items():
        existing = db.query(PortKg).filter(PortKg.pays == pays).first()
        if not existing:
            db.add(PortKg(
                pays=pays, prix=info["prix"],
                delai=info["delai"], actif=info["actif"]
            ))
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
    migrations = [
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarifs_unite TEXT DEFAULT NULL",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS tarif_poids_kg FLOAT DEFAULT 12.0",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS operateurs_pays TEXT DEFAULT NULL",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS numeros_paiement TEXT DEFAULT NULL",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_delai TEXT DEFAULT '15-25j'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_badge1 TEXT DEFAULT '100%'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_badge2 TEXT DEFAULT '0€'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_label1 TEXT DEFAULT 'Authentique'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_label2 TEXT DEFAULT 'Livraison'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS stat_label3 TEXT DEFAULT 'Frais cachés'",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS taux_gnf_updated_at TIMESTAMP DEFAULT NULL",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS reduction_parrainage FLOAT DEFAULT 1000.0",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS gain_parrain FLOAT DEFAULT 500.0",
        "ALTER TABLE configs ADD COLUMN IF NOT EXISTS livraison_domicile TEXT DEFAULT NULL",
    ]
    for sql in migrations:
        try:
            db.execute(text(sql))
            db.commit()
        except Exception:
            db.rollback()

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


# ══════════════════════════════════════════════════════════════
# TAUX GNF
# ══════════════════════════════════════════════════════════════

async def fetch_taux_gnf_from_api() -> float | None:
    urls = [
        "https://open.er-api.com/v6/latest/EUR",
        "https://api.exchangerate-api.com/v4/latest/EUR",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                data  = resp.json()
                rates = data.get("rates") or data.get("conversion_rates") or {}
                gnf   = rates.get("GNF")
                if gnf and float(gnf) >= 1000:
                    print(f"[taux] EUR/GNF={gnf} depuis {url}")
                    return float(gnf)
        except Exception as e:
            print(f"[taux] Erreur {url}: {e}")
    return None


async def refresh_taux_gnf_en_base(db: Session) -> float | None:
    global _taux_gnf_last_update
    gnf = await fetch_taux_gnf_from_api()
    if gnf:
        cfg = get_config(db)
        cfg.taux_gnf = gnf
        try:
            db.execute(
                text("UPDATE configs SET taux_gnf_updated_at = NOW() WHERE id = :id"),
                {"id": cfg.id}
            )
        except Exception:
            pass
        db.commit()
        _taux_gnf_last_update = datetime.utcnow()
        print(f"[taux] taux_gnf mis à jour en base : {gnf} GNF/€")
        return gnf
    return None


async def auto_refresh_taux_gnf():
    """Tâche de fond — met à jour le taux GNF toutes les heures."""
    while True:
        try:
            db = SessionLocal()
            await refresh_taux_gnf_en_base(db)
        except Exception as e:
            print(f"[taux] Erreur refresh auto: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass
        await asyncio.sleep(3600)


# ══════════════════════════════════════════════════════════════
# CONFIG PUBLIQUE
# ══════════════════════════════════════════════════════════════

@router.get("/public")
def config_public(db: Session = Depends(get_db)):
    cfg = get_config(db)
    ports = {
        p.pays: {
            "prix":  p.prix,
            "delai": p.delai,
            "actif": p.actif if p.actif is not None else True
        }
        for p in db.query(PortKg).all()
    }
    try:
        row = db.execute(text(
            "SELECT tarifs_unite, tarif_poids_kg, operateurs_pays, numeros_paiement,"
            " stat_delai, stat_badge1, stat_badge2, stat_label1, stat_label2, stat_label3,"
            " taux_gnf_updated_at, reduction_parrainage, gain_parrain, livraison_domicile"
            " FROM configs WHERE id = :id"
        ), {"id": cfg.id}).mappings().first() or {}
    except Exception:
        row = {}

    def parse_json(val):
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return None

    tarifs_unite     = parse_json(row.get("tarifs_unite"))
    tarif_poids_kg   = row.get("tarif_poids_kg") or 12.0
    operateurs_pays  = parse_json(row.get("operateurs_pays")) or {}
    numeros_paiement = parse_json(row.get("numeros_paiement")) or {}
    stats_landing    = {
        "delai":  row.get("stat_delai")  or "15-25j",
        "badge1": row.get("stat_badge1") or "100%",
        "badge2": row.get("stat_badge2") or "0€",
        "label1": row.get("stat_label1") or "Authentique",
        "label2": row.get("stat_label2") or "Livraison",
        "label3": row.get("stat_label3") or "Frais cachés",
    }
    taux_gnf_updated_at = row.get("taux_gnf_updated_at")

    data = {
        "taux_change":          cfg.taux_change,
        "commission":           cfg.commission,
        "taux_gnf":             cfg.taux_gnf,
        "taux_gnf_updated_at":  str(taux_gnf_updated_at) if taux_gnf_updated_at else None,
        "wa_number":            cfg.wa_number,
        "port_kg":              ports,
        "tarifs_unite":         tarifs_unite,
        "tarif_poids_kg":       tarif_poids_kg,
        "operateurs_pays":      operateurs_pays,
        "numeros_paiement":     numeros_paiement,
        "stats_landing":        stats_landing,
        "reduction_parrainage": float(row.get("reduction_parrainage") or 1000),
        "gain_parrain":         float(row.get("gain_parrain") or 500),
        "livraison_domicile":   parse_json(row.get("livraison_domicile")) or {},
    }

    # ✅ JSONResponse avec ensure_ascii=False pour corriger l'encodage UTF-8
    # (évite SÃ©nÃ©gal au lieu de Sénégal, etc.)
    return JSONResponse(
        content=data,
        media_type="application/json; charset=utf-8"
    )


@router.post("/livraison-domicile")
def save_livraison_domicile(
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    try:
        data = json.dumps(body, ensure_ascii=False)
        db.execute(text("UPDATE configs SET livraison_domicile=:d WHERE id=1"), {"d": data})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    return {"ok": True}


@router.post("/parrainage")
def save_parrainage_config(
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    red  = float(body.get("reduction_parrainage", 1000))
    gain = float(body.get("gain_parrain", 500))
    try:
        db.execute(text(
            "UPDATE configs SET reduction_parrainage=:r, gain_parrain=:g WHERE id=1"
        ), {"r": red, "g": gain})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    return {"ok": True, "reduction_parrainage": red, "gain_parrain": gain}


@router.post("/refresh-taux-gnf")
async def refresh_taux_gnf(
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    gnf = await refresh_taux_gnf_en_base(db)
    if gnf:
        return {"ok": True, "taux_gnf": gnf, "message": f"Taux mis à jour : {gnf:.0f} GNF/€"}
    raise HTTPException(503, "API de taux indisponible — réessayez dans quelques secondes")


@router.put("/")
def update_config(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    cfg = get_config(db)

    if "taux_change" in body: cfg.taux_change = float(body["taux_change"])
    if "commission"  in body: cfg.commission  = float(body["commission"])
    if "taux_gnf"    in body: cfg.taux_gnf    = float(body["taux_gnf"])
    if "wa_number"   in body: cfg.wa_number   = str(body["wa_number"])

    if "admin_pwd" in body:
        new_pwd = str(body["admin_pwd"]).strip()
        if len(new_pwd) < PWD_MIN_LENGTH:
            raise HTTPException(
                400,
                f"Mot de passe trop court (minimum {PWD_MIN_LENGTH} caractères)"
            )
        cfg.admin_pwd = hash_password(new_pwd)

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

    ops_updates = {k[4:]: v for k, v in body.items() if k.startswith("ops_")}
    if ops_updates:
        try:
            row = db.execute(
                text("SELECT operateurs_pays FROM configs WHERE id=:id"), {"id": cfg.id}
            ).mappings().first()
            existing = {}
            if row and row.get("operateurs_pays"):
                try: existing = json.loads(row["operateurs_pays"])
                except Exception: pass
            existing.update(ops_updates)
            db.execute(
                text("UPDATE configs SET operateurs_pays=:v WHERE id=:id"),
                {"v": json.dumps(existing, ensure_ascii=False), "id": cfg.id}
            )
        except Exception:
            pass

    num_updates = {}
    for k, v in body.items():
        if k.startswith("num_") and v:
            num_updates[k[4:].replace("-", " ")] = str(v).strip()
    if num_updates:
        try:
            row = db.execute(
                text("SELECT numeros_paiement FROM configs WHERE id=:id"), {"id": cfg.id}
            ).mappings().first()
            existing = {}
            if row and row.get("numeros_paiement"):
                try: existing = json.loads(row["numeros_paiement"])
                except Exception: pass
            existing.update(num_updates)
            db.execute(
                text("UPDATE configs SET numeros_paiement=:v WHERE id=:id"),
                {"v": json.dumps(existing, ensure_ascii=False), "id": cfg.id}
            )
        except Exception:
            pass

    for field, col in STAT_FIELDS_MAP.items():
        if field in body and body[field] is not None:
            try:
                db.execute(
                    text(f"UPDATE configs SET {col} = :v WHERE id = :id"),
                    {"v": str(body[field]).strip(), "id": cfg.id}
                )
            except Exception:
                pass

    db.commit()
    return {"ok": True}


@router.put("/port")
def update_port(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    pays = str(body.get("pays", ""))
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    if not p:
        p = PortKg(pays=pays)
        db.add(p)
    p.prix  = float(body.get("prix", 7000))
    p.delai = str(body.get("delai", "—"))
    db.commit()
    return {"ok": True}


@router.patch("/pays/{pays}/toggle")
def toggle_pays(
    pays: str,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    p = db.query(PortKg).filter(PortKg.pays == pays).first()
    if not p:
        raise HTTPException(404, "Pays introuvable")
    p.actif = not (p.actif if p.actif is not None else True)
    db.commit()
    return {"ok": True, "pays": pays, "actif": p.actif}


@router.get("/pays")
def list_pays(
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    return [
        {
            "pays":  p.pays,
            "prix":  p.prix,
            "delai": p.delai,
            "actif": p.actif if p.actif is not None else True
        }
        for p in db.query(PortKg).order_by(PortKg.pays).all()
    ]


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
def create_employe(
    body: Dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    ensure_role_column(db)
    nom      = str(body.get("nom", "")).strip()
    pwd      = str(body.get("pwd", "")).strip()
    emp_role = str(body.get("role", "employe"))

    if not nom:
        raise HTTPException(400, "Nom requis")
    if not pwd:
        raise HTTPException(400, "Mot de passe requis")

    if len(pwd) < PWD_MIN_LENGTH:
        raise HTTPException(
            400,
            f"Mot de passe trop court (minimum {PWD_MIN_LENGTH} caractères)"
        )

    if emp_role not in ROLES_AUTORISES:
        emp_role = "employe"

    e = Employe(nom=nom, pwd=hash_password(pwd))
    db.add(e)
    db.commit()
    db.refresh(e)
    try:
        db.execute(
            text("UPDATE employes SET role = :role WHERE id = :id"),
            {"role": emp_role, "id": e.id}
        )
        db.commit()
    except Exception:
        db.rollback()
    return {"id": e.id, "nom": e.nom, "role": emp_role}


@router.delete("/employes/{emp_id}")
def delete_employe(
    emp_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Depends(require_patron)
):
    e = db.query(Employe).filter(Employe.id == emp_id).first()
    if e:
        e.actif = False
        db.commit()
    return {"ok": True}
