---
source_sha256: 5e2ac3299c3cc7687d86698df36b0bd3c4f74a5a32017b29340aa78dd6289043
---

<!-- vale off -->

# Extension du registre Portolan pour QGIS

Une extension QGIS pour le [registre Portolan](https://github.com/portolan-sdi/portolan-registry). Elle liste chaque catalogue enregistré et parcourt l'arborescence STAC de chaque catalogue. De là, vous ajoutez les données à la carte ou vous les téléchargez.

- Les **PMTiles** deviennent des couches de tuiles vectorielles dessinées avec le style MapLibre de la collection.
- Le **GeoParquet** se charge avec [DuckDB](https://duckdb.org/). La vue de la carte, un polygone dessiné ou une couche de polygones peut limiter la copie.
- Les fichiers **COG**, **GeoJSON**, **FlatGeobuf** et **CSV** s'ouvrent par HTTP sans téléchargement.
- Les **collections d'items COG** s'ajoutent en une seule couche de mosaïque des items de la zone choisie.
- Les **téléchargements** conservent la structure de dossiers du catalogue et vérifient chaque fichier avec sa `file:checksum`. Ils ne conservent que les items et les entités GeoParquet de la zone choisie.

L'extension nécessite QGIS 3.34 ou plus récent, et elle fonctionne sous QGIS 4. Le GeoParquet nécessite aussi DuckDB 1.5.0 ou plus récent. Voir [Installation](install.md).
