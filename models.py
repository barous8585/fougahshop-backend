from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, Date
from sqlalchemy.sql import func
from database import Base


class Config(Base):
    __tablename__ = "configs"

    id           = Column(Integer, primary_key=True, default=1)
    taux_change  = Column(Float,   default=660.0)
    commission   = Column(Float,   default=3500.0)
    taux_gnf     = Column(Float,   default=9500.0)
    wa_number    = Column(String,  default="33651727112")
    admin_pwd    = Column(String,  default="admin123")
    secret_reset = Column(String,  default="fougah2026")
    totp_secret  = Column(String,  nullable=True)
    totp_enabled = Column(Boolean, default=False)


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
    suivi_num           = Column(String,  nullable=True)
    motif_refus         = Column(Text,    nullable=True)
    archived            = Column(Boolean, default=False, nullable=True)
    created_at          = Column(DateTime, server_default=func.now())
    updated_at          = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    code             = Column(String,  unique=True, index=True)
    influenceur      = Column(String,  nullable=True)
    gain_influenceur = Column(Float,   default=0.0)
    type             = Column(String,  default="fixe")
    valeur           = Column(Float,   default=0.0)
    reduction_fcfa   = Column(Float,   default=0.0)
    client_tel       = Column(String,  nullable=True)
    max_uses         = Column(Integer, default=0)
    uses_count       = Column(Integer, default=0)
    quota            = Column(Integer, default=0)
    utilisations     = Column(Integer, default=0)
    note             = Column(String,  nullable=True)
    expiry           = Column(Date,    nullable=True)
    actif            = Column(Boolean, default=True)
    created_at       = Column(DateTime, server_default=func.now())


class Avis(Base):
    __tablename__ = "avis"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    nom           = Column(String)
    pays          = Column(String,  nullable=True)
    drapeau       = Column(String,  nullable=True)
    note          = Column(Integer, default=5)
    texte         = Column(Text)
    reponse       = Column(Text,    nullable=True)
    visible       = Column(Boolean, default=True)
    created_at    = Column(DateTime, server_default=func.now())
    client_tel    = Column(String,  nullable=True)
    taille_retour = Column(String,  nullable=True)
    photo_url     = Column(String,  nullable=True)
    verifie       = Column(Boolean, default=False)
    utile_count   = Column(Integer, default=0)


# ══════════════════════════════════════════════════════════════════════
# BOUTIQUE — Catalogue produits à la demande
# La table est créée automatiquement par Base.metadata.create_all()
# ══════════════════════════════════════════════════════════════════════
class Produit(Base):
    __tablename__ = "produits"

    id          = Column(Integer,  primary_key=True, autoincrement=True)
    nom         = Column(String(200), nullable=False)
    description = Column(Text,     default="")
    categorie   = Column(String(80), default="")
    image_url   = Column(String(500), default="")
    images      = Column(Text,     default="[]")       # JSON list d'URLs
    prix_eur    = Column(Float,    nullable=False)      # Prix de vente fixé par le patron
    badge       = Column(String(50), default="")       # "Nouveau", "Populaire", "Promo"
    actif       = Column(Boolean,  default=True)
    ordre       = Column(Integer,  default=0)          # Ordre d'affichage dans la boutique
    created_at  = Column(DateTime, server_default=func.now())
