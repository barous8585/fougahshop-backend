from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
from sqlalchemy.sql import func
from database import Base


class Config(Base):
    __tablename__ = "config"
    id           = Column(Integer, primary_key=True, default=1)
    taux_change  = Column(Float,  default=660.0)
    commission   = Column(Float,  default=3500.0)
    taux_gnf     = Column(Float,  default=9500.0)
    wa_number    = Column(String, default="33651727112")
    admin_pwd    = Column(String, default="admin123")
    secret_reset = Column(String, default="fougah2026")
    # NOTE : tarifs_unite, tarif_poids_kg, operateurs_pays, numeros_paiement,
    # stat_* sont gérées uniquement via migration SQL dans config.py
    # et lues en raw SQL — ne pas les déclarer ici pour éviter le crash
    # "column does not exist" avant la première migration.


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
    # NOTE : suivi_num et motif_refus ajoutés via migration dans config.py
    created_at          = Column(DateTime, server_default=func.now())
    updated_at          = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    code             = Column(String,  unique=True, index=True)
    influenceur      = Column(String)
    reduction_fcfa   = Column(Float,   default=500.0)
    gain_influenceur = Column(Float,   default=1000.0)
    quota            = Column(Integer, default=50)
    utilisations     = Column(Integer, default=0)
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
