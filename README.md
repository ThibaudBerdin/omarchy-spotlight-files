# thibaud.files — Spotlight Files pour Omarchy

Plugin overlay natif **Quickshell** (`omarchy-shell`) pour chercher instantanément dans ses dossiers et **Dropbox** façon Spotlight sur macOS, **sans démon permanent en arrière-plan** (100 % aligné avec la philosophie Omarchy 4 / `folio`).

## Fonctionnalités

- **Recherche par nom ultra-rapide (`fd` + `fzf`)** : parcourt ~220 000 fichiers en ~200 ms avec correspondance floue multi-mots (`invoice 2019 pdf`, `berdin devis`).
- **Recherche dans le contenu (`Tab` ou préfixe `?mot` / `c:mot`)** :
  - **PDF, Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`), LibreOffice (`.odt`/`.ods`)** via un cache local **SQLite FTS5** insensible aux accents (`unicode61 remove_diacritics 2`) + extraction à la demande (`pdftotext` / XML).
  - **Texte, Markdown, CSV, JSON et code** en direct via `ripgrep` (`rg`).
  - **`Ctrl + I`** dans l'interface lance (ou met en pause) la pré-indexation incrémentale de tous les PDF/Office dans `~/.cache/thibaud.files/fts.sqlite` (seuls les fichiers nouveaux ou modifiés sont lus).
- **Préfixes rapides dans la barre** :
  - `?terme` ou `c:terme` : chercher dans le contenu
  - `d:terme` : dossiers uniquement
  - `pdf:terme`, `doc:terme`, `xls:terme`, `img:terme` : filtrer par type
- **Aperçu Spotlight en direct** : rendu de la 1re page des PDF (`pdftoppm`), images, texte extrait des documents Word/Excel/code, contenu des dossiers.
- **Intégration Dropbox** : icône `󰇣` automatique et copie directe d'un **lien de partage Dropbox** avec `Ctrl + L`.

## Raccourcis dans l'overlay

| Touche | Action |
|---|---|
| `Entrée` | Ouvrir le fichier ou dossier |
| `Ctrl + Entrée` | Révéler le fichier dans le gestionnaire de fichiers |
| `Shift + Entrée` | Parcourir ce sous-dossier dans la barre (si dossier) |
| `Tab` / `Ctrl + G` | Basculer entre recherche par **Noms** et par **Contenu** |
| `Ctrl + Y` | Copier le chemin complet dans le presse-papiers |
| `Ctrl + L` | Générer et copier le lien de partage **Dropbox** |
| `Ctrl + I` | Lancer / mettre en pause l'indexation cache PDF & Office |
| `Ctrl + Suppr` | Déplacer le fichier sélectionné dans la corbeille |
| `Échap` | Effacer la recherche, puis fermer |

## Développement & mise à jour

Le code source vit dans `/home/thibaud/Work/thibaud-files` et est installé dans `~/.config/omarchy/plugins/thibaud.files`.

Après modification dans `~/Work/thibaud-files` :

```bash
git -C ~/Work/thibaud-files commit -am "Update plugin"
omarchy plugin update thibaud.files --yes
```

## Configuration optionnelle

Créer `~/.config/omarchy/thibaud-files.json` pour personnaliser les dossiers exclus :

```json
{
  "excludes": [".git", "node_modules", "__MACOSX", "target", "venv"],
  "maxIndexedTextBytes": 350000
}
```
