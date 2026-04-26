from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, Date
from sqlalchemy.sql import func
from database import Base


class Config(Base):
    # ✅ CORRIGÉ — 'configs' (pluriel) aligné avec les requêtes SQL dans config.py
    __tablename__ = "configs"

    id           = Column(Integer, primary_key=True, default=1)
    taux_change  = Column(Float,   default=660.0)
    commission   = Column(Float,   default=3500.0)
    taux_gnf     = Column(Float,   default=9500.0)
    wa_number    = Column(String,  default="33651727112")
    admin_pwd    = Column(String,  default="admin123")
    secret_reset = Column(String,  default="fougah2026")
    # Colonnes étendues — ajoutées via migration SQL dans config.py au startup
    # Déclarées ici pour que SQLAlchemy les connaisse après la première migration
    # nullable=True + server_default pour éviter le crash si la colonne n'existe pas encore


class PortKg(Base):
    __tablename__ = "port_kg"

    id    = Column(Integer, primary_key=True, autoincrement=True)
    pays  = Column(String,  unique=True)
    prix  = Column(Float,   default=7000.0)
    delai = Column(String,  default="7-10 jours")
    actif = Column(Boolean, default=True)


class Employe(Base):
    __tablename__ = "employes"

    id    = Column(Integer, primary_key=True, autoincrement=True)
    nom   = Column(String)
    pwd   = Column(String)
    actif = Column(Boolean, default=True)
    role  = Column(String,  default="employe")


class Commande(Base):
    __tablename__ = "commandes"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    ref                 = Column(String,  unique=True, index=True)
    client_nom          = Column(String)
    client_tel          = Column(String,  index=True)
    client_pays         = Column(String)
    client_adresse      = Column(Text,    nullable=True)
    client_instructions = Column(Text,    nullable=True)
    operateur           = Column(String)
    monnaie             = Column(String,  default="FCFA")
    total_euro          = Column(Float)
    total_local         = Column(Float)
    poids_estime        = Column(Float,   nullable=True)
    poids_reel          = Column(Float,   nullable=True)
    articles            = Column(Text)
    nb_articles         = Column(Integer, default=1)
    statut              = Column(String,  default="en_attente_paiement")
    paiement_ref        = Column(String,  nullable=True)
    paiement_statut     = Column(String,  nullable=True)
    note_admin          = Column(Text,    nullable=True)
    delai_livraison     = Column(String,  nullable=True)
    promo_code          = Column(String,  nullable=True)
    # ✅ Déclarés ici — plus de getattr() fragile dans admin.py
    suivi_num           = Column(String,  nullable=True)
    motif_refus         = Column(Text,    nullable=True)
    archived            = Column(Boolean, default=False, nullable=True)
    created_at          = Column(DateTime, server_default=func.now())
    updated_at          = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    code             = Column(String,  unique=True, index=True)

    # ✅ CORRIGÉ — structure alignée avec promo.py (raw SQL)
    # Anciens champs conservés pour rétro-compat
    influenceur      = Column(String,  nullable=True)
    gain_influenceur = Column(Float,   default=0.0)   # gain par commande en FCFA

    # Nouveaux champs (utilisés par promo.py)
    type             = Column(String,  default="fixe")  # 'fixe' | 'pct'
    valeur           = Column(Float,   default=0.0)     # montant FCFA ou %
    reduction_fcfa   = Column(Float,   default=0.0)     # alias rétro-compat

    client_tel       = Column(String,  nullable=True)   # NULL = tous les clients
    max_uses         = Column(Integer, default=0)       # 0 = illimité
    uses_count       = Column(Integer, default=0)       # ✅ nouveau compteur

    # Anciens champs — conservés pour ne pas casser les données existantes
    quota            = Column(Integer, default=0)       # alias de max_uses
    utilisations     = Column(Integer, default=0)       # alias de uses_count

    note             = Column(String,  nullable=True)
    expiry           = Column(Date,    nullable=True)
    actif            = Column(Boolean, default=True)
    created_at       = Column(DateTime, server_default=func.now())


class Avis(Base):
    __tablename__ = "avis"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    nom        = Column(String)
    pays       = Column(String,  nullable=True)
    drapeau    = Column(String,  nullable=True)
    note       = Column(Integer, default=5)
    texte      = Column(Text)
    reponse    = Column(Text,    nullable=True)
    visible    = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
