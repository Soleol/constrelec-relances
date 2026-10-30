# Constrelec V6 · photo sur le chargement, logo sombre adapté à la barre latérale
"""Lancer : py -m streamlit run tableau_clickup.py --server.address 127.0.0.1"""
import ssl
import secrets
import threading
import time
from zoneinfo import ZoneInfo
import os
import sys
from PIL import Image
from pathlib import Path
from contextlib import contextmanager
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import pydeck as pdk
from datetime import datetime, date, timedelta
import unicodedata
import re
from functools import lru_cache
from html import escape
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import truststore
import pandas as pd
import streamlit as st
import streamlit.components.v1 as composants
from navigation_relances import installer_defilement, actualiser_sans_deplacer

# Configuration dédiée à Constrelec, sans repli vers les données d’autres sociétés.
def parametre(nom, valeur_par_defaut=""):
    valeur = os.environ.get(nom)
    if valeur is None:
        try:
            valeur = st.secrets.get(nom, valeur_par_defaut)
        except FileNotFoundError:
            valeur = valeur_par_defaut
    return str(valeur).strip()

LISTE_ID = parametre("CONSTRELEC_CLICKUP_LIST_ID")
STATUT_OFFRES = parametre("CONSTRELEC_STATUT_OFFRES", "10_ENVOYéES")
STATUT_PERDU = parametre("CONSTRELEC_STATUT_PERDU", "Perdues")
DEBUT_TEXTE = parametre("CONSTRELEC_DATE_DEBUT", "2026-01-01")
ACTIVER_REGLE_ADMIN_AO = parametre("CONSTRELEC_REGLE_ADMIN_AO", "false").lower() == "true"
try:
    DEBUT_RELANCES = date.fromisoformat(DEBUT_TEXTE)
except ValueError:
    DEBUT_RELANCES = date(2026, 1, 1)
    DATE_DEBUT_INVALIDE = True
else:
    DATE_DEBUT_INVALIDE = False
st.set_page_config(page_title="Constrelec SA · Relances clients", page_icon=Image.open(Path(__file__).parent / "assets" / "favicon_constrelec.png"), layout="wide")
if DATE_DEBUT_INVALIDE:
    st.warning("CONSTRELEC_DATE_DEBUT invalide : format attendu AAAA-MM-JJ. Valeur 2026-01-01 utilisée.")

installer_defilement()


@contextmanager
def chargement_constrelec(texte):
    zone = st.empty()
    zone.markdown('<div class="chargement-constrelec" role="status"><span class="cercle-constrelec" aria-hidden="true"></span><span>' + escape(texte) + '</span></div>', unsafe_allow_html=True)
    try:
        yield
    finally:
        zone.empty()

def libelle_vendeur(nom):
    return nom

# Built from the connected list after loading its tasks.
COULEURS_GRAPHIQUE = {"Admin": "#9299A3"}
PALETTE_EQUIPE = ("#E32323", "#B34754", "#D98274", "#678ECA", "#B48AD5", "#59A49F",
                  "#BEA05E", "#8EAC78", "#9B85A6", "#7C98B5", "#CE718B", "#76AFBB")

def adresse_client(tache):
    champs = [c.get("value") for c in tache.get("custom_fields", [])
              if normaliser(c.get("name", "")).strip() in ("adresse", "a - adresse")
              and c.get("value") not in (None, "")]
    if len(champs) != 1:
        return "", None
    valeur = champs[0]
    if isinstance(valeur, str):
        return " ".join(valeur.split()), None
    if isinstance(valeur, dict):
        texte = valeur.get("formatted_address") or valeur.get("address") or valeur.get("label") or ""
        emplacement = valeur.get("location") or valeur
        if not isinstance(emplacement, dict):
            emplacement = {}
        try:
            lat = float(emplacement.get("lat", emplacement.get("latitude")))
            lon = float(emplacement.get("lng", emplacement.get("longitude")))
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return str(texte), (lon, lat)
        except (TypeError, ValueError):
            pass
        return str(texte), None
    return "", None

@st.cache_data(ttl=604800, max_entries=2000, show_spinner=False)
def geocoder_adresse(adresse):
    # Aucune clé ClickUp et aucun nom de client ne sont transmis au géocodeur.
    parametres = urlencode({"searchText": adresse, "type": "locations", "origins": "address",
                           "sr": "4326", "lang": "fr", "limit": 2})
    req = Request("https://api3.geo.admin.ch/rest/services/ech/SearchServer?" + parametres,
                  headers={"Accept": "application/json", "User-Agent": "Constrelec-Relances/1"})
    with urlopen(req, timeout=10, context=contexte_tls()) as reponse:
        resultats = json.load(reponse).get("results", [])
    if len(resultats) != 1:
        return None  # Une adresse ambiguë n'est pas placée arbitrairement.
    attrs = resultats[0].get("attrs", {})
    if attrs.get("origin") != "address":
        return None
    lon, lat = float(attrs["lon"]), float(attrs["lat"])
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def couleur_repere(ids_assignes, correspondances):
    noms = [nom for nom in COULEURS_GRAPHIQUE
            if len(correspondances.get(nom, [])) == 1
            and str(correspondances[nom][0]) in {str(i) for i in ids_assignes}]
    if not ids_assignes:
        noms = ["Admin"]
    hexadecimal = COULEURS_GRAPHIQUE[noms[0]] if noms else "#9299A3"
    return [int(hexadecimal[i:i+2], 16) for i in (1, 3, 5)] + [255], noms


def filtrer_carte_vendeurs(vue, selection, correspondances):
    ids = {str(i) for nom in selection if nom != 'Admin'
           for i in correspondances.get(nom, []) if len(correspondances.get(nom, [])) == 1}
    masque = vue['_ids'].apply(lambda affectations:
        ('Admin' in selection and not affectations) or bool(ids.intersection(str(i) for i in affectations)))
    return vue.loc[masque.astype(bool)]


def carte_portefeuille(vue, taches, correspondances):
    st.subheader("Localisation des offres")
    st.caption("Offres en cours depuis janvier 2026 · Adresses ClickUp · Géocodage des adresses suisses par geo.admin.ch")
    filtre_carte, _ = st.columns([1, 2])
    with filtre_carte:
        vendeur_carte = st.selectbox("Vendeur à afficher sur la carte", ["Global"] + list(COULEURS_GRAPHIQUE),
                                    format_func=lambda nom: "Global" if nom == "Global" else libelle_vendeur(nom),
                                    key="vendeur_carte_unique")
    selection_carte = list(COULEURS_GRAPHIQUE) if vendeur_carte == "Global" else [vendeur_carte]
    vue = filtrer_carte_vendeurs(vue, selection_carte, correspondances)
    if vue.empty:
        st.info("Aucune offre ne correspond aux vendeurs sélectionnés sur la carte.")
        return
    correspondances_carte = {nom: ids for nom, ids in correspondances.items() if nom in selection_carte}
    par_id = {str(t["id"]): t for t in taches}
    projets = []
    for _, ligne in vue.iterrows():
        adresse, position = adresse_client(par_id.get(ligne["ID"], {}))
        couleur, noms = couleur_repere(ligne["_ids"], correspondances_carte)
        projets.append({"couleur": couleur, "vendeurs_couleur": noms, "Projet": ligne["Projet"], "Référence": ligne["N° offre"],
                        "Adresse": adresse, "Vendeurs": ligne["Responsables"],
                        "Montant": argent(ligne["Prix offre CHF"]) if isinstance(ligne["Prix offre CHF"], Decimal) else "Prix non renseigné",
                        "position": position})
    positions = st.session_state.setdefault("positions_adresses", {})
    non_resolues = sorted({p["Adresse"] for p in projets if p["Adresse"] and p["position"] is None
                          and p["Adresse"] not in positions})
    barre_carte = None
    if st.button("Charger / compléter la carte", key="charger_carte_globale"):
        st.session_state["carte_demandee"] = True
        barre_carte = st.progress(0, text="Préparation de la carte · 0 %")
        if non_resolues:
            erreurs = 0
            with ThreadPoolExecutor(max_workers=3) as pool:
                recherches = {pool.submit(geocoder_adresse, a): a for a in non_resolues}
                for i, futur in enumerate(as_completed(recherches), 1):
                    try:
                        positions[recherches[futur]] = futur.result()
                    except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError, KeyError, TypeError):
                        erreurs += 1
                    pourcentage = round(90*i/len(recherches))
                    barre_carte.progress(pourcentage, text=f"Localisation des adresses · {pourcentage} % · {i}/{len(recherches)} traitées")
            if erreurs:
                st.warning(f"{erreurs} adresse(s) non traitée(s) : service indisponible. Réessayez avec le bouton de la carte.")
    if not st.session_state.get("carte_demandee"):
        return
    if barre_carte is not None:
        barre_carte.progress(95, text="Préparation des repères · 95 %")
    points, absents = [], []
    for projet in projets:
        pos = projet["position"] or positions.get(projet["Adresse"])
        if pos:
            points.append({**projet, "position": list(pos)})
        else:
            absents.append({"Projet": projet["Projet"], "Adresse": projet["Adresse"] or "Non renseignée"})
    if points:
        couche = pdk.Layer("ScatterplotLayer", points, get_position="position", get_fill_color="couleur",
                           get_radius=80, radius_min_pixels=6, radius_max_pixels=16,
                           pickable=True, stroked=True, get_line_color=[255,255,255,200], line_width_min_pixels=1)
        if any(len(p["vendeurs_couleur"]) > 1 for p in points):
            st.caption("Offres partagées : couleur du premier vendeur sélectionné dans l’ordre de la légende ; tous les vendeurs sont indiqués au survol.")
        centre = pdk.data_utils.compute_view([p["position"] for p in points])
        centre.zoom = min(centre.zoom, 13)
        st.pydeck_chart(pdk.Deck(layers=[couche], initial_view_state=centre, map_style="dark",
            tooltip={"text": "{Projet}\n{Référence}\n{Adresse}\n{Vendeurs}\n{Montant}"}), key="carte_offres")
        st.caption(f"{len(points)} offre(s) localisée(s) sur {len(projets)} · Survolez un repère pour voir le client.")
    else:
        st.info("Aucune adresse n’a pu être localisée pour ces offres.")
    if absents:
        with st.expander(f"{len(absents)} offre(s) non localisée(s) — adresses manquantes, ambiguës ou introuvables"):
            st.dataframe(pd.DataFrame(absents), hide_index=True, use_container_width=True)

    if barre_carte is not None:
        barre_carte.progress(100, text=f"Préparation terminée · 100 % · {len(points)}/{len(projets)} offres localisées")


@st.cache_resource(show_spinner=False)
def contexte_tls():
    # Contexte sans identité utilisateur, réutilisable entre requêtes HTTPS.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def aujourd_hui():
    return datetime.now(ZoneInfo('Europe/Zurich')).date()


# Schéma minimal conservé en session : aucun changement envoyé à ClickUp.
from relances_regles import MAX_RELANCES, LIBELLES_RELANCES, COLONNES_RELANCES, aliases_relances, prochaine_relance, echeance_relance

ALIASES_CHAMPS = {
        "Chance to win": ["Chance to win"],
        "Prix offre": ["Prix offre CHF", "Prix de l'offre", "Prix offre", "Montant de l'offre", "Montant offre", "Montant CHF", "Prix total", "Prix", "Montant", "A - Montant offre", "A - Prix offre"],
        "Référence": ["N° Constrelec", "N° offre", "N° projet", "Numéro de projet", "Référence projet", "Référence", "Numéro offre", "N° commande", "N° devis"],
        "SharePoint": ["Lien SP", "A - Lien SP", "SharePoint", "Lien SharePoint", "Dossier SharePoint", "Lien dossier client", "Lien Sharepoint client"],
        "Type projet": ["Type de projet", "Type projet", "A - Type de projet", "Type de projets"],
        "Offre envoyée": ["A - Offres envoyées", "Offres envoyées", "Offre envoyée"],
        "Première relance": ["A - 1ère relance", "A - 1ere relance", "1ère relance", "1ere relance"],
        "Deuxième relance": ["A - 2ème relance", "A - 2eme relance", "2ème relance", "2eme relance"],
        "Troisième relance": ["A - 3ème relance", "A - 3eme relance", "3ème relance", "3eme relance"],
        "Statut relance": ["État relance", "A - État relance", "A - Statut relance", "Statut relance"],
        "E-mail client": ["E-mail client", "A - E-mail client", "E-mail", "Email"],
        "Téléphone": ["N° Tél.", "N° Tel.", "Téléphone", "Telephone", "Téléphone client"],
    }

ALIASES_CHAMPS.update(aliases_relances())
ALIASES_CHAMPS["Prochaine action"] = ["A - Prochaine action"]
from relances_regles import echeance_planifiee, est_admin_ao

def normaliser_nom_champ(nom):
    texte = str(nom).lower().replace('–', '-').replace('—', '-')
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', texte)
                           if not unicodedata.combining(c)).split())

NOMS_CHAMPS_UTILISES = frozenset(normaliser_nom_champ(n)
    for noms in ALIASES_CHAMPS.values() for n in noms) | {'adresse', 'a - adresse'}

NOMS_MOTIFS_PERTE = ("Motif de perte", "Raison de la perte", "Raison perte", "Motif offre perdue",
                     "A - Motif de perte", "Motifs de perte", "Motif de refus")

def est_champ_motif_perte(champ):
    return (champ.get('type') == 'drop_down' and
            normaliser_nom_champ(champ.get('name', '')) in
            {normaliser_nom_champ(nom) for nom in NOMS_MOTIFS_PERTE})

def champ_motif_perte(tache):
    candidats = [c for c in tache.get('custom_fields', []) if est_champ_motif_perte(c)]
    if len(candidats) != 1:
        raise ValueError('Le champ ClickUp « Motif de perte » (liste déroulante) est absent ou ambigu. Vérifiez son nom.')
    return candidats[0]

def compacter_champ(champ):
    # Garder les champs sans nom : leur absence doit toujours être détectable.
    if champ.get('name') and normaliser_nom_champ(champ['name']) not in NOMS_CHAMPS_UTILISES and not est_champ_motif_perte(champ):
        return None
    return {k: champ[k] for k in ('id', 'name', 'type', 'value', 'type_config') if k in champ}

def compacter_tache(tache):
    resultat = {k: tache[k] for k in ('id', 'name', 'url', 'parent') if k in tache}
    resultat['tags'] = [{'name': t.get('name', '')} for t in (tache.get('tags') or []) if isinstance(t, dict)]
    resultat['status'] = {'status': (tache.get('status') or {}).get('status', 'Sans statut')}
    resultat['assignees'] = [{k: p[k] for k in ('id', 'username', 'email') if k in p}
                             for p in tache.get('assignees', [])]
    resultat['custom_fields'] = [c for champ in tache.get('custom_fields', [])
                                if (c := compacter_champ(champ)) is not None]
    return resultat


def enrichir_tache(tache, definitions):
    resultat = dict(tache)
    enrichis = []
    for champ in tache.get('custom_fields', []):
        definition = definitions.get(champ.get('id'), {})
        complet = {**definition, **champ}
        # Ne jamais réintroduire une ancienne valeur effacée dans ClickUp.
        if 'value' not in champ:
            complet.pop('value', None)
        if not (complet.get('type_config') or {}).get('options') and (definition.get('type_config') or {}).get('options'):
            complet['type_config'] = definition['type_config']
        enrichis.append(complet)
    resultat['custom_fields'] = enrichis
    return compacter_tache(resultat)


def lire_tache(task_id, cle):
    return enrichir_tache(lire_api('task/' + task_id, cle), st.session_state.get('_definitions_champs', {}))


def reconcilier_relances(taches):
    # Reprendre une vérification interrompue, sans jamais renvoyer le POST.
    index = {str(t.get('id')): t for t in taches}
    en_cours = st.session_state.setdefault('_relances_a_verifier', {})
    for task_id, operation in list(en_cours.items()):
        tache = index.get(task_id)
        if tache is None:
            continue
        champ = next((c for c in tache.get('custom_fields', []) if c.get('id') == operation['champ']), {})
        confirme = lire_date(champ.get('value')) == operation['jour']
        if confirme and operation['attente']:
            etat = next((c for c in tache.get('custom_fields', []) if c.get('id') == operation['champ_attente']), {})
            if normaliser(decoder(etat)).strip() == 'en attente':
                st.session_state['attente_a_effacer_' + task_id] = True
        st.session_state.pop('ecriture_incertaine_' + task_id, None)
        del en_cours[task_id]


def lire_api(chemin, cle):
    requete = Request("https://api.clickup.com/api/v2/" + chemin,
                      headers={"Authorization": cle, "Accept": "application/json"})
    for tentative in range(2):
        try:
            with urlopen(requete, context=contexte_tls(), timeout=30) as reponse:
                return json.load(reponse)
        except HTTPError as erreur:
            if tentative or erreur.code not in (429, 502, 503, 504):
                raise
            try:
                attente = float(erreur.headers.get('Retry-After', '1'))
            except (ValueError, TypeError, AttributeError):
                attente = 1
            if attente > 2:
                raise
            time.sleep(max(0.25, attente))
        except (URLError, TimeoutError):
            if tentative:
                raise
            time.sleep(0.3)


