from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
from sqlalchemy.sql import func
from database import Base


class Config(Base):
    __tablename__ = "config"
    id           = Column(Integer, primary_key=True, default=1)
    taux_change  = Column(Float, default=660.0)      # € → FCFA
    commission   = Column(Float, default=3500.0)     # FCFA fixe par article
    taux_gnf     = Column(Float, default=9500.0)     # € → GNF (Guinée)
    wa_number    = Column(String, default="33651727112")
    admin_pwd    = Column(String, default="admin123")


class PortKg(Base):
    __tablename__ = "port_kg"
    id    = Column(Integer, primary_key=True, autoincrement=True)
    pays  = Column(String, unique=True)
    prix  = Column(Float, default=7000.0)   # FCFA par kg
    delai = Column(String, default="7-10 jours")


class Employe(Base):
    __tablename__ = "employes"
    id   = Column(Integer, primary_key=True, autoincrement=True)
    nom  = Column(String)
    pwd  = Column(String)
    actif = Column(Boolean, default=True)


class Commande(Base):
    __tablename__ = "commandes"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    ref           = Column(String, unique=True, index=True)
    # Client
    client_nom    = Column(String)
    client_tel    = Column(String, index=True)
    client_pays   = Column(String)
    client_adresse= Column(Text, nullable=True)
    client_instructions = Column(Text, nullable=True)
    # Paiement
    operateur     = Column(String)
    monnaie       = Column(String, default="FCFA")
    total_euro    = Column(Float)
    total_local   = Column(Float)   # dans la monnaie du pays
    poids_estime  = Column(Float, nullable=True)
    poids_reel    = Column(Float, nullable=True)
    # Articles (JSON)
    articles      = Column(Text)    # JSON string
    nb_articles   = Column(Integer, default=1)
    # Statut
    statut        = Column(String, default="en_attente_paiement")
    # en_attente_paiement → paye → achete → expedie → arrive
    paiement_ref  = Column(String, nullable=True)   # ref CinetPay
    paiement_statut = Column(String, nullable=True) # ACCEPTED / REFUSED / etc.
    note_admin    = Column(Text, nullable=True)
    delai_livraison = Column(String, nullable=True)
    # Dates
    created_at    = Column(DateTime, server_default=func.now())
    updated_at    = Column(DateTime, server_default=func.now(), onupdate=func.now())
