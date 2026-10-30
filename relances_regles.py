"""Règles communes à l'application et au récapitulatif hebdomadaire."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re
import unicodedata

# Constrelec dispose de quatre champs de relance dans ClickUp.
MAX_RELANCES = 4
LIBELLES_RELANCES = ('Première relance', 'Deuxième relance', 'Troisième relance', 'Quatrième relance')
COLONNES_RELANCES = tuple(('1ère' if n == 1 else f'{n}ème') + ' relance' for n in range(1, MAX_RELANCES + 1))
# Les membres de l’équipe sont identifiés depuis la liste ClickUp Constrelec.
# Aucun destinataire de courriel n’est codé dans cette version.

def normaliser(texte):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(texte).lower().replace('–', '-').replace('—', '-')) if not unicodedata.combining(c)).split())

def aliases_relances():
    return {libelle: [f'A - {colonne}', f'A - {normaliser(colonne)}', colonne, normaliser(colonne)]
            for libelle, colonne in zip(LIBELLES_RELANCES, COLONNES_RELANCES)}

def prochaine_relance(dates):
    # Les anciens dossiers peuvent avoir des dates intermédiaires manquantes :
    # continuer après le niveau le plus élevé renseigné, sans réécrire l'historique.
    indices = [i for i, d in enumerate(dates) if isinstance(d, date)]
    numero = max(indices, default=-1) + 2
    return numero if numero <= MAX_RELANCES else None

NOMS_TYPE_PROJET = ('Type de projet', 'Type projet', 'A - Type de projet', 'Type de projets')

def est_admin_ao(tache):
    """Admin = tâche non assignée ; AO est un choix exact du type de projet."""
    if tache.get('assignees'):
        return False
    for nom in NOMS_TYPE_PROJET:
        champs = [c for c in tache.get('custom_fields', [])
                  if normaliser(c.get('name', '')) == normaliser(nom)]
        if len(champs) > 1:
            return False
        if not champs or champs[0].get('value') in (None, '', []):
            continue
        c = champs[0]
        valeur = c['value']
        valeurs = valeur if isinstance(valeur, list) else [valeur]
        options = (c.get('type_config') or {}).get('options', [])
        noms = []
        for v in valeurs:
            trouves = [o for o in options if o.get('id') is not None and str(o['id']) == str(v)]
            if not trouves:
                trouves = [o for o in options if o.get('orderindex') is not None and str(o['orderindex']) == str(v)]
            if len(trouves) == 1:
                noms.append(trouves[0].get('name') or trouves[0].get('label') or '')
            elif not options:
                noms.append(v)
        return any(normaliser(n) == 'ao' for n in noms)
    return False

def echeance_relance(offre, dates, admin_ao=False):
    numero = prochaine_relance(dates)
    if numero is None or not isinstance(offre, date):
        return numero, None
    if numero == 1:
        return numero, offre + timedelta(days=60 if admin_ao else 15)
    base = dates[numero - 2]
    return numero, base + timedelta(days=60 if admin_ao else 30)

def date_clickup(valeur):
    if valeur in (None, ''):
        return None
    return datetime.fromtimestamp(float(valeur) / 1000, ZoneInfo('Europe/Zurich')).date()

def semaine_suivante(jour):
    lundi = jour + timedelta(days=7 - jour.weekday())
    return lundi, lundi + timedelta(days=6)


def echeance_planifiee(offre, dates, planifiee=None, admin_ao=False):
    numero, automatique = echeance_relance(offre, dates, admin_ao)
    derniere = max((d for d in dates if isinstance(d, date)), default=None)
    # Une ancienne date ne doit pas réactiver une étape déjà effectuée.
    if numero is not None and isinstance(planifiee, date) and (derniere is None or planifiee >= derniere):
        # Les anciennes dates J+30 ne doivent pas relancer un AO Admin avant J+60.
        # Une date manuelle plus tardive reste prioritaire.
        if admin_ao and automatique is not None:
            return numero, max(automatique, planifiee)
        return numero, planifiee
    return numero, automatique
