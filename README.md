# Constrelec SA — Application de relances clients (V6)

Application Streamlit dédiée uniquement à **Constrelec SA**. Cette version reprend toutes les fonctions de V5 et adapte l'accueil visuel sans changer les données ClickUp.

## Modifications V6

- **Écran de chargement :** la photographie de l'armoire électrique fournie par Constrelec est affichée pendant la lecture des offres ClickUp. Elle disparaît après la synchronisation ; la page normale de relances réapparaît.
- **Logo en haut à gauche :** utilisation du logo fourni sur fond noir/anthracite dans la barre latérale, sans fond blanc ni encadré.
- Conservation du **portefeuille global**, des **quatre relances maximum**, du statut ClickUp **`10_ENVOYéES`**, des commentaires par Entrée et de la synchronisation personnelle OAuth existante.

## Mise à jour du repository GitHub

Dans le dépôt `Soleol/constrelec-relances`, sur la branche `main`, remplacer **trois fichiers** :

| Fichier de ce ZIP | Emplacement GitHub |
|---|---|
| `tableau_clickup.py` | Racine du repository |
| `assets/logo_constrelec.png` | Dossier `assets` (écraser l'ancien) |
| `assets/photo_chargement_constrelec.jpg` | Dossier `assets` (nouveau fichier) |

Cliquer sur **Commit changes** pour enregistrer. Streamlit Cloud redéploiera le code. **Ne pas publier le ZIP tel quel** : importer les fichiers dans leur emplacement d'origine.

Aucune modification des Secrets n'est nécessaire. Ne jamais placer dans GitHub de clé API ClickUp, de Client Secret OAuth ou de fichier `.streamlit/secrets.toml` contenant de vrais secrets.

## Fonctionnement

1. Dès qu'un premier chargement ou une actualisation des données ClickUp démarre, une page de chargement affiche la photo, un titre et une barre de progression.
2. Lorsqu'on termine le traitement des données, le panneau photo disparaît pour laisser place à l'interface de relances habituelle.
3. Le logo Constrelec est toujours visible en haut de la barre latérale. Sa couleur de fond est assortie à celle de la barre latérale pour éviter l'effet « carré blanc ».

## Vérification

La syntaxe Python et la présence des ressources ont été vérifiées localement. Le fonctionnement connecté à ClickUp et le rendu exact sur Streamlit Cloud restent à valider après déploiement.
