---
source_sha256: a73dc24723ffeda6e6ad9fed1d45206fe2aadf9c25a44af7515299f13992eb04
---

<!-- vale off -->

# Utilisation

Ouvrez la fenêtre depuis **Internet > Portolan Registry** ou le bouton de la barre d'outils. La fenêtre n'est pas modale, donc la carte se déplace et zoome pendant qu'elle est ouverte. Elle conserve sa taille et la position de son séparateur d'une session à l'autre.

Le bouton d'actualisation de l'en-tête relit le registre.

## Trouver un éditeur

Le volet de gauche s'ouvre sur les éditeurs du registre. Chaque ligne affiche une petite carte avec l'emprise du catalogue, son nombre de collections, sa taille et ses licences, ainsi que son logo quand le registre en a un. Un catalogue dont l'emprise couvre la plus grande partie du monde colore les terres au lieu de dessiner un rectangle. Filtrez la liste par nom ou licence, ou cochez **Dans l'emprise de la carte** pour garder les catalogues qui recoupent la carte.

La liste affiche 25 éditeurs à la fois. Sélectionnez **Afficher 25 de plus** à la fin de la liste pour voir les suivants. Faites un clic droit sur un éditeur et sélectionnez **Ouvrir dans le navigateur Portolan** pour voir son catalogue dans le [navigateur Portolan](https://browser.portolan-sdi.org/).

Sélectionnez un éditeur pour ouvrir son catalogue. L'en-tête affiche le catalogue, et l'arborescence en dessous liste ses collections. Chaque collection affiche sa propre icône quand le catalogue en publie une. L'arborescence liste 50 entrées à la fois, avec une ligne **Afficher 50 de plus** pour le reste. **Tous les éditeurs** revient à la liste.

Une collection dont les enfants sont tous des items, comme un ensemble de tuiles d'images, affiche une seule ligne comme **240 fichiers** au lieu d'une ligne par item. Sélectionnez la collection ou cette ligne, puis utilisez **Ajouter en une seule couche** sous **Ajouter à la carte**. **Tous les fichiers** liste le fichier de données de chaque item, comme un COG ou un fichier GeoParquet, 50 à la fois, avec une ligne **Afficher plus**. Sélectionnez-en un pour ajouter ou télécharger ce seul fichier.

## Voir les détails

Le volet de droite affiche le nom du nœud choisi, avec deux boutons à côté. **Zoomer sur** déplace la carte vers l'emprise du nœud. **Ouvrir dans le navigateur Portolan ↗** ouvre le nœud dans le [navigateur Portolan](https://browser.portolan-sdi.org/), qui affiche chaque fichier et toutes les métadonnées STAC que l'extension laisse de côté.

Sélectionnez le titre d'une section pour la fermer ou l'ouvrir. La fenêtre se souvient des sections que vous avez fermées. **Tous les fichiers** commence fermée.

- **À propos** affiche la vignette, ou une carte de l'emprise quand il n'y a pas de vignette, et la description.
- **Ajouter à la carte** contient la zone d'intérêt, puis les boutons qui ajoutent ou téléchargent les données qu'elle contient.
- **Tous les fichiers** liste les fichiers que QGIS peut ouvrir, avec **Ajouter la sélection** et **Télécharger la sélection**. Un badge sur chaque fichier indique son format, et une coche indique qu'une couche du projet le lit déjà.
- **Détails** est une seule liste : **Éditeur**, **Licence**, **Taille**, **Documentation**, **Source** et **Plus**, qui renvoie au navigateur. La licence, la documentation et la source renvoient aux pages de l'éditeur. **Source** est la page d'où viennent les données, comme le portail de données ouvertes d'une ville. Une valeur de licence `other` s'affiche comme **Conditions particulières**, et un lien que le catalogue répète s'affiche une seule fois.

Sélectionnez l'en-tête du catalogue pour revoir les détails du catalogue lui-même.

## Ajouter des données à la carte

**Ajouter à la carte** affiche les boutons qui conviennent au nœud. **Voir sur la carte** ajoute les PMTiles, ou un COG quand il n'y a pas de PMTiles. Utilisez-le pour regarder les données, car il ne lit que les tuiles à l'écran et dessine avec le style de l'éditeur. **Charger toutes les données** ajoute le GeoParquet, sinon le FlatGeobuf, le GeoJSON ou le CSV. Utilisez-le pour l'analyse, car il donne chaque entité avec tous ses attributs. Quand les `table:columns` de la collection ne nomment aucune colonne de géométrie, le badge indique **Parquet** au lieu de **GeoParquet**, et le bouton devient **Charger la table**. Placez le pointeur sur un bouton pour voir le format qu'il ajoute et le fichier.

Pour ajouter un autre fichier, ouvrez **Tous les fichiers**, sélectionnez une ou plusieurs lignes, puis sélectionnez **Ajouter la sélection**. Un double-clic sur une ligne l'ajoute aussi. **Ajouter la sélection** ajoute chaque fichier en entier, quelle que soit la zone d'intérêt. **Tous les fichiers** liste chaque fichier une seule fois, et laisse de côté les styles, les vignettes, les fichiers `source` amont, les extraits `archive` antérieurs et l'index `collection-mirror`.

| Fichier | Couche |
|---|---|
| Lien ou ressource PMTiles | Couche de tuiles vectorielles, avec les styles MapLibre de la collection |
| GeoParquet | Couche vecteur issue d'un GeoPackage qu'écrit DuckDB |
| Parquet sans géométrie | Table sans géométrie, issue d'un GeoPackage qu'écrit DuckDB |
| COG | Couche raster, lue par HTTP |
| GeoJSON, FlatGeobuf | Couche vecteur, lue par HTTP |
| CSV | Couche de points quand l'en-tête nomme des colonnes de longitude et de latitude, sinon une table, lue par HTTP |

Une couche PMTiles reçoit chaque style MapLibre de la collection qui dessine son archive, chacun comme un style QGIS nommé. Le style par défaut de la collection est le style actif. Pour changer de style, faites un clic droit sur la couche, puis sélectionnez-en un sous **Styles**. **Style par défaut de QGIS** dessine les tuiles sans style de catalogue. Un projet enregistre chaque style avec la couche.

Dans un style, `sources.<id>.url` référence l'archive. L'extension accepte un chemin relatif au fichier de style, comme `../tiles.pmtiles`. Le préfixe `pmtiles://` est facultatif. Une source avec un modèle `tiles` comme `pmtiles://https://host/tiles.pmtiles/{z}/{x}/{y}` référence aussi l'archive. Une source vecteur sans `url` lit l'archive de la collection quand la collection n'en a qu'une. Quand un style référence l'archive à une autre URL, l'extension reconnaît l'archive par son nom de fichier.

Un CSV s'ouvre en points en EPSG:4326 quand sa première ligne a une colonne de longitude (`lon`, `lng`, `longitude` ou `x`) et une colonne de latitude (`lat`, `latitude` ou `y`). Les noms ne tiennent pas compte de la casse.

## Choisir la zone d'intérêt

La zone d'intérêt en haut de **Ajouter à la carte** limite les données que lisent les boutons situés en dessous. Le choix s'affiche quand le nœud a du GeoParquet ou des nœuds enfants.

| Choix | Zone |
|---|---|
| Tout | Le jeu de données entier |
| Emprise de la carte | Le rectangle que la carte affiche quand le chargement ou le téléchargement commence |
| Dessiner une zone | Un polygone que vous tracez par clics sur la carte |
| Depuis une couche | Les polygones d'une couche du projet, ou uniquement ses entités sélectionnées |

Pour dessiner une zone, sélectionnez **Dessiner une zone**, puis cliquez sur la carte pour ajouter chaque sommet. Faites un clic droit ou un double-clic pour terminer le polygone après trois sommets ou plus. Retour arrière supprime le dernier sommet, et Échap annule. La zone reste sur la carte jusqu'à ce que vous choisissiez une autre zone ou fermiez la fenêtre. **Redessiner** la remplace.

Quand la zone n'est pas complète, la fenêtre en indique la raison sous le choix et désactive les boutons qui en ont besoin. **Voir sur la carte** ne lit que les tuiles à l'écran, donc il n'utilise pas la zone.

## Charger du GeoParquet

DuckDB copie les entités GeoParquet dans un GeoPackage du dossier de profil QGIS, et la couche lit ce fichier. Avec une emprise de la carte ou une zone, DuckDB ne copie que les entités qui l'intersectent. DuckDB copie en entier une entité qui traverse le bord de la zone. Quand un fichier a une colonne GeoParquet `bbox`, DuckDB utilise ses statistiques pour ignorer les groupes de lignes hors de la zone. Un polygone dessiné ou issu d'une couche teste ensuite chaque entité par rapport au polygone lui-même, pas à son rectangle englobant.

La barre en bas de la fenêtre montre la part du décompte et de la copie que DuckDB a faite, et le temps restant. DuckDB indique sa progression par étapes, donc la barre peut rester à 0 % pendant que DuckDB lit les métadonnées du fichier. **Annuler** arrête la lecture ou la copie.

Les chargements n'ont pas de limite d'entités. Pour une estimation de plus de 1 000 000 d'entités, la fenêtre demande d'abord, car la copie prend du temps et de l'espace disque. L'estimation compte les entités du rectangle englobant de la zone, donc la copie peut en contenir moins. Pour arrêter une lecture ou une copie, sélectionnez **Annuler** dans le gestionnaire de tâches de QGIS.

Une couche stocke un seul type de géométrie. Quand une entité a un autre type, elle conserve ses attributs mais n'a pas de géométrie, et la fenêtre indique combien d'entités sont concernées. Un fichier Parquet sans colonne de géométrie se charge comme une table avec toutes ses lignes, quelle que soit la zone.

L'extension supprime un GeoPackage quand vous retirez la dernière couche qui le lit. Un projet enregistré conserve chaque couche GeoParquet comme un lien vers son fichier source et sa zone. À l'ouverture du projet, l'extension copie à nouveau les entités. La nouvelle copie perd vos modifications, donc exportez la couche pour les conserver.

## Charger du GeoParquet partitionné

Un grand jeu de données peut se diviser en de nombreux fichiers GeoParquet, que le catalogue décrit avec un seul motif glob comme `s3://bucket/data/country=*/*.parquet`. L'extension lit le glob dans le `partition:glob` de la collection, ou dans une ressource de données dont le href contient `*`. DuckDB lit ensuite tous les fichiers comme une seule table, et ajoute les clés de partition, comme `country`, en colonnes.

HTTPS ne peut pas lister les fichiers d'un dossier, donc DuckDB a besoin du glob sous forme d'URL `s3://` ou `gs://`. L'extension convertit les adresses HTTPS d'Amazon S3 et de Google Cloud Storage vers ces formes. Pour un glob sur un autre serveur HTTPS, **Charger toutes les données** lit à la place le fichier GeoParquet de chaque item de la zone. L'extension trouve les items dans le `collection-mirror` de la collection quand elle en a un. Sinon, elle lit les documents des items, 32 à la fois. Quand le catalogue ne donne ni glob de bucket ni items, **Charger toutes les données** est désactivé, et son info-bulle en indique la raison.

Les couches PMTiles lisent par un serveur local que l'extension exécute sur `127.0.0.1`. Un projet enregistré les reconnecte à sa réouverture.

## Ajouter les fichiers de plusieurs items en une seule couche

Pour une collection dont les enfants sont tous des items, **Ajouter à la carte** affiche **Ajouter en une seule couche**. L'extension trouve les items qui intersectent la zone d'intérêt et lit le premier COG de chaque item, sinon son fichier GeoParquet. DuckDB copie les items GeoParquet dans un seul GeoPackage, comme le fait **Charger toutes les données**. Pour les items COG, l'extension construit un VRT GDAL. Avec une emprise de la carte ou une zone, le VRT découpe la mosaïque selon celle-ci, donc la couche n'affiche que les pixels à l'intérieur. Le découpage conserve le SCR et les pixels des COG. QGIS ajoute le VRT comme une seule couche raster et ne lit que les tuiles à l'écran.

Pendant que GDAL ouvre les COG, la barre montre la part des fichiers ouverts et le temps restant.

Quand la collection a une ressource `collection-mirror` en stac-geoparquet, DuckDB y lit les emprises des items en une seule requête. Sans elle, ou sans DuckDB, l'extension lit chaque document d'item. Au-delà de 500 fichiers, la fenêtre demande d'abord, car GDAL ouvre chaque fichier pour construire le VRT.

Le VRT va dans le même dossier temporaire que les GeoPackages, et l'extension le supprime avec la dernière couche qui le lit. Un projet enregistré conserve les URL des COG et le découpage, et l'extension reconstruit le VRT à l'ouverture du projet.

## Télécharger

**Télécharger** sous **Ajouter à la carte** enregistre le nœud choisi et tout ce qui se trouve en dessous dans la zone d'intérêt, après avoir affiché le nombre de fichiers et la taille connue. Il laisse de côté chaque item dont l'emprise se trouve hors de la zone, et indique combien il en a laissé de côté. Avec une emprise de la carte ou une zone, il enregistre chaque fichier GeoParquet en GeoPackage qui ne contient que les entités de la zone. Le GeoPackage prend le nom du fichier avec une extension `.gpkg`.

**Télécharger la sélection** sous **Tous les fichiers** enregistre en entier les fichiers que vous avez sélectionnés, quelle que soit la zone.

Une ressource GeoParquet partitionnée se télécharge sous la forme de chaque fichier que son glob désigne, que DuckDB liste d'abord. Avec une emprise de la carte ou une zone, elle se télécharge en un seul GeoPackage, nommé d'après le dossier qui contient les partitions.

La barre compte les octets téléchargés quand le catalogue donne la taille de chaque fichier, et sinon les fichiers. Elle montre le temps restant.

L'extension enregistre chaque fichier dans le dossier que vous choisissez, au même chemin relatif que dans le catalogue, donc la copie s'ouvre comme un catalogue STAC local. Après chaque téléchargement, l'extension vérifie le fichier avec sa `file:checksum` quand le catalogue en donne une. Une seconde exécution ignore les fichiers déjà complets, donc elle reprend un téléchargement interrompu.

## Utiliser un autre registre ou navigateur

L'extension lit l'export du registre Portolan à `exports/catalogs.json` et renvoie au navigateur à `https://browser.portolan-sdi.org/`. Pour diriger l'extension vers un miroir, ouvrez **Préférences > Options > Avancé**. Définissez `PortolanRegistry/registry_url` pour le registre ou `PortolanRegistry/browser_url` pour le navigateur, puis rouvrez la fenêtre.