def ecrire_date_relance(task_id, field_id, cle):
    contexte = contexte_tls()
    # Date locale du PC, à midi pour éviter un décalage de jour à minuit.
    maintenant = datetime.now(ZoneInfo("Europe/Zurich")).replace(hour=12, minute=0, second=0, microsecond=0)
    contenu = json.dumps({"value": int(maintenant.timestamp() * 1000),
                          "value_options": {"time": False}}).encode("utf-8")
    requete = Request(f"https://api.clickup.com/api/v2/task/{task_id}/field/{field_id}",
                      data=contenu, method="POST",
                      headers={"Authorization": cle, "Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(requete, context=contexte, timeout=30) as reponse:
        reponse.read()


def champ_etat_relance(tache):
    # Écriture uniquement dans le champ explicitement demandé, jamais le statut du projet.
    candidats = [c for c in tache.get("custom_fields", [])
                 if normaliser(c.get("name", "")).strip() in ("etat relance", "a - etat relance")]
    if len(candidats) != 1 or candidats[0].get("type") != "drop_down":
        raise ValueError("Le champ unique « État relance » de type liste déroulante est introuvable dans ClickUp.")
    return candidats[0]


CHOIX_CHANCE = ("Inconnue", "Faible", "Moyenne", "Élevée")

def selectionner_chance_locale(task_id, choix):
    if choix in CHOIX_CHANCE:
        st.session_state["choix_chance_local_" + task_id] = choix


def champ_chance(tache):
    candidats = [c for c in tache.get('custom_fields', [])
                 if normaliser_nom_champ(c.get('name', '')) == 'chance to win']
    if len(candidats) != 1 or candidats[0].get('type') != 'drop_down':
        raise ValueError('Le champ unique « Chance to win » de type liste déroulante est introuvable. Actualisez les données et vérifiez le champ dans ClickUp.')
    return candidats[0]

def valeur_chance(tache):
    try:
        texte = normaliser_nom_champ(decoder(champ_chance(tache)))
    except ValueError:
        return ''
    return next((v for v in CHOIX_CHANCE if normaliser_nom_champ(v) == texte), '')

def modifier_chance_clickup(task_id, cle, choix):
    if choix not in CHOIX_CHANCE:
        raise ValueError('Choisissez Inconnue, Faible, Moyenne ou Élevée.')
    frais = lire_tache(task_id, cle)
    champ = champ_chance(frais)
    if normaliser_nom_champ((frais.get('status') or {}).get('status', '')) != normaliser_nom_champ(STATUT_OFFRES):
        raise ValueError('Le statut de l’offre a changé. Actualisez les données.')
    options = [o for o in champ.get('type_config', {}).get('options', [])
               if normaliser_nom_champ(o.get('name', '')) == normaliser_nom_champ(choix)]
    if len(options) != 1 or not options[0].get('id'):
        raise ValueError('Le choix « ' + choix + ' » est introuvable ou ambigu dans ClickUp.')
    if valeur_chance(frais) == choix:
        return frais
    requete = Request(f"https://api.clickup.com/api/v2/task/{task_id}/field/{champ['id']}",
                      data=json.dumps({'value': options[0]['id']}).encode('utf-8'), method='POST',
                      headers={'Authorization': cle, 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urlopen(requete, context=contexte_tls(), timeout=30) as reponse:
            reponse.read()
    except (URLError, TimeoutError, ssl.SSLError):
        # Une réponse perdue n’autorise jamais un renvoi automatique de l’écriture.
        verification = lire_tache(task_id, cle)
        if valeur_chance(verification) != choix:
            raise ValueError('Choix non confirmé. Actualisez les données avant de réessayer.')
        return verification
    verification = lire_tache(task_id, cle)
    if valeur_chance(verification) != choix:
        raise ValueError('Le choix relu dans ClickUp diffère. Actualisez les données avant de continuer.')
    return verification


def modifier_attente_clickup(task_id, cle, retirer=False):
    frais = lire_tache(task_id, cle)
    champ = champ_etat_relance(frais)
    if retirer and normaliser(decoder(champ)).strip() != "en attente":
        return frais  # Ne pas effacer un autre choix posé par un collègue.
    if not retirer and normaliser(decoder(champ)).strip() == "en attente":
        return frais
    if not retirer:
        options = [o for o in champ.get("type_config", {}).get("options", [])
                   if normaliser(o.get("name", "")).strip() == "en attente"]
        if len(options) != 1 or not options[0].get("id"):
            raise ValueError("Le choix unique « En attente » est introuvable dans « État relance ».")
        if normaliser((frais.get("status") or {}).get("status", "")) != normaliser_nom_champ(STATUT_OFFRES):
            raise ValueError("Le statut de l’offre a changé. Actualisez les données.")
    requete = Request(
        f"https://api.clickup.com/api/v2/task/{task_id}/field/{champ['id']}",
        data=None if retirer else json.dumps({"value": options[0]["id"]}).encode("utf-8"),
        method="DELETE" if retirer else "POST",
        headers={"Authorization": cle, "Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(requete, context=contexte_tls(), timeout=30) as reponse:
        reponse.read()
    verification = lire_tache(task_id, cle)
    retour = champ_etat_relance(verification)
    if (retirer and retour.get("value") not in (None, "")) or (not retirer and normaliser(decoder(retour)).strip() != "en attente"):
        raise ValueError("Modification de l’attente non confirmée. Actualisez les données avant de réessayer.")
    return verification


def actualiser_tache_session(tache):
    nom, taches, instant = st.session_state['donnees']
    st.session_state['donnees'] = (nom, [tache if str(t.get('id')) == str(tache['id']) else t for t in taches], instant)


def champ_prochaine_action(tache):
    noms = {normaliser_nom_champ(n) for n in ALIASES_CHAMPS['Prochaine action']}
    candidats = [c for c in tache.get('custom_fields', []) if normaliser_nom_champ(c.get('name', '')) in noms]
    if len(candidats) != 1 or candidats[0].get('type') != 'date':
        raise ValueError('Ajoutez dans ClickUp un champ Date nommé « A - Prochaine action », puis actualisez les données.')
    return candidats[0]


def enregistrer_prochaine_action(task_id, cle, jour):
    if not isinstance(jour, date) or jour < aujourd_hui():
        raise ValueError('Choisissez une date à partir d’aujourd’hui.')
    frais = lire_tache(task_id, cle)
    if normaliser_nom_champ((frais.get('status') or {}).get('status', '')) != normaliser_nom_champ(STATUT_OFFRES):
        raise ValueError('Le statut du projet a changé. Actualisez les données.')
    champ = champ_prochaine_action(frais)
    if lire_date(champ.get('value')) == jour:
        return frais
    valeur = int(datetime.combine(jour, datetime.min.time(), tzinfo=ZoneInfo('Europe/Zurich')).replace(hour=12).timestamp() * 1000)
    req = Request(f"https://api.clickup.com/api/v2/task/{task_id}/field/{champ['id']}",
                  data=json.dumps({'value': valeur, 'value_options': {'time': False}}).encode(), method='POST',
                  headers={'Authorization': cle, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, context=contexte_tls(), timeout=30) as rep:
            rep.read()
    except (URLError, TimeoutError, ssl.SSLError):
        pass
    resultat = lire_tache(task_id, cle)
    if lire_date(champ_prochaine_action(resultat).get('value')) != jour:
        raise ValueError('La prochaine action n’est pas confirmée dans ClickUp. Réessayez son enregistrement.')
    return resultat


@st.dialog('Mettre le client en attente')
def confirmer_mise_en_attente(r):
    st.write(r['Projet'])
    st.caption('Choisissez la date de reprise du suivi. Le client apparaîtra dans En attente jusqu’à cette date. Cette action ne comptabilise pas de relance effectuée.')
    demain = aujourd_hui() + timedelta(days=1)
    if r['_admin_ao'] and isinstance(r['Prochain contrôle'], date):
        demain = max(demain, echeance_relance(r['Offre envoyée'], [r[c] for c in COLONNES_RELANCES], admin_ao=True)[1] or demain)
    prochaine = r['Prochain contrôle'] if r['Prochaine action'] is not None else None
    date_initiale = prochaine if isinstance(prochaine, date) and prochaine >= demain else max(demain, aujourd_hui() + timedelta(days=int(r['Délai automatique'])))
    jour = st.date_input('Recontacter le client le', value=date_initiale,
                         min_value=demain, format='DD.MM.YYYY',
                         key='date_mise_en_attente_' + r['ID'])
    valider, annuler = st.columns(2)
    with valider:
        confirmation = st.button('Confirmer la mise en attente',
                                 key='confirmer_attente_' + r['ID'],
                                 type='primary', use_container_width=True,
                                 disabled=jour is None or not compte_personnel_disponible())
    with annuler:
        if st.button('Annuler', key='annuler_attente_' + r['ID'], use_container_width=True):
            actualiser_sans_deplacer()
    if confirmation:
        try:
            if not isinstance(jour, date) or jour < demain:
                raise ValueError('Choisissez une date de reprise à partir du ' + demain.strftime('%d.%m.%Y') + '.')
            with chargement_constrelec('Mise en attente du client…'):
                cle = cle_actions_personnelles()
                actualiser_tache_session(enregistrer_prochaine_action(r['ID'], cle, jour))
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
            st.error(str(erreur))
        else:
            st.session_state['relance_message'] = 'Client en attente jusqu’au ' + jour.strftime('%d.%m.%Y') + ' · ' + r['Projet']
            actualiser_sans_deplacer()


@st.dialog('Confirmer la relance effectuée')
def confirmer_relance_et_date(r, choix, enregistrer):
    st.write(r['Projet'])
    if r['Niveau relance'] == MAX_RELANCES:
        st.info('Il s’agit de la 4e et dernière relance. Aucun nouveau rappel ne sera programmé.')
        if st.button('Valider la 4e et dernière relance', key='valider_derniere_relance_' + r['ID'],
                     type='primary', use_container_width=True):
            try:
                with chargement_constrelec('Enregistrement de la dernière relance dans ClickUp…'):
                    enregistrer(r, cle_actions_personnelles(), choix)
            except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
                st.error(str(erreur))
            else:
                st.session_state['relance_message'] = '4e et dernière relance enregistrée · ' + r['Projet']
                actualiser_sans_deplacer()
        return
    terminee = st.session_state.get('_relance_dialogue_terminee_' + r['ID'], False)
    delai = int(r['Délai automatique'])
    minimum = aujourd_hui() + timedelta(days=60) if r['_admin_ao'] else aujourd_hui()
    automatique = st.button(f'Rappel automatique dans {delai} jours',
                             key='rappel_auto_' + r['ID'], type='primary', use_container_width=True)
    st.markdown('**Ou choisir une date manuellement**')
    st.caption('La date sera enregistrée dans A - Prochaine action. Une date future place le projet dans En attente jusqu’à son échéance.')
    if r['_admin_ao']:
        st.caption('AO du portefeuille Admin : prochaine relance au plus tôt dans 60 jours.')
    date_manuelle = st.date_input('Prochaine action', value=None,
                                 min_value=minimum, format='DD.MM.YYYY',
                                 key='date_manuelle_relance_' + r['ID'])
    manuel = st.button('Enregistrer cette date' if terminee else 'Valider la relance avec cette date',
                       key='rappel_manuel_' + r['ID'], disabled=date_manuelle is None,
                       use_container_width=True)
    if automatique or manuel:
        jour = aujourd_hui() + timedelta(days=delai) if automatique else date_manuelle
        try:
            if not isinstance(jour, date) or jour < minimum:
                raise ValueError('Choisissez une prochaine action à partir du ' + minimum.strftime('%d.%m.%Y') + '.')
            with chargement_constrelec('Enregistrement dans ClickUp…'):
                cle = cle_actions_personnelles()
                if not terminee:
                    # Vérifier la configuration avant toute écriture de la relance.
                    champ_prochaine_action(lire_tache(r['ID'], cle))
                    enregistrer(r, cle, choix)
                    st.session_state['_relance_dialogue_terminee_' + r['ID']] = True
                st.session_state.setdefault('_actions_a_enregistrer', {})[r['ID']] = {'projet': r['Projet'], 'jour': jour}
                actualiser_tache_session(enregistrer_prochaine_action(r['ID'], cle, jour))
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
            st.error(str(erreur))
            if st.session_state.get('_relance_dialogue_terminee_' + r['ID']):
                st.info('La relance est enregistrée. Une nouvelle confirmation ne réenregistrera que la prochaine action.')
        else:
            st.session_state.get('_actions_a_enregistrer', {}).pop(r['ID'], None)
            st.session_state.pop('_relance_dialogue_terminee_' + r['ID'], None)
            st.session_state['relance_message'] = 'Relance effectuée · Prochaine action : ' + jour.strftime('%d.%m.%Y')
            actualiser_sans_deplacer()


def statut_perdu_clickup(liste):
    candidats = [x['status'] for x in liste.get('statuses', [])
                 if normaliser_nom_champ(x.get('status', '')) == normaliser_nom_champ(STATUT_PERDU)]
    if len(candidats) != 1:
        raise ValueError(f'Le statut « {STATUT_PERDU} » est introuvable ou ambigu dans la liste ClickUp Constrelec.')
    return candidats[0]


def enregistrer_offre_perdue(task_id, cle, statut, motif):
    # Le statut doit exister dans la liste ; aucune création ou suppression de tâche.
    liste = lire_api(f'list/{LISTE_ID}', cle)
    disponibles = [statut_perdu_clickup(liste)]
    if not statut or statut not in disponibles or normaliser_nom_champ(statut) == normaliser_nom_champ(STATUT_OFFRES):
        raise ValueError('Sélectionnez le statut ClickUp correspondant à une offre perdue.')
    frais = lire_tache(task_id, cle)
    actuel = (frais.get('status') or {}).get('status', '')
    if actuel != statut and normaliser_nom_champ(actuel) != normaliser_nom_champ(STATUT_OFFRES):
        raise ValueError('Le statut de ce projet a changé. Actualisez avant de continuer.')
    champ = champ_motif_perte(frais)
    options = [o for o in champ.get('type_config', {}).get('options', []) if o.get('name') == motif]
    if len(options) != 1 or not options[0].get('id'):
        raise ValueError('Sélectionnez un motif de perte valide et unique.')
    if decoder(champ) != motif:
        requete = Request(f"https://api.clickup.com/api/v2/task/{task_id}/field/{champ['id']}",
                          method='POST', data=json.dumps({'value': options[0]['id']}).encode(),
                          headers={'Authorization': cle, 'Content-Type': 'application/json'})
        try:
            with urlopen(requete, context=contexte_tls(), timeout=30) as rep:
                rep.read()
        except (URLError, TimeoutError, ssl.SSLError):
            pass  # Relire pour confirmer ; ne jamais renvoyer automatiquement.
        frais = lire_tache(task_id, cle)
        if decoder(champ_motif_perte(frais)) != motif:
            raise ValueError('Motif non confirmé dans ClickUp. Le statut n’a pas été modifié. Actualisez avant de réessayer.')
    actuel = (frais.get('status') or {}).get('status', '')
    if actuel == statut:
        return frais
    if normaliser_nom_champ(actuel) != normaliser_nom_champ(STATUT_OFFRES):
        raise ValueError('Le motif est enregistré, mais le statut a changé entre-temps. Actualisez les données.')
    req = Request(f'https://api.clickup.com/api/v2/task/{task_id}', method='PUT',
                  data=json.dumps({'status': statut}).encode(),
                  headers={'Authorization': cle, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, context=contexte_tls(), timeout=30) as rep:
            rep.read()
    except (URLError, TimeoutError, ssl.SSLError):
        resultat = lire_tache(task_id, cle)
        if (resultat.get('status') or {}).get('status') != statut:
            raise ValueError('Changement non confirmé. Actualisez avant de réessayer.')
    else:
        resultat = lire_tache(task_id, cle)
    if (resultat.get('status') or {}).get('status') != statut:
        raise ValueError('Le statut relu diffère. Actualisez les données.')
    return resultat


@st.dialog('Marquer l’offre comme perdue')
def confirmer_offre_perdue(task_id, projet):
    st.write(projet)
    try:
        cle = cle_actions_personnelles()
        liste = lire_api(f'list/{LISTE_ID}', cle)
        champ = champ_motif_perte(lire_tache(task_id, cle))
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
        st.error('Impossible de préparer la mise à jour : ' + str(erreur))
        return
    try:
        statut = statut_perdu_clickup(liste)
    except ValueError as erreur:
        st.error(str(erreur))
        return
    motifs = [o['name'] for o in champ.get('type_config', {}).get('options', []) if o.get('name')]
    motif = st.selectbox('Raison de la perte', motifs, index=None, placeholder='Choisir une raison', key='motif_perte_' + task_id)
    st.caption('Le projet quittera les relances actives. Son historique restera conservé.')
    if st.button('Confirmer : offre perdue', disabled=not statut or not motif, type='primary', use_container_width=True):
        try:
            with chargement_constrelec('Mise à jour du statut…'):
                cle = cle_actions_personnelles()
                actualiser_tache_session(enregistrer_offre_perdue(task_id, cle, statut, motif))
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
            st.error(str(erreur))
        else:
            st.session_state.pop('choix_chance_local_' + task_id, None)
            st.session_state['relance_message'] = 'Offre marquée comme perdue · ' + projet
            actualiser_sans_deplacer()


# Connexion personnelle pour toutes les modifications. Aucun jeton dans les URL,
# fichiers ou caches de données. Le relais entre onglets est temporaire et privé.
# Récapitulatifs par courriel désactivés : aucun destinataire Constrelec réutilisé.


class RegistrePresences:
    """Présences éphémères du processus, sans jetons ni données clients."""
    def __init__(self):
        self.verrou = threading.Lock()
        self.sessions = {}

    def signaler(self, session, utilisateur=None, maintenant=None):
        instant = time.monotonic() if maintenant is None else maintenant
        with self.verrou:
            self.sessions = {k: v for k, v in self.sessions.items() if instant - v['signal'] < 120}
            if session not in self.sessions and len(self.sessions) >= 1000:
                self.sessions.pop(min(self.sessions, key=lambda k: self.sessions[k]['signal']))
            self.sessions[session] = {
                'id': str(utilisateur['id']) if utilisateur and utilisateur.get('id') else '',
                'nom': str(utilisateur.get('nom') or 'Compte ClickUp')[:120] if utilisateur else '',
                'signal': instant,
            }
            personnes = {}
            anonymes = 0
            for presence in self.sessions.values():
                if presence['id']:
                    ancien = personnes.get(presence['id'])
                    if ancien is None or presence['signal'] > ancien['signal']:
                        personnes[presence['id']] = dict(presence)
                else:
                    anonymes += 1
            return sorted(personnes.values(), key=lambda p: p['nom'].casefold()), anonymes


@st.cache_resource
def registre_presences():
    return RegistrePresences()


@st.fragment(run_every=30)
def afficher_presences():
    session = st.session_state.setdefault('_presence_session', secrets.token_urlsafe(24))
    personnel = st.session_state.get('_clickup_personnel') or {}
    utilisateur = personnel.get('utilisateur')
    personnes, anonymes = registre_presences().signaler(session, utilisateur)
    titre = '● Utilisateurs connectés · ' + str(len(personnes))
    if anonymes:
        titre += ' (+' + str(anonymes) + ' non identifié' + ('s' if anonymes > 1 else '') + ')'
    with st.expander(titre, expanded=False):
        st.markdown('**Personnes connectées**')
        if not personnes:
            st.caption('Aucun compte identifié pour le moment.')
        for personne in personnes:
            vous = ' · vous' if utilisateur and str(utilisateur.get('id')) == personne['id'] else ''
            st.markdown('<div class="presence-personne"><span class="presence-point">●</span> '
                        + escape(personne['nom']) + '<span class="presence-vous">' + vous + '</span></div>',
                        unsafe_allow_html=True)
        if anonymes:
            st.caption(str(anonymes) + ' session(s) sans compte ClickUp synchronisé.')
        if not utilisateur:
            st.info('Pour afficher votre nom ici, cliquez sur « Synchroniser mon ClickUp » dans la colonne de gauche.')
        st.caption('Présence actualisée toutes les 30 s. Retrait après 2 min sans signal. Un onglet ouvert peut rester connecté sans activité.')


class RelaisOAuth:
    def __init__(self):
        self.verrou = threading.Lock()
        self.demandes = {}

    def nettoyer(self):
        maintenant = time.monotonic()
        for identifiant in list(self.demandes):
            if self.demandes[identifiant]['expiration'] <= maintenant:
                del self.demandes[identifiant]

    def creer(self):
        with self.verrou:
            self.nettoyer()
            if len(self.demandes) >= 1000:
                raise ValueError('Trop de connexions en cours. Réessayez plus tard.')
            etat, preuve = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.demandes[etat] = {'preuve': preuve, 'expiration': time.monotonic()+600, 'statut': 'attente'}
            return etat, preuve

    def reserver(self, etat):
        with self.verrou:
            self.nettoyer()
            demande = self.demandes.get(etat)
            if not demande or demande['statut'] != 'attente':
                return False
            demande['statut'] = 'traitement'
            return True

    def terminer(self, etat, resultat):
        with self.verrou:
            self.nettoyer()
            demande = self.demandes.get(etat)
            if demande and demande['statut'] == 'traitement':
                demande.update(statut='termine', resultat=resultat)
                return True
            return False

    def recuperer(self, etat, preuve):
        with self.verrou:
            self.nettoyer()
            demande = self.demandes.get(etat)
            if not demande or not secrets.compare_digest(demande['preuve'], preuve):
                return {'erreur': 'Connexion expirée. Recommencez la connexion.'}
            if demande['statut'] != 'termine':
                return None
            del self.demandes[etat]
            return demande['resultat']

    def annuler(self, etat, preuve):
        with self.verrou:
            self.nettoyer()
            demande = self.demandes.get(etat)
            if demande and secrets.compare_digest(demande['preuve'], preuve):
                del self.demandes[etat]

@st.cache_resource(show_spinner=False)
def relais_oauth():
    return RelaisOAuth()


def secret_oauth(nom):
    valeur = os.environ.get(nom, '').strip()
    if not valeur:
        try:
            valeur = str(st.secrets.get(nom, '')).strip()
        except FileNotFoundError:
            pass
    return valeur


def configuration_oauth():
    config = {nom: secret_oauth('CONSTRELEC_CLICKUP_OAUTH_'+nom.upper())
              for nom in ('client_id', 'client_secret', 'redirect_uri')}
    url = urlparse(config['redirect_uri'])
    if not all(config.values()) or url.scheme != 'https' or not url.netloc or url.query or url.fragment or url.username:
        return None
    return config


def requete_oauth_json(url, jeton=None, donnees=None):
    entetes = {'Accept': 'application/json'}
    if jeton:
        entetes['Authorization'] = 'Bearer ' + jeton
    if donnees is not None:
        entetes['Content-Type'] = 'application/json'
    requete = Request(url, headers=entetes,
                      data=json.dumps(donnees).encode('utf-8') if donnees is not None else None,
                      method='POST' if donnees is not None else 'GET')
    with urlopen(requete, timeout=30, context=contexte_tls()) as reponse:
        return json.load(reponse)


def utilisateur_oauth(jeton):
    utilisateur = requete_oauth_json('https://api.clickup.com/api/v2/user', jeton).get('user') or {}
    if not utilisateur.get('id') or not utilisateur.get('username'):
        raise ValueError('Identité ClickUp non confirmée.')
    return {'id': str(utilisateur['id']), 'nom': utilisateur['username']}


def compte_personnel_disponible():
    personnel = st.session_state.get('_clickup_personnel') or {}
    return bool(personnel.get('jeton') and (personnel.get('utilisateur') or {}).get('id'))


def cle_actions_personnelles():
    """Vérifier l'auteur avant écriture, sans repli vers la clé serveur."""
    personnel = st.session_state.get('_clickup_personnel') or {}
    if not compte_personnel_disponible():
        raise ValueError('Cliquez sur « Synchroniser mon ClickUp » pour enregistrer vos modifications sous votre compte.')
    try:
        auteur = utilisateur_oauth(personnel['jeton'])
        if str(auteur['id']) != str(personnel['utilisateur']['id']):
            raise ValueError('Le compte ClickUp a changé. Reconnectez votre compte personnel.')
    except HTTPError as erreur:
        if erreur.code in (401, 403):
            st.session_state.pop('_clickup_personnel', None)
            raise ValueError('Votre connexion personnelle ClickUp doit être renouvelée : cliquez sur « Synchroniser mon ClickUp ».') from erreur
        raise
    except (ValueError, KeyError, TypeError):
        st.session_state.pop('_clickup_personnel', None)
        raise ValueError('Identité ClickUp non confirmée. Reconnectez votre compte personnel.')
    return 'Bearer ' + personnel['jeton']


def fermer_onglet_oauth():
    # Ne ferme que l'onglet de retour OAuth validé, jamais la fenêtre principale.
    # Aucun jeton ni identifiant de connexion n'est envoyé à ce composant.
    composants.html("""
    <style>body{margin:0;color:#cbd2da;font:14px sans-serif;background:transparent}
    #secours{display:none;padding:8px 0}
</style>
    <div id="secours">Votre autorisation ClickUp a été reçue.
    Si cet onglet reste ouvert, fermez-le pour retrouver le logiciel.</div>
    <script>
    setTimeout(function(){
        try { window.top.close(); } catch (e) {}
        setTimeout(function(){document.getElementById('secours').style.display='block';},1200);
    },150);
    </script>
    """, height=75)


def retour_oauth():
    # Le nouvel onglet ne reçoit jamais la connexion : seul l'onglet ayant
    # créé la demande possède la preuve privée pour récupérer son résultat.
    if not any(k in st.query_params for k in ('code', 'state', 'error')):
        if st.session_state.get('_oauth_retour_message'):
            if st.session_state.get('_oauth_retour_reussi'):
                fermer_onglet_oauth()
            else:
                st.info(st.session_state['_oauth_retour_message'])
            st.stop()
        return
    parametres = {k: st.query_params.get_all(k) for k in ('code', 'state', 'error')}
    for k in ('code', 'state', 'error', 'error_description'):
        if k in st.query_params:
            del st.query_params[k]
    etats, codes = parametres['state'], parametres['code']
    config = configuration_oauth()
    if not config or len(etats) != 1 or (not parametres['error'] and len(codes) != 1) or not relais_oauth().reserver(etats[0]):
        message = 'Connexion invalide ou expirée. Revenez à votre fenêtre initiale et recommencez.'
    else:
        etat = etats[0]
        resultat = {'erreur': 'Autorisation refusée. Vous pouvez recommencer la connexion.'}
        if not parametres['error']:
            try:
                reponse = requete_oauth_json('https://api.clickup.com/api/v2/oauth/token', donnees={
                    'client_id': config['client_id'], 'client_secret': config['client_secret'], 'code': codes[0]})
                jeton = reponse.get('access_token')
                if not isinstance(jeton, str) or not jeton or any(c.isspace() for c in jeton):
                    raise ValueError('Jeton absent.')
                resultat = {'jeton': jeton, 'utilisateur': utilisateur_oauth(jeton)}
            except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError, KeyError, TypeError):
                resultat = {'erreur': 'Connexion ClickUp non confirmée. Vérifiez la configuration puis recommencez.'}
        livre = relais_oauth().terminer(etat, resultat)
        st.session_state['_oauth_retour_reussi'] = bool(livre and 'jeton' in resultat)
        message = ('Autorisation reçue. Vous pouvez fermer cet onglet et revenir dans votre fenêtre initiale : la connexion y est finalisée automatiquement.'
                   if livre and 'jeton' in resultat else
                   'Connexion non aboutie. Revenez dans votre fenêtre initiale pour recommencer.')
    st.session_state['_oauth_retour_message'] = message
    if st.session_state.get('_oauth_retour_reussi'):
        fermer_onglet_oauth()
    else:
        st.info(message)
    st.stop()


@st.fragment(run_every=None if st.session_state.get('_clickup_personnel') else 2)
def afficher_connexion_commentaires():
    personnel = st.session_state.get('_clickup_personnel')
    if personnel:
        st.success('ClickUp synchronisé : ' + personnel['utilisateur']['nom'], icon='✅')
        with st.expander('Mon compte ClickUp'):
            st.caption('Relances et commentaires enregistrés sous votre identité ClickUp.')
            st.caption('ID du compte : ' + personnel['utilisateur']['id'])
            if st.button('Déconnecter mon compte ClickUp', key='oauth_deconnexion', use_container_width=True):
                st.session_state.pop('_clickup_personnel', None)
                st.rerun()
        return
    config = configuration_oauth()
    if not config:
        with st.container(key='action_clickup_connexion'):
            st.button('Synchroniser mon compte ClickUp', key='oauth_config_manquante',
                      disabled=True, use_container_width=True,
                      help='La synchronisation nécessite une application OAuth ClickUp configurée dans les Secrets Streamlit.')
        st.caption('Synchronisation à activer par l’administrateur (identifiant OAuth, secret OAuth et URL de retour).')
        return
    attente = st.session_state.get('_oauth_demande')
    if attente:
        resultat = relais_oauth().recuperer(*attente)
        if resultat is not None:
            st.session_state.pop('_oauth_demande', None)
            if 'jeton' in resultat:
                st.session_state['_clickup_personnel'] = resultat
                # La synchronisation personnalise les écritures, mais ne modifie pas le portefeuille global.
                st.session_state['vendeur_actif'] = 'Portefeuille global'
                st.session_state['_oauth_confirmation'] = resultat['utilisateur']['nom']
                st.session_state.pop('_oauth_message', None)
                st.rerun()
            elif resultat.get('erreur') != 'Connexion expirée. Recommencez la connexion.':
                st.session_state['_oauth_message'] = resultat['erreur']
    if not st.session_state.get('_oauth_demande'):
        try:
            st.session_state['_oauth_demande'] = relais_oauth().creer()
        except ValueError as erreur:
            st.caption(str(erreur))
            return
    attente = st.session_state['_oauth_demande']
    url = 'https://app.clickup.com/api?' + urlencode({
        'client_id': config['client_id'], 'redirect_uri': config['redirect_uri'], 'state': attente[0]})
    with st.container(key='action_clickup_connexion'):
        st.link_button('Synchroniser mon compte ClickUp', url, type='primary', use_container_width=True,
                       help='Connecter mon compte ClickUp pour enregistrer mes relances et mes commentaires.')
    st.caption('Connectez votre compte ClickUp personnel : vos commentaires et relances apparaîtront sous votre nom.')
    if st.session_state.get('_oauth_message'):
        st.caption(st.session_state['_oauth_message'])


def publier_commentaire(task_id, texte, cle):
    contexte = contexte_tls()
    requete = Request(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        data=json.dumps({"comment_text": texte, "notify_all": False}).encode("utf-8"),
        method="POST",
        headers={"Authorization": cle, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urlopen(requete, context=contexte, timeout=30) as reponse:
        resultat = json.load(reponse)
    if not resultat.get("id"):
        raise ValueError("Réponse ClickUp sans confirmation du commentaire.")
    return resultat


def soumettre_commentaire(task_id, cle_saisie):
    texte = st.session_state.get(cle_saisie, "").strip()
    retour = "retour_commentaire_" + task_id
    blocage = "commentaire_incertain_" + task_id
    if not texte:
        st.session_state[retour] = ("info", "Saisissez un commentaire avant de l’envoyer.")
        return
    if st.session_state.get(blocage):
        return
    personnel = st.session_state.get('_clickup_personnel')
    if not personnel:
        st.session_state[retour] = ('error', 'Connectez votre compte ClickUp dans le panneau de gauche pour publier.')
        return
    try:
        auteur = utilisateur_oauth(personnel['jeton'])
        if auteur['id'] != personnel['utilisateur']['id']:
            raise ValueError('Identité différente.')
    except (URLError, TimeoutError, ssl.SSLError) as erreur:
        if isinstance(erreur, HTTPError) and erreur.code in (401, 403):
            st.session_state.pop('_clickup_personnel', None)
        st.session_state[retour] = ('error', 'Vérification ClickUp temporairement impossible. Votre texte est conservé ; réessayez dans un instant.')
        return
    except (ValueError, KeyError, TypeError):
        st.session_state.pop('_clickup_personnel', None)
        st.session_state[retour] = ('error', 'Connexion personnelle non confirmée. Reconnectez-vous à ClickUp. Votre texte est conservé.')
        return
    cle = 'Bearer ' + personnel['jeton']
    # Aucun nouvel essai automatique après une réponse réseau incertaine.
    st.session_state[blocage] = True
    try:
        publier_commentaire(task_id, texte, cle)
    except HTTPError as erreur:
        if 400 <= erreur.code < 500:
            st.session_state.pop(blocage, None)
        st.session_state[retour] = ("error", f"Envoi non confirmé (HTTP {erreur.code}). Votre texte est conservé.")
    except (URLError, TimeoutError, ssl.SSLError, ValueError):
        st.session_state[retour] = ("error", "Envoi non confirmé. Vérifiez les commentaires dans ClickUp avant un nouvel essai. Votre texte est conservé.")
    else:
        st.session_state.pop(blocage, None)
        st.session_state[cle_saisie] = ""
        st.session_state[retour] = ("success", "Commentaire publié dans ClickUp par " + auteur["nom"] + ".")


def date_texte(valeur):
    try:
        return datetime.fromtimestamp(int(valeur) / 1000).strftime("%d/%m/%Y") if valeur else ""
    except (ValueError, TypeError, OSError, OverflowError):
        return ""





def afficher_chargement(zone, pourcentage, texte):
    pourcentage = max(0, min(100, int(pourcentage)))
    zone["barre"].progress(pourcentage, text=f"{pourcentage} % · {texte}")



def charger(cle, progression):
    # Ecran d'accueil transitoire : la photo fournie reste visible pendant la
    # lecture de la liste ClickUp et disparaît dès la préparation terminée.
    with progression.container():
        with st.container(key="chargement_accueil"):
            gauche, droite = st.columns([1.3, 1], vertical_alignment="center", gap="large")
            with gauche:
                st.image(
                    Path(__file__).parent / "assets" / "photo_chargement_constrelec.jpg",
                    use_container_width=True,
                )
            with droite:
                st.markdown(
                    '<div class="chargement-texte">'
                    '<span class="eyebrow">CONSTRELEC SA · PORTEFEUILLE COMMERCIAL</span>'
                    '<h1>Relances clients</h1>'
                    '<p>Chargement de vos offres et de vos prochaines relances depuis ClickUp.</p>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                barre = st.progress(0, text="0 % · Connexion à ClickUp…")
    progression = {"barre": barre}
    afficher_chargement(progression, 0, "Connexion à la liste ClickUp…")
    liste = lire_api(f"list/{LISTE_ID}", cle)
    try:
        definitions = {c["id"]: c for champ in lire_api(f"list/{LISTE_ID}/field", cle).get("fields", [])
                       if (c := compacter_champ(champ)) is not None}
    except HTTPError as erreur:
        if erreur.code not in (403, 404):
            raise
        definitions = {}
    try:
        estimation = max(100, int(liste.get("task_count") or 0))
    except (ValueError, TypeError):
        estimation = 100
    # Le compteur de liste peut exclure les sous-tâches : estimation plafonnée
    # à 95 %, puis 100 % uniquement après réception de la dernière page.
    precedent = 5
    afficher_chargement(progression, 5, "Liste identifiée · lecture des offres…")
    taches = {}
    page = 0
    def lire_page(numero):
        reponse = lire_api(f"list/{LISTE_ID}/task?page={numero}&include_closed=true"
                           "&subtasks=true&include_timl=true&archived=false", cle)
        # Les futures ne retiennent jamais les descriptions et pièces jointes brutes.
        if isinstance(reponse.get('tasks'), list):
            return {'tasks': [enrichir_tache(t, definitions) for t in reponse['tasks']],
                    'last_page': reponse.get('last_page')}
        return reponse
    with ThreadPoolExecutor(max_workers=3) as executeur:
        termine = False
        while not termine:
            futures = {n: executeur.submit(lire_page, n) for n in range(page, page + 3)}
            for numero, futur in futures.items():
                reponse = futur.result()
                if 'tasks' not in reponse or not isinstance(reponse['tasks'], list):
                    raise ValueError('Réponse ClickUp incomplète : les anciennes données sont conservées.')
                lot = reponse['tasks']
                if not lot:
                    termine = True
                    break
                nouveaux = {t['id']: t for t in lot}
                if not (nouveaux.keys() - taches.keys()):
                    raise ValueError('ClickUp renvoie une page répétée. Réessayez l’actualisation.')
                taches.update(nouveaux)
                precedent = max(precedent, min(95, 5 + int(90 * len(taches) / estimation)))
                afficher_chargement(progression, precedent, f"Progression estimée · {len(taches)} tâches reçues · page {numero + 1}")
                if reponse.get('last_page') is True:
                    termine = True
                    break
            if termine:
                for futur in futures.values():
                    futur.cancel()
            page += 3
    st.session_state['_definitions_champs'] = definitions
    afficher_chargement(progression, 98, "Préparation des portefeuilles…")
    return liste.get("name", "Établissement des offres"), list(taches.values())


st.markdown("""<style>
.stApp {background:radial-gradient(ellipse at 85% 0%,#281E1C 0,transparent 45%),#101419;color:#edf0f4}
[data-testid="stHeader"] {background:#101419}
[data-testid="stSidebar"] {background:#111419;border-right:1px solid #303033}
.block-container {max-width:1450px;padding-top:5rem;padding-bottom:4rem}
h1,h2,h3 {letter-spacing:-.035em;color:#f4f6f8!important}
[data-testid="stMetric"] {background:radial-gradient(ellipse at 100% 0%,#E2232328,transparent 75%),linear-gradient(135deg,#272321,#191D23);border:1px solid #383536;border-top:2px solid #E22323;border-radius:18px;padding:22px;box-shadow:0 10px 30px #00000020}
[data-testid="stMetricValue"] {color:#E22323}
[data-testid="stCaptionContainer"] {color:#a9b4c2}
.hero {display:flex;align-items:center;justify-content:space-between;gap:24px;border-bottom:1px solid #E2232345;padding-bottom:26px;margin-bottom:26px;flex-wrap:wrap}
.brand {background:transparent;padding:0 0 16px;width:100%;max-width:230px;margin:-28px 0 4px;border-bottom:1px solid #E2232340}
.brand img {width:100%;display:block}
.eyebrow {font-size:12px;letter-spacing:.18em;color:#E22323;font-weight:700}
.hero h1 {font-size:36px;margin:8px 0;line-height:1.15}
.hero p {color:#a9b4c2;margin:0;font-size:15px}
.step {font-size:12px;letter-spacing:.13em;color:#E22323;margin:22px 0 6px;font-weight:700}
.offer {background:radial-gradient(ellipse at 100% 0%,#E2232315,transparent 65%),linear-gradient(120deg,#272322,#191E24);border:1px solid #343A41;border-left:3px solid #E22323;border-radius:16px;padding:24px;margin-top:18px;box-shadow:0 8px 24px #00000018}
.offer h3 {font-size:20px;margin:8px 0 12px}
.badge {font-size:12px;color:#FFCECE;background:#E2232322;padding:5px 10px;border-radius:20px;display:inline-block}
.offer .details {display:flex;gap:26px;flex-wrap:wrap;color:#cbd4df;font-size:14px}
.offer .label {color:#93a1b2;font-size:11px;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:4px}

[data-testid="stMetricValue"]{font-size:clamp(24px,2.2vw,34px);font-weight:650;letter-spacing:-.035em}
[data-testid="stBaseButton-primary"]{background:#E22323!important;border:1px solid #E22323!important;color:#101419!important;font-weight:700!important;border-radius:10px!important}
[data-testid="stBaseButton-primary"]:hover{background:#EC715A!important;border-color:#EC715A!important}
[data-testid="stBaseButton-secondary"],[data-testid="stBaseLinkButton-secondary"]{background:#20252C;color:#EDF0F4;border:1px solid #454047;border-radius:10px;transition:border-color .2s,background .2s}
[data-testid="stBaseButton-secondary"]:hover,[data-testid="stBaseLinkButton-secondary"]:hover{border-color:#E22323;color:#FA9595;background:#2B2526}
button:focus-visible,a:focus-visible{outline:2px solid #E22323!important;outline-offset:3px}
[class*="st-key-attente_action_"] button{background:#E2232326!important;color:#FFB8B8!important;border:1px solid #E2232380!important;border-radius:10px!important}
[class*="st-key-attente_action_"] button:hover{background:#E2232345!important}
[class*="st-key-attente_action_"] button{width:100%!important;min-height:40px!important}
.label-tri{font-size:14px;font-weight:400;color:#EDF0F4;margin-bottom:8px}
.st-key-filtres_clients{background:#1B1E24;border:1px solid #454047;border-radius:12px;padding:16px;margin:0 0 12px}
[data-testid="stVegaLiteChart"], [data-testid="stVegaLiteChart"] canvas, [data-testid="stVegaLiteChart"] svg{background:transparent!important}
/* Première page de chargement : photo réelle de l'atelier Constrelec. */
.st-key-chargement_accueil{
    padding:clamp(14px,2.4vw,32px);
    background:linear-gradient(120deg,#181B20 0%,#20191A 100%);
    border:1px solid #383137;
    border-radius:22px;
    box-shadow:0 18px 45px #00000038;
    margin:12px 0 26px;
}
.st-key-chargement_accueil [data-testid="stImage"]{overflow:hidden;border-radius:15px}
.st-key-chargement_accueil [data-testid="stImage"] img{
    display:block;width:100%;height:clamp(260px,43vh,425px);
    object-fit:cover;object-position:42% center;
    border-radius:15px;
}
.chargement-texte .eyebrow{color:#F18888;font-size:11px;letter-spacing:.16em}
.chargement-texte h1{color:#FFF!important;font-size:clamp(30px,3vw,46px);line-height:1.12;margin:18px 0 14px;letter-spacing:-.04em}
.chargement-texte p{color:#CDD0D6;font-size:15px;line-height:1.65;margin:0 0 30px}
.st-key-chargement_accueil [data-testid="stProgress"]{margin-top:14px}
@media(max-width:850px){
    .st-key-chargement_accueil [data-testid="stImage"] img{height:clamp(220px,35vh,340px)}
    .chargement-texte p{margin-bottom:16px}
}
.chargement-constrelec{display:flex;align-items:center;gap:10px;color:#EDF0F4;font-size:14px;padding:8px 0}
.cercle-constrelec{display:inline-block;width:15px;height:15px;flex:0 0 15px;box-sizing:border-box;border:2px solid #E2232330;border-top-color:#E22323;border-radius:50%;animation:rotation-constrelec .75s linear infinite}
@keyframes rotation-constrelec{to{transform:rotate(360deg)}}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"]{gap:10px}
[data-testid="stSidebar"] [data-testid="stHeading"]{margin-top:0;padding-top:0}
[data-testid="stSidebar"] h3{padding-top:8px;padding-bottom:6px}
[data-testid="stSidebar"] h3.titre-portefeuilles{margin:0 0 24px!important;padding:8px 0 0!important}
[data-testid="stSidebar"] [class*="st-key-vendeur_"] button{justify-content:flex-start!important;text-align:left!important;padding-left:16px!important;padding-right:12px!important}
[data-testid="stSidebar"] [class*="st-key-vendeur_"] button p{text-align:left!important}
[data-testid="stSidebar"] .st-key-actualiser_donnees button{width:100%!important;min-height:56px!important;padding:14px 16px!important;background:#E2232326!important;border:1px solid #E2232380!important;color:#FFB8B8!important;border-radius:10px!important}
[data-testid="stSidebar"] .st-key-actualiser_donnees button:hover{background:#E2232345!important;border-color:#E22323!important}
[data-testid="stSidebar"] .st-key-actualiser_donnees button:disabled{opacity:.55!important}
/* Libellés lisibles sur les cartes et les filtres du thème sombre. */
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] span,
[data-testid="stRadio"] label,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] label span,
[data-testid="stRadio"] [data-testid="stMarkdownContainer"],
[data-testid="stRadio"] [data-testid="stMarkdownContainer"] p{
    color:#EDF0F4!important;opacity:1!important}
[data-testid="stMetricLabel"] p{font-size:14px!important;font-weight:500!important}
[data-testid="stRadio"] [role="radiogroup"]{color:#EDF0F4!important}

/* Streamlit 1.64 : champs React Aria et progression native. */
[data-testid="stTextInputRootElement"],
[data-testid="stSelectbox"] div:has(> input){
    background:#262730!important;color:#EDF0F4!important;
    border-color:#454047!important;color-scheme:dark}
[data-testid="stTextInputRootElement"] input,
[data-testid="stSelectbox"] input{
    background:transparent!important;color:#EDF0F4!important;
    -webkit-text-fill-color:#EDF0F4!important}
[data-testid="stSelectbox"] button,
[data-testid="stTextInputRootElement"] button{
    background:transparent!important;color:#EDF0F4!important}
[data-testid="stSelectboxVirtualDropdown"],
[data-testid="stSelectboxVirtualDropdown"] [role="option"],
[role="listbox"], [role="listbox"] [role="option"]{
    background:#262730!important;color:#EDF0F4!important}
[role="listbox"] [role="option"][data-focused],
[role="listbox"] [role="option"][aria-selected="true"]{
    background:#3A3030!important}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stSelectbox"] div:has(> input):focus-within{
    border-color:#E22323!important}
[data-testid="stProgressBarTrack"]{background:#34363B!important}
[data-testid="stProgressBarTrack"] > div{background:#E22323!important}
[data-testid="stProgress"] [data-baseweb="progress-bar"] > div > div > div{
    background:#E22323!important}
[data-testid="stBaseLinkButton-primary"],
[class*="st-key-action_outlook_"] a{
    background:#E22323!important;border:1px solid #E22323!important;
    color:#FFFFFF!important;border-radius:10px!important}
[data-testid="stBaseLinkButton-primary"]:hover,
[class*="st-key-action_outlook_"] a:hover{
    background:#EC715A!important;border-color:#EC715A!important}

/* Champs anthracite, même lorsque Streamlit Cloud utilise le thème clair. */
[data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stTextInput"] [data-baseweb="base-input"],
[data-testid="stSelectbox"] [data-baseweb="select"] > div{
    background-color:#262730!important;color:#EDF0F4!important;
    border-color:#454047!important;color-scheme:dark}
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] input{
    background-color:#262730!important;color:#EDF0F4!important;
    -webkit-text-fill-color:#EDF0F4!important;caret-color:#E22323}
[data-testid="stTextInput"] input::placeholder{
    color:#A9B4C2!important;-webkit-text-fill-color:#A9B4C2!important;opacity:1}
[data-testid="stTextInput"] label,[data-testid="stTextInput"] label p,
[data-testid="stSelectbox"] label,[data-testid="stSelectbox"] label p{
    color:#EDF0F4!important}
[data-testid="stSelectbox"] [data-baseweb="select"] span{color:#EDF0F4!important}
[data-testid="stSelectbox"] svg{fill:#EDF0F4!important;color:#EDF0F4!important}
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="popover"] [role="option"]{
    background-color:#262730!important;color:#EDF0F4!important}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="popover"] [role="option"][aria-selected="true"]{
    background-color:#3A3030!important}
[data-testid="stFormSubmitButton"] button{
    background-color:#20252C!important;color:#EDF0F4!important;border:1px solid #454047!important}
[data-testid="stFormSubmitButton"] button:disabled{
    background-color:#20252C!important;color:#8993A1!important;opacity:.65}
[data-testid="stTextInput"] [data-baseweb="input"]:focus-within,
[data-testid="stSelectbox"] [data-baseweb="select"]:focus-within > div{
    border-color:#E22323!important}

[class*="st-key-fiche_"]{background:linear-gradient(90deg,rgba(224,85,58,0) 0%,rgba(224,85,58,0.035) 35%,rgba(224,85,58,0.09) 70%,rgba(224,85,58,0.16) 100%),#171B20;border:1px solid #343A41;border-left:3px solid #E22323;border-radius:16px;padding:24px;margin-top:18px}
[class*="st-key-fiche_"] [data-testid="stForm"]{margin-top:20px}

[class*="st-key-chance_"] [data-testid="stHorizontalBlock"]{gap:5px!important;flex-wrap:nowrap!important}
[class*="st-key-chance_"] [data-testid="stColumn"]{min-width:0!important}
[class*="st-key-chance_"] button{min-height:32px!important;padding:3px 2px!important;border-radius:8px!important}
[class*="st-key-chance_"] button p{font-size:11px!important;white-space:nowrap!important}

/* Tons discrets comme les actions de relance ; contour renforcé pour le choix actif. */
[class*="st-key-chance_inconnue_"] button{background:#30343B!important;color:#D5D9E0!important;border:1px solid #9299A366!important;border-radius:10px!important;font-weight:650!important}
[class*="st-key-chance_inconnue_"] button:hover{background:#424850!important;border-color:#9299A3!important}
[class*="st-key-chance_inconnue_"] button[data-testid="stBaseButton-primary"]{background:#424850!important;border:2px solid #9299A3!important;box-shadow:0 0 0 1px #9299A333!important}
[class*="st-key-chance_faible_"] button{background:#47292D!important;color:#FFA0A0!important;border:1px solid #F06A6A66!important;border-radius:10px!important;font-weight:650!important}
[class*="st-key-chance_faible_"] button:hover{background:#613037!important;border-color:#F06A6A!important}
[class*="st-key-chance_faible_"] button[data-testid="stBaseButton-primary"]{background:#613037!important;border:2px solid #F06A6A!important;box-shadow:0 0 0 1px #F06A6A33!important}
[class*="st-key-chance_moyenne_"] button{background:#443C25!important;color:#F4D47A!important;border:1px solid #E9B94966!important;border-radius:10px!important;font-weight:650!important}
[class*="st-key-chance_moyenne_"] button:hover{background:#5B4D29!important;border-color:#E9B949!important}
[class*="st-key-chance_moyenne_"] button[data-testid="stBaseButton-primary"]{background:#5B4D29!important;border:2px solid #E9B949!important;box-shadow:0 0 0 1px #E9B94933!important}
[class*="st-key-chance_elevee_"] button{background:#213F30!important;color:#4FF080!important;border:1px solid #4FF08066!important;border-radius:10px!important;font-weight:650!important}
[class*="st-key-chance_elevee_"] button:hover{background:#2B513C!important;border-color:#4FF080!important}
[class*="st-key-chance_elevee_"] button[data-testid="stBaseButton-primary"]{background:#2B513C!important;border:2px solid #4FF080!important;box-shadow:0 0 0 1px #4FF08033!important}

[class*="st-key-chance_"] button{display:flex!important;align-items:center!important;justify-content:center!important;text-align:center!important}
[class*="st-key-chance_"] button [data-testid="stMarkdownContainer"]{display:flex!important;align-items:center!important;justify-content:center!important;width:100%!important}
[class*="st-key-chance_"] button p{margin:0!important;padding:0!important;text-align:center!important;line-height:1.2!important}
/* Même métrique que les autres cartes ; bouton transparent sur toute la surface. */
.st-key-historique_relances_action{position:relative}
.st-key-historique_relances_action [data-testid="stVerticalBlock"]{position:relative;gap:0!important}
.st-key-historique_relances_action .st-key-ouvrir_clients_relances{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;z-index:2}
.st-key-historique_relances_action .st-key-ouvrir_clients_relances [data-testid="stButton"]{height:100%!important}
.st-key-historique_relances_action button{width:100%!important;height:100%!important;position:absolute!important;inset:0!important;background:transparent!important;border:0!important;box-shadow:none!important;cursor:pointer!important;border-radius:18px!important}
.st-key-historique_relances_action button [data-testid="stMarkdownContainer"]{opacity:0!important}
.st-key-historique_relances_action [data-testid="stMetric"]{transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}
.st-key-historique_relances_action:has(button:hover) [data-testid="stMetric"],
.st-key-historique_relances_action:has(button:focus-visible) [data-testid="stMetric"]{border-color:#E22323!important;box-shadow:0 0 0 2px #E2232355,0 10px 30px #00000020!important;transform:translateY(-2px)}
.st-key-historique_relances_action button:focus-visible{outline:2px solid #FFB8B8!important;outline-offset:3px!important}
[data-testid="stDialog"] [role="dialog"]{background:#15191E!important;color:#edf0f4!important;border:1px solid #454047!important}
[class*="st-key-offre_perdue_"] button{background:#47292D!important;color:#FFA0A0!important;border:1px solid #F06A6A88!important;border-radius:10px!important}
[class*="st-key-offre_perdue_"] button:hover{background:#613037!important;border-color:#F06A6A!important}
.chance-label{font-size:11px;color:#edf0f4;white-space:nowrap}

.offer-content h3{font-size:20px;margin:12px 0 24px}
.offer-content .details{display:flex;gap:26px;flex-wrap:wrap;color:#cbd4df;font-size:14px}
.offer-content .label{color:#93a1b2;font-size:11px;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:4px}
[class*="st-key-validation_"] button{background:#213F30!important;border:1px solid #315A41!important;color:#4FF080!important;border-radius:12px!important;min-height:56px;font-weight:650}
[class*="st-key-validation_"] button:hover{background:#2B513C!important;border-color:#4FF080!important}
[class*="st-key-validation_"] button:disabled{opacity:.45}

.hero{position:relative;overflow:hidden;min-height:175px;padding:34px 40px;border:1px solid #F27B7B55;border-radius:24px;background:linear-gradient(105deg,#101419 0%,#21191B 38%,#6C1E1E 72%,#BE2020 100%);box-shadow:0 18px 45px #00000035;margin-bottom:30px}

.hero .eyebrow{color:#FFE3E3;font-size:11px;letter-spacing:.24em}
.hero h1{font-size:42px!important;font-weight:750!important;margin:12px 0!important;color:white!important}
.hero p{color:#FADCDC;font-size:14px}
[class*="st-key-action_"] button,[class*="st-key-action_"] a{min-height:46px;gap:9px;white-space:nowrap}
[class*="st-key-action_"] button:before,[class*="st-key-action_"] a:before{content:"";display:inline-block;width:24px;height:24px;flex-shrink:0;background-size:contain;background-repeat:no-repeat;background-position:center;border-radius:5px}
@media(max-width:800px){.hero{padding:26px;min-height:150px}.hero h1{font-size:32px!important}}
[class*="st-key-action_outlook_"] button:before,[class*="st-key-action_outlook_"] a:before{background-image:url("data:image/jpeg;base64,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")}
[class*="st-key-action_sharepoint_"] button:before,[class*="st-key-action_sharepoint_"] a:before{background-image:url("data:image/jpeg;base64,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")}
[class*="st-key-action_clickup_"] button:before,[class*="st-key-action_clickup_"] a:before{background-image:url("data:image/jpeg;base64,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")}
.offer-price{font-size:27px;font-weight:750;color:#FFB0B0;background:#E2232314;border:1px solid #E2232355;border-radius:12px;padding:12px 18px;line-height:1.25}.offer-price .label{color:#E9C4C4;font-size:10px;margin-bottom:7px}.offer-content .details{align-items:center}
.hero{border:0!important}
.hero::after{content:none!important;display:none!important}
.hero>div{position:relative;z-index:1}

@media(max-width:650px){}
/* Le logo reprend exactement le fond sombre de la barre latérale, sans encadré. */
.st-key-brand_sidebar{margin-top:-20px;margin-bottom:14px;padding:0 0 4px;background:transparent;border:0;border-radius:0;box-shadow:none}
.st-key-brand_sidebar [data-testid="stImage"]{width:100%;max-width:360px;margin:0 auto}
.st-key-brand_sidebar img{display:block;width:100%;height:auto;object-fit:contain}
[class*="st-key-action_"] button,[class*="st-key-action_"] a{padding:8px 10px;font-size:13px;white-space:normal;justify-content:center}
[class*="st-key-action_"] button:before,[class*="st-key-action_"] a:before{width:21px;height:21px}
[class*="st-key-actions_compactes_"]{width:550px!important;max-width:100%!important}
[class*="st-key-action_"] button,[class*="st-key-action_"] a{width:170px!important;max-width:100%!important;min-width:0!important;justify-content:center!important}
.tableau-global{max-width:960px;overflow-x:auto;margin:12px 0 18px;border-radius:12px;border:1px solid #343A41}
.tableau-global table{border-collapse:collapse;width:100%;font-size:14px;table-layout:auto}
.tableau-global th{background:#20252C;color:#BAC4CF;text-align:right;font-size:12px;font-weight:600}
.tableau-global td,.tableau-global th{padding:12px 14px;border-bottom:1px solid #30363D;white-space:nowrap}
.tableau-global td{text-align:right}.tableau-global td:first-child,.tableau-global th:first-child{text-align:left}
.tableau-global td:nth-child(4){font-weight:700;color:#FFB0B0}

[data-testid="stSidebar"] .st-key-action_clickup_connexion,
[data-testid="stSidebar"] .st-key-action_clickup_connexion [data-testid="stLinkButton"],
[data-testid="stSidebar"] .st-key-action_clickup_connexion a{
    width:100%!important;max-width:none!important;box-sizing:border-box!important;
}
[data-testid="stSidebar"] .st-key-action_clickup_connexion a{
    justify-content:flex-start!important;padding:10px 16px!important;
    white-space:normal!important;min-height:46px!important;
}
/* V70 : dialogues et navigation sombres, lisibles même avec le thème clair. */
[data-testid="stDialog"] [data-testid="stMarkdownContainer"],
[data-testid="stDialog"] [data-testid="stWidgetLabel"],
[data-testid="stDialog"] label {color:#FFFFFF!important}
[data-testid="stDialog"] [data-baseweb="input"],
[data-testid="stDialog"] [data-baseweb="input"] input,
[data-testid="stDialog"] [data-baseweb="select"] > div {background:#262930!important;color:#FFFFFF!important;border-color:#454951!important}
[data-testid="stSidebar"] [data-testid="stExpander"] details {background:#171B20!important;border:1px solid #343A42!important;border-radius:12px!important;overflow:hidden}
[data-testid="stSidebar"] [data-testid="stExpander"] summary {background:linear-gradient(100deg,#22272E,#1A1E24)!important;color:#FFFFFF!important;padding:14px 16px!important}
[data-testid="stSidebar"] [data-testid="stExpander"] summary:hover {background:#2C3037!important}
[data-testid="stSidebar"] [data-testid="stExpander"] summary p {color:#FFFFFF!important;font-weight:600!important}
[data-testid="stSidebar"] [data-testid="stExpander"] summary svg {color:#E22323!important}
[data-testid="stSidebar"] [data-testid="stExpanderDetails"] {background:#171B20!important;padding:10px!important}
[class*="st-key-offre_perdue_"] {width:100%!important}
[class*="st-key-offre_perdue_"] button {width:100%!important;min-height:40px!important}


[data-testid="stDialog"] [role="dialog"] [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] [role="dialog"] [data-testid="stWidgetLabel"] p,
[role="dialog"] [data-testid="stMarkdownContainer"] p,
[role="dialog"] [data-testid="stWidgetLabel"] p,
[role="dialog"] label p {color:#FFFFFF!important;-webkit-text-fill-color:#FFFFFF!important}
[data-testid="stSidebar"] [data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] [data-testid="stExpander"] summary span {color:#FFFFFF!important;-webkit-text-fill-color:#FFFFFF!important}

.agenda-actions{background:linear-gradient(110deg,#191D23,#252022);border:1px solid #454047;border-left:3px solid #E22323;border-radius:14px;padding:20px 22px;margin:20px 0 26px;color:#FFF}
.agenda-actions h3{color:#FFF!important;font-size:20px!important;margin:0 0 12px!important}
.agenda-actions p{color:#CBD0D7!important;margin:0}
.agenda-ligne{display:grid;grid-template-columns:minmax(0,1fr) auto 105px;gap:20px;align-items:center;padding:12px 0;border-top:1px solid #FFFFFF14}
.agenda-projet{overflow-wrap:anywhere;color:#FFF}.agenda-prix{color:#ED8C70;text-align:right;font-weight:600}.agenda-ligne time{color:#FFF;text-align:right;font-variant-numeric:tabular-nums}
@media(max-width:600px){.agenda-ligne{grid-template-columns:1fr auto;gap:8px}.agenda-projet{grid-column:1/-1}.agenda-prix{text-align:left}}

.st-key-presences_entete [data-testid="stExpander"] details{background:#1A1E24!important;border:1px solid #414750!important;border-radius:12px!important}
.st-key-presences_entete [data-testid="stExpander"] summary{background:#1A1E24!important;color:#FFF!important}
.st-key-presences_entete [data-testid="stExpander"] summary p{color:#FFF!important;font-size:14px!important}
.st-key-presences_entete [data-testid="stExpanderDetails"]{background:#1A1E24!important}
.presence-personne{color:#FFF;padding:7px 0;font-size:14px}.presence-point{color:#56C596;margin-right:6px}.presence-vous{color:#B7C0CC;font-size:12px}

/* Présence sur le bandeau supérieur, sans déplacer le contenu du portefeuille. */
.st-key-presences_entete{position:fixed!important;top:8px!important;right:180px!important;width:380px!important;max-width:calc(100vw - 260px)!important;z-index:999990!important;margin:0!important}
.st-key-presences_entete [data-testid="stExpander"] details{box-shadow:0 8px 24px #0005!important;border-color:#333B45!important;background:#101419!important}
.st-key-presences_entete [data-testid="stExpander"] summary{min-height:38px!important;padding:7px 12px!important;background:#101419!important}
.st-key-presences_entete [data-testid="stExpanderDetails"]{max-height:65vh;overflow-y:auto;background:#1A1E24!important}
.st-key-presences_entete [data-testid="stMarkdownContainer"] p{color:#FFF!important}
@media(max-width:700px){.st-key-presences_entete{right:65px!important;width:290px!important;max-width:calc(100vw - 125px)!important}.st-key-presences_entete [data-testid="stExpander"] summary p{font-size:12px!important}}

/* Nom du vendeur noir sur un en-tête clair lorsque le récapitulatif est ouvert. */
[data-testid="stDialog"] .st-key-recaps_vendeurs details[open] > summary{background:#F0E4DC!important;color:#111111!important}
[data-testid="stDialog"] .st-key-recaps_vendeurs details[open] > summary [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] .st-key-recaps_vendeurs details[open] > summary span,
[data-testid="stDialog"] .st-key-recaps_vendeurs details[open] > summary svg{color:#111111!important;-webkit-text-fill-color:#111111!important}
</style>""", unsafe_allow_html=True)
# Constrelec ne propose qu'une seule vue, pour tous les projets de sa liste ClickUp.
# Réinitialiser aussi les sessions ouvertes sur une version précédente.
st.session_state["vendeur_actif"] = "Portefeuille global"
vendeur_titre = st.session_state["vendeur_actif"]
if vendeur_titre == "Portefeuille global":
    vendeur_titre = "Relances clients"
with st.container(key='presences_entete'):
    afficher_presences()

with st.sidebar.container(key="brand_sidebar"):
    st.image(Path(__file__).parent / "assets" / "logo_constrelec.png", use_container_width=True)
retour_oauth()
with st.sidebar:
    afficher_connexion_commentaires()
if st.session_state.get('_oauth_confirmation'):
    st.success('Le compte ClickUp de ' + st.session_state.pop('_oauth_confirmation') + ' a bien été connecté à l’application Relances clients.')

# Cloud : clé fournie uniquement par les secrets du serveur.
# Windows local : conservation du coffre existant si aucun secret serveur.
cle_serveur = os.environ.get("CONSTRELEC_CLICKUP_API_KEY", "").strip()
if not cle_serveur:
    try:
        cle_serveur = str(st.secrets.get("CONSTRELEC_CLICKUP_API_KEY", "")).strip()
    except FileNotFoundError:
        pass
mode_serveur = bool(cle_serveur) or sys.platform != "win32"
coffre = None
SERVICE_CLE = "Constrelec.Relances.ClickUp"
COMPTE_CLE = "api_personnelle"
cle_enregistree = cle_serveur
if not mode_serveur:
    try:
        from keyring.backends.Windows import WinVaultKeyring
        coffre = WinVaultKeyring()
        cle_enregistree = coffre.get_password(SERVICE_CLE, COMPTE_CLE) or ""
    except (ImportError, RuntimeError):
        st.error("Installe le composant de mémorisation : py -m pip install keyring")
        st.stop()
    except Exception:
        st.error("Le gestionnaire d’identifiants Windows est inaccessible.")
        st.stop()

nouvelle_cle = ""
enregistrer = False
with st.sidebar:
    if not mode_serveur:
        with st.expander("Remplacer la clé" if cle_enregistree else "Enregistrer ma clé", expanded=not bool(cle_enregistree)):
            with st.form("connexion", clear_on_submit=True):
                nouvelle_cle = st.text_input("Clé API personnelle", type="password")
                enregistrer = st.form_submit_button("Enregistrer et charger", type="primary")
            st.caption("La clé sera conservée dans le gestionnaire d’identifiants Windows après une connexion réussie.")
    actualiser = st.button("Actualiser les données", key="actualiser_donnees", type="primary", use_container_width=True, disabled=not bool(cle_enregistree))

if LISTE_ID == "901503807906":
    st.error("La liste identifiée appartient à l’ancienne application. Renseignez une liste propre à Constrelec.")
    st.stop()
if not LISTE_ID or not LISTE_ID.isdecimal():
    st.info("Configuration en attente : renseignez CONSTRELEC_CLICKUP_LIST_ID (identifiant numérique de la liste ClickUp de Constrelec).")
    st.stop()

if mode_serveur and not cle_enregistree:
    st.info("Configuration en attente : ajoutez CONSTRELEC_CLICKUP_API_KEY dans les secrets de l’application Streamlit, après avoir configuré son accès privé.")
    st.stop()

# Un chargement au démarrage, puis seulement sur demande (pas à chaque filtre).
demarrage = bool(cle_enregistree) and not st.session_state.get("connexion_initiale_tentee", False)
if enregistrer or actualiser or demarrage:
    st.session_state["connexion_initiale_tentee"] = True
    cle = nouvelle_cle.strip() if enregistrer else cle_enregistree
    if not cle.startswith("pk_") or any(not 33 <= ord(c) <= 126 for c in cle):
        st.error("Colle uniquement ta clé API personnelle ClickUp.")
    else:
        progression = st.empty()
        try:
            with chargement_constrelec("Extraction en cours"):
                nom, taches = charger(cle, progression)
            st.session_state["donnees"] = (nom, taches, datetime.now().strftime("%d/%m/%Y à %H:%M:%S"))
            st.session_state["_donnees_fraiches"] = True
            for k in list(st.session_state):
                if k.startswith("ecriture_incertaine_"):
                    del st.session_state[k]
            if enregistrer:
                try:
                    coffre.set_password(SERVICE_CLE, COMPTE_CLE, cle)
                except Exception:
                    st.warning("Données chargées, mais Windows n'a pas pu mémoriser la clé. Réessaie de l'enregistrer.")
                else:
                    st.rerun()
            chargement_termine = True
        except HTTPError as erreur:
            progression.empty()
            messages = {401: "Clé refusée : vérifiez la configuration de la clé ClickUp.", 403: "Accès refusé à cette liste.",
                        429: "Limite de requêtes atteinte. Réessaie dans une minute."}
            st.error(f"Erreur HTTP {erreur.code} : " + messages.get(erreur.code, "La lecture a échoué."))
            st.caption("Les éventuelles données précédentes restent affichées avec leur date de chargement.")
        except (URLError, TimeoutError, ValueError, ssl.SSLError):
            progression.empty()
            st.error("Lecture impossible : vérifie la connexion et clique sur Actualiser. Les données précédentes ne sont pas actualisées.")

if "donnees" not in st.session_state:
    st.info("À la première utilisation, enregistre ta clé dans le panneau à gauche. Elle sera réutilisée automatiquement aux prochains démarrages.")
    st.stop()

if st.session_state.get("relance_message"):
    st.toast(st.session_state.pop("relance_message"), icon="✅")
# Une session déjà ouverte peut contenir les données d’une ancienne version.
if st.session_state.get('_schema_memoire') != 68:
    nom_liste, anciennes_taches, instant = st.session_state['donnees']
    st.session_state['donnees'] = (nom_liste, [compacter_tache(t) for t in anciennes_taches], instant)
    st.session_state['_definitions_champs'] = {
        i: c for i, champ in st.session_state.get('_definitions_champs', {}).items()
        if (c := compacter_champ(champ)) is not None}
    st.session_state.pop('_portefeuilles_prepares', None)
    st.session_state['_schema_memoire'] = 68
    del anciennes_taches
nom, taches, moment = st.session_state["donnees"]
# Filtre obligatoire avant les calculs, compteurs et exports de tous les onglets.
taches = [t for t in taches if unicodedata.normalize(
    "NFC", str((t.get("status") or {}).get("status", ""))
).strip().casefold() == STATUT_OFFRES.casefold()]
if not taches:
    st.info(f"Aucune tâche non archivée avec le statut « {STATUT_OFFRES} » dans cette liste. Vérifiez le statut choisi dans CONSTRELEC_STATUT_OFFRES.")
    st.stop()
CHANCE_ACTIVE = any(normaliser_nom_champ(champ.get("name", "")) == "chance to win"
                    and champ.get("type") == "drop_down" for t in taches for champ in t.get("custom_fields", []))


@lru_cache(maxsize=8192)
def normaliser(texte):
    return "".join(c for c in unicodedata.normalize("NFKD", str(texte).lower().replace("–", "-").replace("—", "-")) if not unicodedata.combining(c)).strip()


def lire_date(valeur):
    if valeur is None or valeur == "":
        return None
    try:
        return datetime.fromtimestamp(float(valeur) / 1000, ZoneInfo("Europe/Zurich")).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def decoder(champ):
    valeur = champ.get("value")
    if valeur is None:
        return ""
    if champ.get("type") == "drop_down":
        for option in champ.get("type_config", {}).get("options", []):
            if str(valeur) in (str(option.get("id")), str(option.get("orderindex"))):
                return str(option.get("name", valeur))
    return str(valeur)


def montant_chf(champ):
    devise = str(champ.get("type_config", {}).get("currency_type") or champ.get("type_config", {}).get("currency") or "CHF").upper()
    if devise != "CHF":
        return None
    valeur = champ.get("value")
    if valeur is None or valeur == "" or isinstance(valeur, bool):
        return None
    texte = str(valeur).replace("CHF", "").replace("’", "").replace("'", "")
    texte = re.sub(r"\s+", "", texte)
    if "," in texte and "." not in texte:
        texte = texte.replace(",", ".")
    try:
        montant = Decimal(texte)
        return montant if montant.is_finite() and montant >= 0 else None
    except InvalidOperation:
        return None


def argent(valeur):
    return f"{valeur:,.2f}".replace(",", "’") + " CHF"


def lien_sharepoint(tache, champ):
    # Lien SP est la référence prioritaire, jamais un lien supposé à partir du nom client.
    champs_sp = [c for c in tache.get("custom_fields", [])
                 if " ".join(normaliser(c.get("name", "")).split()) in ("lien sp", "a - lien sp")]
    source = champs_sp[0].get("value", "") if len(champs_sp) == 1 else champ.get("value", "")
    if isinstance(source, dict):
        source = source.get("url") or source.get("value") or ""
    if not isinstance(source, str):
        return ""
    liens = []
    for lien in re.findall(r"https://[^\s<>\"']+", source):
        lien = lien.rstrip(").,;")
        u = urlparse(lien)
        if u.hostname and not u.username:
            liens.append(lien)
    liens = list(dict.fromkeys(liens))
    return liens[0] if len(liens) == 1 else ""


def badges_projet(champ):
    valeur = champ.get("value")
    valeurs = valeur if isinstance(valeur, list) else [valeur]
    badges = []
    for option in champ.get("type_config", {}).get("options", []):
        if any(v is not None and str(v) in (str(option.get("id")), str(option.get("orderindex"))) for v in valeurs):
            couleur = str(option.get("color") or "#475569")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", couleur):
                couleur = "#475569"
            r,g,b = [int(couleur[i:i+2],16) for i in (1,3,5)]
            texte = "#101419" if .299*r+.587*g+.114*b > 155 else "#FFFFFF"
            nom = option.get("name") or option.get("label") or "Type non renseigné"
            badges.append('<span class="badge" style="background:'+couleur+';color:'+texte+';margin-left:8px">'+escape(str(nom))+'</span>')
    if not badges:
        badges.append('<span class="badge" style="background:#334155;color:white;margin-left:8px">'+escape(decoder(champ) or "Type non renseigné")+'</span>')
    return "".join(badges)


def action(offre, dates_relances, aujourd, planifiee=None, admin_ao=False):
    numero, prochaine = echeance_planifiee(offre, dates_relances, planifiee, admin_ao=admin_ao)
    if numero is None:
        return "Relances terminées", 0, None
    if prochaine is None:
        return "Aucune date renseignée", 0, None
    retard = max(0, (aujourd - prochaine).days)
    return (f"{numero}e relance à effectuer" if aujourd >= prochaine else "Délai en cours"), retard, prochaine



OPTIONS_TRI_CLIENTS = [
    "Délai : du plus ancien au plus récent",
    "Délai : du plus récent au plus ancien",
    "Montant CHF : du plus élevé au plus faible",
    "Montant CHF : du plus faible au plus élevé",
]

def trier_clients(tableau, choix):
    if choix in OPTIONS_TRI_CLIENTS[2:]:
        return tableau.assign(_tri=tableau["Prix offre CHF"].apply(
            lambda v: v if isinstance(v, Decimal) else None)).sort_values(
                ["_tri", "Retard (jours)", "Projet", "ID"],
                ascending=[choix == OPTIONS_TRI_CLIENTS[3], False, True, True], na_position="last").drop(columns="_tri")
    return tableau.sort_values(["Retard (jours)", "Projet", "ID"],
                               ascending=[choix == OPTIONS_TRI_CLIENTS[1], True, True], na_position="last")


def cinq_prochaines_actions(tableau, jour):
    if tableau.empty:
        return tableau.copy()
    selection = tableau[
        tableau['Prochaine action effective'].apply(lambda d: isinstance(d, date) and pd.notna(d) and d >= jour)
        & tableau['Statut ClickUp'].apply(lambda s: normaliser_nom_champ(s) == normaliser_nom_champ(STATUT_OFFRES))
    ]
    return selection.sort_values(['Prochaine action effective', 'Projet', 'ID']).drop_duplicates('ID').head(5)


def preparer_portefeuilles(taches, aujourdhui):
    champs = {}
    for t in taches:
        for cf in t.get("custom_fields", []):
            champs[cf["id"]] = cf
    aliases = ALIASES_CHAMPS
    champs_par_nom = {}
    for identifiant, champ_definition in champs.items():
        nom_normalise = ' '.join(normaliser(champ_definition.get('name', '')).split())
        champs_par_nom.setdefault(nom_normalise, []).append(identifiant)
    mapping = {}
    for libelle, noms in aliases.items():
        mapping[libelle] = ""
        for nom_champ in noms:
            candidats = champs_par_nom.get(" ".join(normaliser(nom_champ).split()), [])
            if len(candidats) == 1:
                mapping[libelle] = candidats[0]
                break
            if len(candidats) > 1:
                break
    # Les coordonnées et le statut d'attente sont facultatifs ; la date d'envoi
    # et les quatre dates de relance doivent être identifiées.
    manquants = [aliases[k][0] for k in ["Offre envoyée", *LIBELLES_RELANCES] if not mapping[k]]
    lignes = []
    for t in taches:
        cf = {c["id"]: c for c in t.get("custom_fields", [])}
        def champ(n):
            trouve = cf.get(mapping[n], {})
            if trouve.get("value") not in (None, ""):
                return trouve
            if n not in ("Type projet", "SharePoint", "Référence"):
                return trouve
            # Plusieurs champs homonymes peuvent exister sur différentes tâches.
            noms = {" ".join(normaliser(x).split()) for x in aliases[n]}
            candidats = [c for c in cf.values() if " ".join(normaliser(c.get("name", "")).split()) in noms and c.get("value") not in (None, "")]
            if n in ("Type projet", "SharePoint", "Référence") and len(candidats) == 1:
                return candidats[0]
            return trouve
        offre, *dates_relances = [lire_date(champ(n).get("value")) for n in ["Offre envoyée", *LIBELLES_RELANCES]]
        avertissements = []
        for n in ["Offre envoyée", *LIBELLES_RELANCES]:
            v = champ(n).get("value")
            if v not in (None, "") and lire_date(v) is None:
                avertissements.append(n + " : date illisible")
        statut = (t.get("status") or {}).get("status", "Sans statut")
        champ_statut = champ("Statut relance")
        planifiee = lire_date(champ("Prochaine action").get("value"))
        admin_ao = ACTIVER_REGLE_ADMIN_AO and est_admin_ao(t)
        libelle, retard, prochaine = action(offre, dates_relances, aujourdhui, planifiee, admin_ao=admin_ao)
        derniere = max((d for d in dates_relances if isinstance(d, date)), default=None)
        plan_valide = planifiee is not None and (derniere is None or planifiee >= derniere)
        attente = plan_valide and prochaine is not None and prochaine > aujourdhui
        if manquants:
            categorie = "Configuration incomplète"
        elif attente:
            categorie = "En attente"
        elif avertissements or not any([offre, *dates_relances]):
            categorie = "À vérifier"
        elif prochaine_relance(dates_relances) is None:
            categorie = "Relances terminées"
        elif prochaine is not None and prochaine <= aujourdhui:
            categorie = "À relancer"
        else:
            categorie = "Délai en cours"
        responsables = [p.get("username") or p.get("email") or "Sans nom" for p in t.get("assignees", [])] or ["Non assigné"]
        nom_projet = t.get("name", "")
        reference_brute = decoder(champ("Référence")).strip()
        numero = reference_brute if reference_brute else ""
        row = {"_admin_ao": admin_ao, "Délai automatique": 60 if admin_ao else 30,
               "Prochaine action effective": prochaine if plan_valide else None,
               "Chance to win": valeur_chance(t), "Prix offre CHF": montant_chf(champ("Prix offre")),
               "SharePoint": lien_sharepoint(t, champ("SharePoint")),
               "Type projet": decoder(champ("Type projet")), "_badges": badges_projet(champ("Type projet")),
               "Projet": nom_projet, "N° offre": numero, "Responsables": ", ".join(responsables),
               "ID": str(t["id"]), "Statut ClickUp": statut, "Suivi": categorie, "Action": libelle, "Retard (jours)": retard,
               "Offre envoyée": offre, **dict(zip(COLONNES_RELANCES, dates_relances)), "Prochain contrôle": prochaine,
               "Niveau relance": prochaine_relance(dates_relances), "Prochaine action": planifiee,
               "E-mail client": decoder(champ("E-mail client")), "Téléphone": decoder(champ("Téléphone")),
               "Statut relance": decoder(champ_statut), "À vérifier": "; ".join(avertissements),
               "Lien": t.get("url", ""), "Type": "Sous-tâche" if t.get("parent") else "Tâche", "_responsables": responsables, "_ids": [str(p["id"]) for p in t.get("assignees", []) if p.get("id") is not None]}
        lignes.append(row)
    df = pd.DataFrame(lignes)
    return df, mapping, manquants

def clients_relances(taches, vendeur, ids_vendeur, aujourdhui):
    # Calcul uniquement à l’ouverture : pas de nouveau cache ni d’appel ClickUp.
    ids = set(str(i) for i in ids_vendeur)
    retenues = []
    for t in taches:
        affectations = {str(p['id']) for p in t.get('assignees', []) if p.get('id') is not None}
        inclus = (not t.get('assignees') if vendeur == 'Admin' else
                  bool(ids.intersection(affectations)) or (vendeur == 'Portefeuille global' and not t.get('assignees')))
        if inclus:
            retenues.append(t)
    if not retenues:
        return pd.DataFrame()
    tableau, _, _ = preparer_portefeuilles(retenues, aujourdhui)
    debut = DEBUT_RELANCES
    tableau = tableau[tableau['Offre envoyée'].apply(lambda d: isinstance(d, date) and debut <= d <= aujourdhui)].copy()
    tableau['Dernière relance'] = tableau.apply(lambda r: max(
        [d for d in (r[c] for c in COLONNES_RELANCES) if isinstance(d, date) and debut <= d <= aujourdhui],
        default=None), axis=1) if not tableau.empty else pd.Series(dtype=object)
    return tableau[tableau['Dernière relance'].notna()].drop_duplicates('ID').sort_values('Dernière relance', ascending=False)


@st.dialog("Clients relancés", width="large")
def afficher_clients_relances(vendeur, ids_vendeur, aujourdhui):
    tableau = clients_relances(st.session_state['donnees'][1], vendeur, ids_vendeur, aujourdhui)
    st.caption(libelle_vendeur(vendeur) + ' · Offres envoyées depuis le ' + DEBUT_RELANCES.strftime('%d.%m.%Y') + ', y compris celles ayant changé de statut.')
    periode = st.selectbox('Période des relances', ['Depuis le ' + DEBUT_RELANCES.strftime('%d.%m.%Y'), '30 derniers jours'], key='periode_clients_relances')
    if not tableau.empty and periode == '30 derniers jours':
        tableau = tableau[tableau['Dernière relance'].apply(lambda d: aujourdhui - timedelta(days=29) <= d <= aujourdhui)]
    if tableau.empty:
        st.info('Aucun client relancé pour ce portefeuille sur cette période.')
    else:
        st.caption(str(len(tableau)) + ' client(s) · Une ligne par projet')
        colonnes = ['Projet', 'N° offre', 'Responsables', *COLONNES_RELANCES, 'Statut ClickUp', 'Lien']
        affichage = tableau[colonnes].copy()
        for colonne in COLONNES_RELANCES:
            affichage[colonne] = affichage[colonne].apply(lambda d: d.strftime('%d/%m/%Y') if isinstance(d, date) else '—')
        st.dataframe(affichage, hide_index=True, use_container_width=True,
                     column_config={'Lien': st.column_config.LinkColumn('ClickUp', display_text='Ouvrir')})
    if st.button('Fermer', key='fermer_clients_relances', use_container_width=True):
        st.rerun()


if st.session_state.pop('_donnees_fraiches', False):
    reconcilier_relances(st.session_state["donnees"][1])
aujourdhui = aujourd_hui()
source_actuelle = st.session_state["donnees"]
cache_portefeuilles = st.session_state.get("_portefeuilles_prepares")
if (not cache_portefeuilles or cache_portefeuilles.get("version") != 91
        or cache_portefeuilles["source"] is not source_actuelle
        or cache_portefeuilles["jour"] != aujourdhui):
    df, mapping, manquants = preparer_portefeuilles(taches, aujourdhui)
    cache_portefeuilles = {"version": 91, "source": source_actuelle, "jour": aujourdhui,
                          "resultat": (df, mapping, manquants), "historiques": {}}
    st.session_state["_portefeuilles_prepares"] = cache_portefeuilles
else:
    df, mapping, manquants = cache_portefeuilles["resultat"]
if globals().get("chargement_termine"):
    # Ne pas laisser la photo et la barre de chargement sur la page principale.
    progression.empty()

# La page normale reste identique à la V5 après la synchronisation ClickUp.
st.markdown('<div class="hero"><div><div class="eyebrow">PORTEFEUILLE COMMERCIAL</div><h1>' + escape(libelle_vendeur(vendeur_titre) or "Relances clients") + '</h1><p>Vos clients. Vos projets. Vos prochaines relances.</p></div></div>', unsafe_allow_html=True)

# Les identifiants ClickUp évitent de mélanger deux vendeurs portant le même nom.
personnes = {}
for t in taches:
    for p in t.get("assignees", []):
        if p.get("id") is not None:
            personnes[str(p["id"])] = p
correspondances = {}
for identifiant, personne in sorted(personnes.items(), key=lambda item: (str(item[1].get("username") or item[1].get("email") or "").casefold(), item[0])):
    nom = str(personne.get("username") or personne.get("email") or f"Compte {identifiant}").strip()
    if not nom:
        nom = f"Compte {identifiant}"
    if nom in ("Admin", "Portefeuille global") or nom in correspondances:
        nom += f" (ID {identifiant})"
    correspondances[nom] = [identifiant]
VENDEURS = list(correspondances)
COULEURS_GRAPHIQUE.clear()
COULEURS_GRAPHIQUE.update({nom: PALETTE_EQUIPE[i % len(PALETTE_EQUIPE)] for i, nom in enumerate(VENDEURS)})
COULEURS_GRAPHIQUE["Admin"] = "#9299A3"

def nom_vendeur(nom):
    return libelle_vendeur(nom) if nom else "Choisir votre nom…"


with st.sidebar:
    st.markdown('<h3 class="titre-portefeuilles">Portefeuille Constrelec</h3>', unsafe_allow_html=True)
    st.button("Portefeuille global", key="choisir_global", use_container_width=True, type="primary",
              on_click=lambda: st.session_state.update(vendeur_actif="Portefeuille global"))
    vendeur = "Portefeuille global"
if manquants:
    st.error("Certains champs ClickUp ne sont pas identifiés : " + ", ".join(manquants) + ". Envoyez une capture de ce message pour corriger la détection.")
    st.stop()
for id_action, action_restante in list(st.session_state.get('_actions_a_enregistrer', {}).items()):
    st.warning('Prochaine action à confirmer pour ' + action_restante['projet'] + ' : ' + action_restante['jour'].strftime('%d.%m.%Y'))
    if st.button('Réessayer la prochaine action', key='reprendre_action_' + id_action):
        try:
            actualiser_tache_session(enregistrer_prochaine_action(id_action, cle_actions_personnelles(), action_restante['jour']))
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, ValueError) as erreur:
            st.error(str(erreur))
        else:
            st.session_state['_actions_a_enregistrer'].pop(id_action, None)
            st.session_state.pop('_relance_dialogue_terminee_' + id_action, None)
            actualiser_sans_deplacer()

with st.container(key="vue_portefeuille_" + vendeur):
    global_actif = vendeur == "Portefeuille global"
    if global_actif:
        ids_vendeur = [ids[0] for ids in correspondances.values() if len(ids) == 1]
        ambigus = [nom for nom, ids in correspondances.items() if len(ids) > 1]
        if ambigus:
            st.warning("Comptes ambigus exclus : " + ", ".join(ambigus))
    elif vendeur == "Admin":
        ids_vendeur = []
    elif vendeur == "Mes relances":
        compte = (st.session_state.get('_clickup_personnel') or {}).get('utilisateur') or {}
        if not compte.get('id'):
            st.info('Synchronisez votre compte ClickUp pour voir les offres qui vous sont attribuées.')
            st.stop()
        ids_vendeur = [str(compte['id'])]
    else:
        st.error('Portefeuille non reconnu. Sélectionnez un portefeuille valide.')
        st.stop()
    if global_actif:
        vue_vendeur = df[df.apply(lambda r: r["_responsables"] == ["Non assigné"] or bool(set(ids_vendeur).intersection(r["_ids"])), axis=1)]
    elif vendeur == "Admin":
        vue_vendeur = df[df["_responsables"].apply(lambda noms: noms == ["Non assigné"])]
    else:
        vue_vendeur = df[df["_ids"].apply(lambda vs: bool(set(ids_vendeur).intersection(vs)))]
    DEBUT = DEBUT_RELANCES
    vue = vue_vendeur[vue_vendeur["Offre envoyée"].apply(lambda d: isinstance(d, date) and DEBUT <= d <= aujourdhui)]
    vue = vue.copy()
    # Aucun filtre par type de projet dans Constrelec.
    relances = vue[vue["Suivi"] == "À relancer"].sort_values("Retard (jours)", ascending=False)
    couleur = "#E22323"
    st.markdown('<style>.offer{border-left-color:' + couleur + '}[data-testid="stMetricValue"]{color:' + couleur + '}.step{color:' + couleur + '}</style>', unsafe_allow_html=True)
    a,b,c = st.columns([1,1,1.5])
    a.metric("Offres en cours", len(vue))
    b.metric("Relances à effectuer", len(relances))
    montants = [v for v in relances["Prix offre CHF"] if isinstance(v, Decimal)]
    c.metric("Montant à relancer", argent(sum(montants, Decimal("0"))))
    d,e = st.columns(2)
    d.metric("Retard maximal", str(int(relances["Retard (jours)"].max())) + " j" if not relances.empty else "—")
    debut_30j = aujourdhui - timedelta(days=29)
    nb_relances_30j = sum(1 for _, projet in vue.iterrows() for colonne in COLONNES_RELANCES
                         if isinstance(projet[colonne], date) and debut_30j <= projet[colonne] <= aujourdhui)
    with e:
        with st.container(key="historique_relances_action"):
            st.metric("Relances effectuées · 30 jours", nb_relances_30j)
            if st.button("Consulter les clients relancés", key="ouvrir_clients_relances",
                         use_container_width=True):
                afficher_clients_relances(vendeur, ids_vendeur, aujourdhui)

    # Historique des dates enregistrées, y compris les offres qui ont changé de statut.
    cle_historique = (vendeur, tuple(ids_vendeur))
    if cle_historique not in cache_portefeuilles["historiques"]:
        historiques = []
        for t in st.session_state["donnees"][1]:
            assignes = {str(p["id"]) for p in t.get("assignees", []) if p.get("id") is not None}
            sans_assignation = not t.get("assignees", [])
            inclus = (sans_assignation if vendeur == "Admin" else
                      bool(assignes.intersection(ids_vendeur)) or (global_actif and sans_assignation))
            if not inclus:
                continue
            champs_hist = {c["id"]: c for c in t.get("custom_fields", [])}
            offre_hist = lire_date(champs_hist.get(mapping["Offre envoyée"], {}).get("value"))
            if not offre_hist or not DEBUT <= offre_hist <= aujourdhui:
                continue
            for etape in LIBELLES_RELANCES:
                jour = lire_date(champs_hist.get(mapping[etape], {}).get("value"))
                if jour and DEBUT <= jour <= aujourdhui:
                    historiques.append({"ID": str(t["id"]), "Étape": etape, "Date": jour,
                        "Prix": montant_chf(champs_hist.get(mapping["Prix offre"], {})),
                        "Vendeurs": (["Mes relances"] if vendeur == "Mes relances" else
                                     ["Admin"] if sans_assignation else
                                     [nom_v for nom_v, ids_v in correspondances.items()
                                      if len(ids_v) == 1 and ids_v[0] in assignes and ids_v[0] in ids_vendeur])})

        # Deux vues récentes suffisent ; une vue évincée sera recalculée à l’identique.
        historique_cache = cache_portefeuilles["historiques"]
        while len(historique_cache) >= 2:
            del historique_cache[next(iter(historique_cache))]
        historique_cache[cle_historique] = historiques
    else:
        historiques = cache_portefeuilles["historiques"][cle_historique]

    st.subheader("Montant des offres relancées par semaine")
    premier_lundi = DEBUT - timedelta(days=DEBUT.weekday())
    dernier_lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
    vendeurs_graph = list(COULEURS_GRAPHIQUE) if global_actif else [vendeur]
    couleurs_graph = {**COULEURS_GRAPHIQUE, "Mes relances": "#E22323"}
    mois_courts = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
    semaines, ordre_semaines = {}, []
    jour = premier_lundi
    while jour <= dernier_lundi:
        libelle = f"S{jour.isocalendar().week:02d} · {mois_courts[(jour + timedelta(days=3)).month-1]}"
        ordre_semaines.append(libelle)
        for nom_v in vendeurs_graph:
            semaines[(jour, nom_v)] = {"Semaine": libelle, "Début": jour.isoformat(),
                                      "Vendeur": nom_v, "Montant CHF": 0.0}
        jour += timedelta(days=7)
    evenements_vus = set()
    sans_prix_historique = 0
    partages = False
    for evenement in historiques:
        identite = (evenement["ID"], evenement["Étape"])
        if identite in evenements_vus:
            continue
        evenements_vus.add(identite)
        lundi = evenement["Date"] - timedelta(days=evenement["Date"].weekday())
        noms = evenement["Vendeurs"]
        if not noms:
            continue
        if isinstance(evenement["Prix"], Decimal):
            partages = partages or len(noms) > 1
            part = float(evenement["Prix"]) / len(noms)
            for nom_v in noms:
                semaines[(lundi, nom_v)]["Montant CHF"] += part
        else:
            sans_prix_historique += 1
    st.markdown('<div style="font-size:12px;color:#CBD0D8;margin-bottom:2px">Montant relancé (CHF)</div>', unsafe_allow_html=True)
    st.vega_lite_chart(pd.DataFrame(list(semaines.values())), {
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": "Semaine", "type": "ordinal", "sort": ordre_semaines, "title": None,
                  "axis": {"labelAngle": -60, "labelOverlap": False, "labelFontSize": 10}},
            "y": {"field": "Montant CHF", "type": "quantitative", "stack": "zero", "title": None},
            "color": {"field": "Vendeur", "type": "nominal",
                      "scale": {"domain": vendeurs_graph, "range": [couleurs_graph[n] for n in vendeurs_graph]},
                      "legend": None},
            "tooltip": [{"field": "Semaine", "type": "nominal"},
                        {"field": "Vendeur", "type": "nominal"},
                        {"field": "Montant CHF", "type": "quantitative", "format": ",.2f"}]},
        "height": 260, "background": "transparent",
        "config": {"view": {"stroke": None, "fill": "transparent"}, "legend": {"labelColor": "#CBD0D8"},
                   "axis": {"gridColor": "#33343A", "labelColor": "#CBD0D8", "titleColor": "#CBD0D8"}}
    }, theme=None, use_container_width=True, key="historique_" + vendeur)
    legende = "".join(
        '<span style="display:inline-flex;align-items:center;gap:6px;flex-shrink:0">'
        '<span style="width:9px;height:9px;border-radius:2px;background:' + couleurs_graph[nom_v] + '"></span>'
        + escape(libelle_vendeur(nom_v)) + '</span>' for nom_v in vendeurs_graph)
    st.markdown('<div style="display:flex;justify-content:flex-start;gap:16px;flex-wrap:nowrap;overflow-x:auto;'
                'font-size:12px;color:#CBD0D8;margin-top:-10px;margin-bottom:10px;padding:0 0 4px">' + legende + '</div>',
                unsafe_allow_html=True)
    if global_actif and partages:
        st.caption("Projets partagés : montant réparti à parts égales entre les vendeurs assignés, sans double comptage.")
    if sans_prix_historique:
        st.caption(f"{sans_prix_historique} relance(s) sans montant CHF : exclues des montants du graphique.")

    if global_actif:
        st.subheader("Priorités de relance")
        config_kpi = {"background": "transparent", "view": {"stroke": None, "fill": "transparent"}, "axis": {"labelColor": "#CBD0D8", "titleColor": "#CBD0D8", "gridColor": "#33343A"}}
        charges = []
        for nom_k, ids_k in {**correspondances, "Admin": []}.items():
            nb = sum(1 for _, r_k in relances.iterrows() if
                     (r_k["_responsables"] == ["Non assigné"] if nom_k == "Admin" else len(ids_k) == 1 and ids_k[0] in r_k["_ids"]))
            charges.append({"Vendeur": libelle_vendeur(nom_k), "Relances": nb})
        gauche_kpi, droite_kpi = st.columns(2)
        with gauche_kpi:
            st.markdown("**Relances à effectuer par vendeur**")
            st.vega_lite_chart(pd.DataFrame(charges), {
                "mark": {"type": "bar", "cornerRadiusEnd": 4}, "height": 280,
                "encoding": {"y": {"field": "Vendeur", "type": "nominal", "sort": "-x", "title": None},
                             "x": {"field": "Relances", "type": "quantitative", "title": "Nombre de relances", "axis": {"tickMinStep": 1}},
                             "color": {"field": "Vendeur", "type": "nominal", "scale": {"domain": [libelle_vendeur(n) for n in COULEURS_GRAPHIQUE], "range": list(COULEURS_GRAPHIQUE.values())}, "legend": None},
                             "tooltip": [{"field": "Vendeur"}, {"field": "Relances"}]},
                "background": "transparent", "config": config_kpi}, theme=None, use_container_width=True)
        with droite_kpi:
            st.markdown("**Ancienneté des relances en retard**")
            tranches = [("0–7 jours", 0, 7), ("8–21 jours", 8, 21), ("22–60 jours", 22, 60), ("Plus de 60 jours", 61, float("inf"))]
            ages = [{"Retard": n, "Offres": int(relances["Retard (jours)"].between(lo, hi).sum())} for n,lo,hi in tranches]
            st.vega_lite_chart(pd.DataFrame(ages), {
                "mark": {"type": "bar", "color": "#E22323", "cornerRadiusEnd": 4}, "height": 280,
                "encoding": {"x": {"field": "Retard", "type": "ordinal", "sort": [x[0] for x in tranches], "axis": {"labelAngle": 0}, "title": None},
                             "y": {"field": "Offres", "type": "quantitative", "title": "Nombre d’offres", "axis": {"tickMinStep": 1}},
                             "tooltip": [{"field": "Retard"}, {"field": "Offres"}]}, "background": "transparent", "config": config_kpi}, theme=None, use_container_width=True)
        st.caption("La première relance est proposée à J+15, puis les suivantes à J+30 après la dernière relance. Une offre partagée compte pour chaque vendeur concerné dans le graphique par vendeur.")
        carte_portefeuille(vue, st.session_state["donnees"][1], correspondances)
        st.subheader("Vue d’ensemble des vendeurs")
        synthese = []
        for nom_v, ids in {**correspondances, "Admin": []}.items():
            groupe = vue[vue["_responsables"].apply(lambda ns: ns == ["Non assigné"])] if nom_v == "Admin" else vue[vue["_ids"].apply(lambda vs: len(ids) == 1 and ids[0] in vs)]
            dues = groupe[groupe["Suivi"] == "À relancer"]
            synthese.append({"Vendeur": nom_v, "Offres en cours": len(groupe),
                "Relances à effectuer": len(dues),
                "Montant à relancer CHF": float(sum((v for v in dues["Prix offre CHF"] if isinstance(v, Decimal)), Decimal("0"))),
                "Relances · 30 jours": sum(1 for _, r in groupe.iterrows() for c in COLONNES_RELANCES
                                          if isinstance(r[c], date) and debut_30j <= r[c] <= aujourdhui)})
        entetes = ["Vendeur", "Offres en cours", "Relances à faire", "Montant à relancer", "Relances · 30 jours"]
        lignes_tableau = []
        for ligne in synthese:
            cellules = [escape(libelle_vendeur(ligne["Vendeur"])), str(ligne["Offres en cours"]),
                        str(ligne["Relances à effectuer"]),
                        argent(Decimal(str(ligne["Montant à relancer CHF"]))),
                        str(ligne["Relances · 30 jours"])]
            lignes_tableau.append("<tr>" + "".join("<td>" + c + "</td>" for c in cellules) + "</tr>")
        st.markdown('<div class="tableau-global"><table><thead><tr>' +
                    "".join("<th>" + escape(c) + "</th>" for c in entetes) +
                    "</tr></thead><tbody>" + "".join(lignes_tableau) +
                    "</tbody></table></div>", unsafe_allow_html=True)
        st.caption("Les totaux globaux comptent chaque projet une fois. Un projet partagé apparaît dans la ligne de chaque vendeur assigné.")

    if len(montants) < len(relances):
        st.warning(f"Total partiel : {len(relances) - len(montants)} offre(s) à relancer sans prix CHF exploitable. Aucun montant n’est estimé.")
    nb_verifier = int((vue["Suivi"] == "À vérifier").sum())
    if nb_verifier:
        st.warning(f"{nb_verifier} offre(s) ont des dates illisibles et ne sont pas incluses dans les relances.")
    if relances.empty:
        st.success("Aucune relance à effectuer aujourd’hui sur ce portefeuille.")

    prochaines_actions = cinq_prochaines_actions(vue_vendeur, aujourdhui)
    lignes_agenda = []
    for _, projet_a_venir in prochaines_actions.iterrows():
        montant_a_venir = projet_a_venir['Prix offre CHF']
        prix_a_venir = argent(montant_a_venir) if isinstance(montant_a_venir, Decimal) else 'Prix non renseigné'
        lignes_agenda.append(
            '<div class="agenda-ligne"><span class="agenda-projet">' + escape(str(projet_a_venir['Projet']))
            + '</span><span class="agenda-prix">' + escape(prix_a_venir)
            + '</span><time>' + projet_a_venir['Prochaine action effective'].strftime('%d.%m.%Y') + '</time></div>')
    st.markdown('<section class="agenda-actions"><h3>Les 5 prochaines actions</h3>'
                + (''.join(lignes_agenda) if lignes_agenda else '<p>Aucune prochaine action planifiée à partir d’aujourd’hui.</p>')
                + '</section>', unsafe_allow_html=True)

    st.subheader("Vos clients à recontacter")
    st.markdown("**Filtrer**")
    with st.container(key="filtres_clients"):
        filtre_etat_col, filtre_niveau_col = st.columns(2, gap="medium")
        with filtre_etat_col:
            etat_selectionne = st.selectbox("État relance", ["Actif", "En attente"], key="etat_clients_" + vendeur)
        with filtre_niveau_col:
            filtre_relance = st.selectbox("Niveau de relance", ["Tous les niveaux", *COLONNES_RELANCES],
                                         key="niveau_clients_" + vendeur)
    afficher_attente = etat_selectionne == "En attente"
    relances = vue[vue["Suivi"] == ("En attente" if afficher_attente else "À relancer")].sort_values("Retard (jours)", ascending=False)
    if filtre_relance in COLONNES_RELANCES:
        relances = relances[relances["Niveau relance"] == COLONNES_RELANCES.index(filtre_relance) + 1]
    tri_cle = "tri_" + vendeur
    ancien_tri = st.session_state.get(tri_cle)
    if ancien_tri not in OPTIONS_TRI_CLIENTS:
        st.session_state[tri_cle] = OPTIONS_TRI_CLIENTS[2] if ancien_tri == "Montant CHF" else OPTIONS_TRI_CLIENTS[0]
    st.selectbox("Trier", OPTIONS_TRI_CLIENTS, key=tri_cle)
    relances = trier_clients(relances, st.session_state[tri_cle])
    recherche = st.text_input("Rechercher dans les relances", key="recherche_"+vendeur, placeholder="Projet, numéro d’offre ou e-mail…")
    visibles = relances
    if recherche:
        visibles = visibles[visibles[["Projet", "N° offre", "E-mail client"]].fillna("").agg(" ".join,axis=1).str.contains(recherche,case=False,regex=False)]
    if visibles.empty:
        st.info("Aucun client en attente ne correspond aux filtres sélectionnés." if afficher_attente else "Aucune relance ne correspond aux filtres sélectionnés.")
        st.stop()

    def derniere_etape(r):
        etapes = [(r[c], i, titre) for i,(c,titre) in enumerate([
            ("Offre envoyée", "Offre envoyée"), *zip(COLONNES_RELANCES, LIBELLES_RELANCES)])
            if isinstance(r[c], date)]
        if not etapes:
            return "Date non renseignée"
        dt, _, titre = max(etapes, key=lambda e: (e[0],e[1]))
        return titre + " · " + dt.strftime("%d/%m/%Y")


    def preparer_message(r):
        sujet = "Constrelec SA – Suivi de votre offre"
        corps = "\n\n".join([
            "Bonjour,",
            "Nous revenons vers vous concernant notre offre pour votre projet de tableaux et armoires électriques.",
            "Avez-vous eu l’occasion d’en prendre connaissance ?",
            "Si certains éléments nécessitent des précisions ou des adaptations, nous restons volontiers à votre disposition pour en discuter.",
            "Un simple retour de votre part nous permettra de savoir comment poursuivre avec votre projet.",
            "Dans l’attente de votre retour, nous vous souhaitons une excellente journée.",
            "Cordialement,",
        ])
        return sujet, corps

    def lien_outlook_web(destinataire, sujet, corps):
        # Lien ouvert côté navigateur : le compte connecté du vendeur est utilisé.
        return "https://outlook.office.com/mail/deeplink/compose?" + urlencode(
            {"to": destinataire, "subject": sujet, "body": corps}, quote_via=quote)


    def cible_relance(*dates):
        numero = prochaine_relance(dates)
        return LIBELLES_RELANCES[numero - 1] if numero else None


    def remplacer_tache_locale(tache):
        nom_liste, anciennes, instant = st.session_state["donnees"]
        st.session_state["donnees"] = (nom_liste,
            [tache if str(t.get("id")) == str(tache.get("id")) else t for t in anciennes], instant)


    def enregistrer_relance(r, cle, choix=None):
        # Relire avant d'écrire : ne pas écraser une date ajoutée par un collègue.
        frais = lire_tache(r["ID"], cle)
        champs_frais = {c["id"]: c for c in frais.get("custom_fields", [])}
        cles = ["Offre envoyée", *LIBELLES_RELANCES]
        dates = []
        for libelle in cles:
            c = champs_frais.get(mapping[libelle])
            if not c or c.get("type") != "date":
                raise ValueError("Le champ de date ClickUp est absent ou incompatible. Actualisez les données.")
            valeur = c.get("value")
            d = lire_date(valeur)
            if valeur not in (None, "") and d is None:
                raise ValueError("Une date ClickUp est illisible : aucun enregistrement effectué.")
            dates.append(d)
        remplacer_tache_locale(frais)
        if dates != [r["Offre envoyée"], *(r[c] for c in COLONNES_RELANCES)]:
            raise ValueError("Les dates ont changé dans ClickUp. Actualisez le portefeuille avant de continuer.")
        if normaliser((frais.get("status") or {}).get("status", "")) != normaliser_nom_champ(STATUT_OFFRES):
            raise ValueError("Le statut de l’offre a changé. Actualisez le portefeuille.")
        cible = cible_relance(*dates[1:])
        if cible == LIBELLES_RELANCES[-1]:
            # Dernière relance : aucune prochaine action à programmer.
            # Le champ peut être absent sans empêcher l'enregistrement.
            candidats = [c for c in frais.get('custom_fields', [])
                         if normaliser_nom_champ(c.get('name', '')) in
                         {normaliser_nom_champ(n) for n in ALIASES_CHAMPS['Prochaine action']}
                         and c.get('type') == 'date']
            c_plan = candidats[0] if len(candidats) == 1 else None
        else:
            c_plan = champ_prochaine_action(frais)
        plan_frais = lire_date(c_plan.get('value')) if c_plan else None
        if plan_frais != r['Prochaine action']:
            raise ValueError('La prochaine relance a changé dans ClickUp. Actualisez avant de continuer.')
        if (ACTIVER_REGLE_ADMIN_AO and est_admin_ao(frais)) != bool(r['_admin_ao']):
            raise ValueError('Les informations du projet ont changé. Actualisez avant de continuer.')
        echeance = action(dates[0], dates[1:], aujourd_hui(), plan_frais, admin_ao=ACTIVER_REGLE_ADMIN_AO and est_admin_ao(frais))[2]
        programme = plan_frais is not None and echeance == plan_frais
        if not cible or echeance is None or (echeance > aujourd_hui() and not programme):
            raise ValueError('Aucune relance à enregistrer aujourd’hui.')
        est_en_attente = False  # L’attente est désormais calculée à partir de la date.
        # Les boutons ne préparent qu'un choix local. L'écriture commence ici,
        # après les contrôles de dates/statut et avant celle de la relance.
        if CHANCE_ACTIVE and choix is not None:
            if choix not in CHOIX_CHANCE:
                raise ValueError("Sélectionnez une chance de réussite avant de valider la relance.")
            if valeur_chance(frais) != choix:
                frais = modifier_chance_clickup(r["ID"], cle, choix)
                remplacer_tache_locale(frais)
                # Recontrôler les dates et l'attente après l'écriture du choix :
                # un collègue peut avoir relancé le client entre les deux appels.
                return enregistrer_relance(r, cle)
        if CHANCE_ACTIVE and not valeur_chance(frais):
            raise ValueError('Sélectionnez Inconnue, Faible, Moyenne ou Élevée dans « Chance to win » avant de valider la relance.')
        field_id = mapping[cible]
        # Marqueur conservé en cas de délai réseau : aucune répétition automatique du POST.
        st.session_state["ecriture_incertaine_" + r["ID"]] = True
        st.session_state.setdefault('_relances_a_verifier', {})[r['ID']] = {
            'champ': field_id, 'jour': aujourd_hui(), 'attente': est_en_attente,
            'champ_attente': mapping.get('Statut relance')}
        try:
            ecrire_date_relance(r["ID"], field_id, cle)
        except (URLError, TimeoutError, ssl.SSLError):
            # Un POST interrompu peut avoir été reçu : vérifier, jamais le répéter.
            verification = lire_tache(r["ID"], cle)
            retour = next((c for c in verification.get('custom_fields', []) if c.get('id') == field_id), {})
            if lire_date(retour.get('value')) != aujourd_hui():
                raise ValueError('Enregistrement non confirmé. Actualisez les données avant de réessayer.')
        else:
            verification = lire_tache(r["ID"], cle)
        retour = next((c for c in verification.get("custom_fields", []) if c.get("id") == field_id), {})
        if lire_date(retour.get("value")) != aujourd_hui():
            raise ValueError("Enregistrement non confirmé par la relecture ClickUp. Cliquez sur Actualiser avant toute nouvelle tentative.")
        remplacer_tache_locale(verification)
        st.session_state.pop("choix_chance_local_" + r["ID"], None)
        st.session_state.pop("ecriture_incertaine_" + r["ID"], None)
        st.session_state.get("_relances_a_verifier", {}).pop(r["ID"], None)
        st.session_state["relance_message"] = cible + " enregistrée dans ClickUp le " + aujourd_hui().strftime("%d/%m/%Y") + " · " + r["Projet"]


    taille_lot = 10
    cle_limite = "limite_clients_" + vendeur
    contexte_clients = (vendeur, afficher_attente, filtre_relance, recherche, st.session_state[tri_cle])
    if st.session_state.get("_contexte_clients") != contexte_clients:
        st.session_state[cle_limite] = taille_lot
        st.session_state["_contexte_clients"] = contexte_clients
    limite_clients = st.session_state.get(cle_limite, taille_lot)
    nombre_affiche = min(limite_clients, len(visibles))
    def afficher_plus_clients():
        st.session_state[cle_limite] = limite_clients + taille_lot

    st.caption(f"{nombre_affiche} clients affichés sur {len(visibles)}")
    for _, r in visibles.iloc[:nombre_affiche].iterrows():
        email = r["E-mail client"].strip()
        telephone = r["Téléphone"] or "Téléphone non renseigné"
        with st.container(key="fiche_"+r["ID"]):
            fiche_infos, fiche_validation = st.columns([3, 1], vertical_alignment="center")
            with fiche_infos:
                st.markdown('<article class="offer-content"><span class="badge">' + escape("En attente" if r["Suivi"] == "En attente" else r["Action"]) + ' · ' + str(r["Retard (jours)"]) + ' j de retard</span><h3>' + escape(r["Projet"]) + '</h3><div class="details">' + '<div><span class="label">Référence projet</span>' + escape(r['N° offre'] or 'Non renseigné') + '</div><div class="offer-price"><span class="label">Prix de l’offre</span>' + (argent(r['Prix offre CHF']) if isinstance(r['Prix offre CHF'], Decimal) else 'Non renseigné en CHF') + '</div><div><span class="label">Dernière étape</span>' + escape(derniere_etape(r)) + '</div><div><span class="label">E-mail client</span>' + escape(email or "À compléter dans le message") + '</div><div><span class="label">Téléphone</span>' + escape(telephone) + '</div></div></article>', unsafe_allow_html=True)
            with fiche_validation:
                choix_selectionne = st.session_state.get("choix_chance_local_" + r["ID"], r["Chance to win"])
                if CHANCE_ACTIVE:
                    with st.container(key="chance_" + r["ID"]):
                        st.markdown('<span class="chance-label">Chance to win :</span>', unsafe_allow_html=True)
                        inconnue, faible, moyenne, elevee = st.columns([1.2, 1, 1.2, 1], gap="small", vertical_alignment="center")
                        for choix, colonne in zip(CHOIX_CHANCE, (inconnue, faible, moyenne, elevee)):
                            with colonne:
                                st.button(choix, key="chance_" + normaliser(choix) + "_" + r["ID"],
                                          type="primary" if choix_selectionne == choix else "secondary",
                                          disabled=not compte_personnel_disponible(), use_container_width=True,
                                          on_click=selectionner_chance_locale, args=(r["ID"], choix))
                with st.container(key="validation_"+r["ID"]):
                    cible = cible_relance(*(r[c] for c in COLONNES_RELANCES))
                    if cible:
                        numero = ("1re" if cible == LIBELLES_RELANCES[0] else str(LIBELLES_RELANCES.index(cible) + 1) + "e") if cible else ""
                        libelle_validation = f"✓ {numero} relance effectuée" if cible else "✓ Relance effectuée · retirer l’attente"
                        incertain = st.session_state.get("ecriture_incertaine_" + r["ID"], False)
                        if st.button(libelle_validation, key="fait_"+vendeur+"_"+r["ID"], disabled=(CHANCE_ACTIVE and bool(cible) and not choix_selectionne) or incertain or not compte_personnel_disponible(), use_container_width=True, help="Choisissez d’abord une chance de réussite, puis enregistrez la relance sous votre compte ClickUp."):
                            confirmer_relance_et_date(r, choix_selectionne or None, enregistrer_relance)
                        if incertain:
                            st.warning("Enregistrement à vérifier : cliquez sur Actualiser les données avant de réessayer.")
                    else:
                        st.caption("Les quatre relances ont déjà été effectuées. Ce projet ne sera plus proposé à la relance.")
                with st.container(key="attente_action_" + r["ID"]):
                    if st.button("Mettre en attente", key="attendre_" + r["ID"], use_container_width=True,
                                 disabled=not compte_personnel_disponible() or st.session_state.get("ecriture_incertaine_" + r["ID"], False) or r["ID"] in st.session_state.get('_actions_a_enregistrer', {}),
                                 help="Choisir une date de reprise du suivi sans enregistrer de relance effectuée."):
                        confirmer_mise_en_attente(r)
                with st.container(key="offre_perdue_" + r["ID"]):
                    if st.button("Offre perdue", key="perdre_" + r["ID"], use_container_width=True,
                                 disabled=not compte_personnel_disponible()):
                        confirmer_offre_perdue(r["ID"], r["Projet"])
                if isinstance(r["Prochaine action effective"], date):
                    st.caption("Prochaine relance : " + r["Prochaine action effective"].strftime("%d.%m.%Y"))
            cle_commentaire = "texte_commentaire_" + vendeur + "_" + r["ID"]
            commentaire_bloque = st.session_state.get("commentaire_incertain_" + r["ID"], False)
            with st.form("form_commentaire_" + vendeur + "_" + r["ID"],
                         clear_on_submit=False, enter_to_submit=True, border=False):
                zone_texte, zone_envoi = st.columns([5, 1], vertical_alignment="center")
                with zone_texte:
                    st.text_input("Commentaire client", key=cle_commentaire, label_visibility="collapsed",
                                  placeholder="Saisir un commentaire · Entrée pour publier dans ClickUp",
                                  disabled=commentaire_bloque or not compte_personnel_disponible())
                with zone_envoi:
                    st.form_submit_button("Publier", disabled=commentaire_bloque or not compte_personnel_disponible(),
                                          on_click=soumettre_commentaire, args=(r["ID"], cle_commentaire),
                                          use_container_width=True)
            retour_commentaire = st.session_state.get("retour_commentaire_" + r["ID"])
            if retour_commentaire:
                getattr(st, retour_commentaire[0])(retour_commentaire[1])
            if commentaire_bloque:
                if st.button("J’ai vérifié dans ClickUp — débloquer la saisie", key="debloquer_commentaire_"+vendeur+"_"+r["ID"]):
                    st.session_state.pop("commentaire_incertain_" + r["ID"], None)
                    st.session_state.pop("retour_commentaire_" + r["ID"], None)
                    st.rerun()
            liens_actions, espace_actions = st.columns([3, 1], vertical_alignment="bottom")
            with liens_actions, st.container(key="actions_compactes_"+r["ID"]):
                gauche,milieu,droite=st.columns(3, gap="small")
                with gauche, st.container(key="action_outlook_"+r["ID"]):
                    adresse = email if re.fullmatch(r"[^\s@;,<>]+@[^\s@;,<>]+\.[^\s@;,<>]+", email) else ""
                    sujet, corps = preparer_message(r)
                    email_autorise = cible is not None and (vendeur == "Admin" or not r["_ids"] or cible in LIBELLES_RELANCES[2:])
                    if email_autorise:
                        st.link_button("Préparer l’e-mail", lien_outlook_web(adresse, sujet, corps),
                                       type="primary", use_container_width=True,
                                       help="Ouvre un message modifiable dans Outlook Web, avec votre compte Microsoft connecté.")
                    else:
                        st.button("Préparer l’e-mail", key="email_desactive_" + vendeur + "_" + r["ID"],
                                  disabled=True, type="primary", use_container_width=True,
                                  help="Les 1ère et 2ème relances doivent être effectuées par téléphone" if cible else "Les quatre relances ont déjà été effectuées.")
                with milieu, st.container(key="action_sharepoint_"+r["ID"]):
                    if r["SharePoint"]:
                        st.link_button("Vers SharePoint", r["SharePoint"], use_container_width=True)
                    else:
                        st.button("SharePoint", disabled=True, key="sp_"+vendeur+"_"+r["ID"], help="Lien SP non renseigné dans ClickUp", use_container_width=True)
                with droite, st.container(key="action_clickup_"+r["ID"]):
                    lien=r["Lien"]
                    if isinstance(lien,str) and lien.startswith("https://app.clickup.com/"):
                        st.link_button("Vers ClickUp",lien, use_container_width=True)



    if nombre_affiche < len(visibles):
        prochain_lot = min(taille_lot, len(visibles) - nombre_affiche)
        st.button(f"Afficher {prochain_lot} clients supplémentaires",
                  key="clients_supplementaires_"+vendeur, type="primary",
                  on_click=afficher_plus_clients)
    else:
        st.caption("Tous les clients correspondant aux filtres sont affichés.")

    st.caption("Cliquez sur « Relance effectuée » après avoir réellement contacté le client. Ce bouton enregistre la date dans ClickUp. Préparer un e-mail ne l’envoie pas et ne valide pas la relance.")
