# Constrelec SA — Application de relances clients (V1)

Version indépendante de l’application de relances Soleol, basée sur son ergonomie et ses workflows, avec le logo Constrelec fourni. **Aucune liste, clé ClickUp, personne, adresse de courriel ni dépôt Soleol n’est repris.**

## Mise en route

1. Créer une **nouvelle** application Streamlit Cloud pour Constrelec, idéalement avec accès privé. Ne pas modifier le déploiement Soleol.
2. Copier les paramètres de `.streamlit/secrets.example.toml` dans les *Secrets* de la nouvelle application (ou dans `.streamlit/secrets.toml` en local). Renseigner l’identifiant **de la liste ClickUp Constrelec** et `CONSTRELEC_CLICKUP_API_KEY` (une clé autorisée à lire cette liste).
3. Si la liste utilise un nom de statut différent, régler `CONSTRELEC_STATUT_OFFRES` et `CONSTRELEC_STATUT_PERDU` avec les noms **exacts** de ClickUp.
4. Pour que chaque salarié enregistre ses modifications sous son **propre compte ClickUp**, créer/configurer une application OAuth ClickUp et ajouter les trois `CONSTRELEC_CLICKUP_OAUTH_...` ; la redirection HTTPS doit correspondre à l’URL de l’application Constrelec. Sans cela, la consultation fonctionne, mais les écritures sont désactivées.
5. Démarrer : `python -m pip install -r requirements.txt` puis `python -m streamlit run tableau_clickup.py`.

## Données attendues dans ClickUp

- Une liste Constrelec avec un statut des offres envoyées (modifiable dans les Secrets).
- Champs de date de l’offre et des relances, comme dans le modèle d’origine : `A - Offres envoyées`, `A - 1ère relance`, …, `A - 10ème relance` (11 champs de date). Les alias acceptés figurent dans `ALIASES_CHAMPS` du programme. **Si les champs Constrelec sont différents, adapter leurs noms avant l’utilisation.**
- Champ Date `A - Prochaine action` pour une mise en attente et pour programmer la prochaine relance.
- Facultatifs mais recommandés : référence (`N° offre`, `N° Constrelec`…), montant, type de projet, adresse, e-mail, téléphone et lien SharePoint.
- Champ `Chance to win` facultatif : s’il existe, la validation de la relance conserve le comportement de Soleol ; sinon, elle n’est pas imposée.
- Pour « Offre perdue », prévoir un statut configuré via `CONSTRELEC_STATUT_PERDU` et un champ *drop_down* intitulé `Motif de perte` (ou un des alias définis par le code).

## Fonctions

Portefeuille global, comptes vendeurs détectés automatiquement depuis les personnes assignées aux tâches ClickUp, non attribuées, relances à effectuer, dates de prochaine action, attente, offre perdue, historique, statistiques, carte, commentaires ClickUp, liens de suivi, préparation de courriels Outlook. La règle spéciale AO de Soleol est **désactivée** par défaut, activable avec `CONSTRELEC_REGLE_ADMIN_AO = "true"`.

**Important** : les récapitulatifs automatiques par e-mail de Soleol ont été volontairement exclus : leurs expéditeurs et destinataires ne doivent pas être réutilisés. Ils pourront être ajoutés après définition des destinataires Constrelec. La version n’est **pas encore testée sur les données ClickUp Constrelec** : une vérification des champs et des statuts sera nécessaire avant son utilisation en production.

**Sécurité** : ne mettez pas de clés API dans ce dépôt et n’utilisez pas un hébergement public sans authentification pour exposer des données clients ou les identifiants des projets.
