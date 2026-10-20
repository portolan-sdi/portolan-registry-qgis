---
source_sha256: 2bbb614db290e57532439399fd9fda6d3e5415d08c4642828313d29fbc75fb9f
---

<!-- vale off -->

# Uso

Abra la ventana desde **Web > Portolan Registry** o con el botón de la barra de herramientas. La ventana no es modal, así que el mapa se desplaza y se acerca mientras está abierta. Conserva su tamaño y la posición de su divisor de una sesión a la siguiente.

El botón de actualizar del encabezado vuelve a leer el registro.

## Buscar un publicador

El panel izquierdo se abre con los publicadores del registro. Cada fila muestra un mapa pequeño con la extensión del catálogo, su número de colecciones, su tamaño y sus licencias, y su logotipo cuando el registro tiene uno. Un catálogo cuya extensión cubre la mayor parte del mundo colorea la tierra en lugar de dibujar un rectángulo. Filtre la lista por nombre o licencia, o seleccione **En la extensión del mapa** para conservar los catálogos que se superponen con el mapa.

La lista muestra 25 publicadores cada vez. Seleccione **Mostrar 25 más** al final de la lista para ver los siguientes. Haga clic derecho en un publicador y seleccione **Abrir en el navegador de Portolan** para ver su catálogo en el [navegador de Portolan](https://browser.portolan-sdi.org/).

Seleccione un publicador para abrir su catálogo. El encabezado muestra el catálogo, y el árbol de abajo lista sus colecciones. Cada colección muestra su propio icono cuando el catálogo publica uno. El árbol lista 50 entradas cada vez, con una fila **Mostrar 50 más** para el resto. **Todos los publicadores** vuelve a la lista.

Una colección cuyos hijos son todos ítems, como un conjunto de teselas de imagen, muestra una fila como **240 archivos** en lugar de una fila por ítem. Seleccione la colección o esa fila y luego use **Añadir como una capa** en **Añadir al mapa**. **Todos los archivos** lista el archivo de datos de cada ítem, como un COG o un archivo GeoParquet, 50 cada vez, con una fila **Mostrar más**. Seleccione uno para añadir o descargar solo ese archivo.

## Ver los detalles

El panel derecho muestra el nombre del nodo elegido, con dos botones al lado. **Zoom a** mueve el mapa a la extensión del nodo. **Abrir en el navegador de Portolan ↗** abre el nodo en el [navegador de Portolan](https://browser.portolan-sdi.org/), que muestra todos los archivos y todos los metadatos STAC que el complemento omite.

Seleccione el título de una sección para cerrarla o abrirla. La ventana recuerda qué secciones cerró. **Todos los archivos** empieza cerrada.

- **Acerca de** muestra la miniatura, o un mapa de la extensión cuando no hay miniatura, y la descripción.
- **Añadir al mapa** contiene el área de interés y luego los botones que añaden o descargan los datos que hay en ella.
- **Todos los archivos** lista los archivos que QGIS puede abrir, con **Añadir seleccionados** y **Descargar seleccionados**. Una etiqueta en cada archivo muestra su formato, y una marca de verificación indica que una capa del proyecto ya lo lee.
- **Detalles** es una sola lista: **Publicador**, **Licencia**, **Tamaño**, **Documentación**, **Fuente** y **Más**, que enlaza al navegador. La licencia, la documentación y la fuente enlazan a las páginas del publicador. **Fuente** es la página de donde vienen los datos, como el portal de datos abiertos de una ciudad. Un valor de licencia `other` se muestra como **Términos personalizados**, y un enlace que el catálogo repite se muestra una sola vez.

Seleccione el encabezado del catálogo para volver a ver los detalles del propio catálogo.

## Añadir datos al mapa

**Añadir al mapa** muestra los botones que corresponden al nodo. **Ver en el mapa** añade los PMTiles, o un COG cuando no hay PMTiles. Úselo para mirar los datos, porque solo lee las teselas en pantalla y dibuja con el estilo del publicador. **Cargar datos completos** añade el GeoParquet o, si no lo hay, el FlatGeobuf, el GeoJSON o el CSV. Úselo para análisis, porque da todos los objetos espaciales con todos sus atributos. Cuando las `table:columns` de la colección no nombran ninguna columna de geometría, la etiqueta dice **Parquet** en lugar de **GeoParquet**, y el botón dice **Cargar tabla**. Mantenga el puntero sobre un botón para ver el formato que añade y el archivo.

Para añadir otro archivo, abra **Todos los archivos**, seleccione una o más filas y luego seleccione **Añadir seleccionados**. Un doble clic en una fila también la añade. **Añadir seleccionados** añade cada archivo completo, sea cual sea el área de interés. **Todos los archivos** lista cada archivo una vez, y omite los estilos, las miniaturas, los archivos `source` de origen, los extractos `archive` anteriores y el índice `collection-mirror`.

| Archivo | Capa |
|---|---|
| Enlace o recurso PMTiles | Capa de teselas vectoriales, con los estilos MapLibre de la colección |
| GeoParquet | Capa vectorial de un GeoPackage que escribe DuckDB |
| Parquet sin geometría | Tabla sin geometría, de un GeoPackage que escribe DuckDB |
| COG | Capa ráster, leída por HTTP |
| GeoJSON, FlatGeobuf | Capa vectorial, leída por HTTP |
| CSV | Capa de puntos cuando el encabezado nombra columnas de longitud y latitud; si no, una tabla; leída por HTTP |

Una capa PMTiles recibe cada estilo MapLibre de la colección que dibuja su archivo, cada uno como un estilo de QGIS con nombre. El estilo predeterminado de la colección es el actual. Para cambiar de estilo, haga clic derecho en la capa y luego seleccione uno en **Estilos**. **Estilo predeterminado de QGIS** dibuja las teselas sin un estilo del catálogo. Un proyecto guarda todos los estilos con la capa.

En un estilo, `sources.<id>.url` hace referencia al archivo. El complemento acepta una ruta relativa al archivo de estilo, como `../tiles.pmtiles`. El prefijo `pmtiles://` es opcional. Una fuente con una plantilla `tiles` como `pmtiles://https://host/tiles.pmtiles/{z}/{x}/{y}` también hace referencia al archivo. Una fuente vectorial sin `url` lee el archivo de la colección cuando la colección tiene solo uno. Cuando un estilo hace referencia al archivo en otra URL, el complemento identifica el archivo por su nombre.

Un CSV se abre como puntos en EPSG:4326 cuando su primera línea tiene una columna de longitud (`lon`, `lng`, `longitude` o `x`) y una columna de latitud (`lat`, `latitude` o `y`). Los nombres no distinguen mayúsculas de minúsculas.

## Elegir el área de interés

El área de interés, en la parte superior de la pestaña **Cargar** de **Añadir al mapa**, limita los datos que leen los botones que hay debajo. La opción aparece cuando el nodo tiene GeoParquet o nodos hijos.

| Opción | Área |
|---|---|
| Todo | El conjunto de datos completo |
| Extensión del mapa | El rectángulo que muestra el mapa cuando empieza la carga o la descarga |
| Dibujar un área | Un polígono que usted marca con clics en el mapa |
| Desde una capa | Los polígonos de una capa del proyecto, o solo sus objetos espaciales seleccionados |

Para dibujar un área, seleccione **Dibujar un área** y luego haga clic en el mapa para añadir cada vértice. Haga clic derecho o doble clic para terminar el polígono después de tres o más vértices. Retroceso quita el último vértice, y Esc cancela. El área se queda en el mapa hasta que usted elige otra área o cierra la ventana. **Dibujar de nuevo** la reemplaza.

Cuando el área no está completa, la ventana indica por qué debajo de la opción y desactiva los botones que la necesitan. **Ver en el mapa** solo lee las teselas en pantalla, así que no usa el área.

## Cargar GeoParquet

DuckDB copia los objetos espaciales GeoParquet en un GeoPackage de la carpeta del perfil de QGIS, y la capa lee ese archivo. Con una extensión del mapa o un área, DuckDB copia solo los objetos espaciales que la intersecan. DuckDB copia completo un objeto espacial que cruza el borde del área. Cuando un archivo tiene una columna GeoParquet `bbox`, DuckDB usa sus estadísticas para saltar los grupos de filas fuera del área. Después, un polígono dibujado o de una capa compara cada objeto espacial con el propio polígono, no con su rectángulo envolvente.

La barra en la parte inferior de la ventana muestra cuánto del recuento y de la copia ha hecho DuckDB, y el tiempo restante. DuckDB informa de su progreso por pasos, así que la barra puede quedarse en 0 % mientras DuckDB lee los metadatos del archivo. **Cancelar** detiene la lectura o la copia.

Las cargas no tienen límite de objetos espaciales. Para una estimación de más de 1.000.000 objetos espaciales, la ventana pregunta primero, porque la copia lleva tiempo y espacio en disco. La estimación cuenta los objetos espaciales del rectángulo envolvente del área, así que la copia puede contener menos. Para detener una lectura o una copia, seleccione **Cancelar** en el administrador de tareas de QGIS.

Una capa almacena un solo tipo de geometría. Cuando un objeto espacial tiene otro tipo, conserva sus atributos y no tiene geometría, y la ventana informa de cuántos objetos espaciales afecta. Un archivo Parquet sin columna de geometría se carga como una tabla con todas las filas, sea cual sea el área.

El complemento elimina un GeoPackage cuando usted quita la última capa que lo lee. Un proyecto guardado conserva cada capa GeoParquet como un enlace a su archivo de origen y a su área. Al abrir el proyecto, el complemento vuelve a copiar los objetos espaciales. La copia nueva descarta sus ediciones, así que exporte la capa para conservarlas.

## Elegir filas y columnas

Para un nodo con GeoParquet, **Añadir al mapa** tiene dos pestañas. **Cargar** contiene el área de interés y los botones. **SQL** elige lo que conserva una carga de GeoParquet y muestra la consulta de DuckDB que la hace.

| Control | Uso |
|---|---|
| Archivo | El archivo GeoParquet que se consulta, cuando el nodo tiene más de uno |
| Filas | Una expresión de QGIS, como `"type" = 'school'`. Seleccione **ε** para construirla con las columnas del archivo |
| Columnas | Una casilla por columna. La geometría siempre se carga |
| Previsualizar filas | Las primeras 100 filas que coinciden, en una tabla bajo la consulta, sin crear una capa |
| Copiar SQL | Un script para la línea de comandos de DuckDB que escribe el mismo GeoPackage |

El filtro puede usar comparaciones, `IN`, `LIKE`, `ILIKE`, `IS NULL`, `AND`, `OR` y `NOT`. La extensión pasa cada valor a DuckDB como parámetro, nunca como texto SQL. La consulta se actualiza mientras escribe y solo lee el área de interés. El script copiado instala las extensiones de DuckDB y crea el secreto de S3 que necesita un bucket con un punto en el nombre.

Una elección se aplica en el acto. La pestaña **Cargar** dice entonces lo que conserva la próxima carga, por ejemplo **Carga 2 de 14 columnas, solo las filas donde "cod_estado" = 'MT'.**, y **Cargar datos completos** pasa a **Cargar datos**. **Descargar** escribe el archivo elegido como un GeoPackage con las mismas filas y columnas. Seleccione **Borrar** al final de esa línea para volver a cargar todas las filas y columnas.

Para escribir la consulta usted mismo, marque **Escribir mi propio SQL** y edite el `SELECT`. Debe devolver la geometría en una columna con el nombre de geometría del propio archivo. La extensión acepta una sola sentencia `SELECT` y rechaza cualquier otro texto antes de que DuckDB lo ejecute. Su SQL sustituye las filas, las columnas y el área de interés.

Un proyecto guardado conserva el filtro, las columnas y su SQL. El SQL de un proyecto puede leer archivos de su equipo y enviarlos a otro servidor. Al abrir el proyecto, la extensión muestra ese SQL completo y pregunta antes de ejecutarlo. **Mantener la capa vacía** no ejecuta nada.

## Repetir una carga fuera de QGIS

La pequeña flecha junto a **Ver en el mapa** para un COG, junto a **Cargar datos completos** para un archivo FlatGeobuf, GeoJSON o CSV, y junto a **Añadir como una capa** abre **Copiar como comando GDAL**. El comando hace lo mismo fuera de QGIS con `gdal_translate`, `ogr2ogr`, o `gdalbuildvrt` y `gdalwarp`. Para un mosaico, la extensión busca primero los COG del área de interés, como hace **Añadir como una capa**, y los comandos escriben el área en `clip.geojson` para el recorte.

## Cargar GeoParquet particionado

Un conjunto de datos grande puede dividirse en muchos archivos GeoParquet, que el catálogo describe con un solo patrón glob como `s3://bucket/data/country=*/*.parquet`. El complemento lee el glob del `partition:glob` de la colección, o de un recurso de datos cuyo href contiene `*`. Después, DuckDB lee todos los archivos como una sola tabla y añade las claves de partición, como `country`, como columnas.

HTTPS no puede listar los archivos de una carpeta, así que DuckDB necesita el glob como una URL `s3://` o `gs://`. El complemento convierte las direcciones HTTPS de Amazon S3 y Google Cloud Storage a estas formas. Para un glob en otro servidor HTTPS, **Cargar datos completos** lee en su lugar el archivo GeoParquet de cada ítem del área. El complemento encuentra los ítems en el `collection-mirror` de la colección cuando tiene uno. Si no, lee los documentos de los ítems, 32 a la vez. Cuando el catálogo no da ni un glob de bucket ni ítems, **Cargar datos completos** está desactivado, y su descripción emergente indica por qué.

Las capas PMTiles leen a través de un servidor local que el complemento ejecuta en `127.0.0.1`. Un proyecto guardado las vuelve a conectar al reabrirse.

## Añadir los archivos de muchos ítems como una capa

Para una colección cuyos hijos son todos ítems, **Añadir al mapa** muestra **Añadir como una capa**. El complemento busca los ítems que intersecan el área de interés y lee el primer COG de cada ítem o, si no lo hay, su archivo GeoParquet. DuckDB copia los ítems GeoParquet en un solo GeoPackage, como hace **Cargar datos completos**. Para los ítems COG, el complemento crea un VRT de GDAL. Con una extensión del mapa o un área, el VRT recorta el mosaico a ella, así que la capa muestra solo los píxeles de dentro. El recorte conserva el SRC y los píxeles de los COG. QGIS añade el VRT como una capa ráster y lee solo las teselas en pantalla.

Mientras GDAL abre los COG, la barra muestra la parte de los archivos abiertos y el tiempo restante.

Cuando la colección tiene un recurso `collection-mirror` en stac-geoparquet, DuckDB lee de él las extensiones de los ítems en una sola consulta. Sin él, o sin DuckDB, el complemento lee el documento de cada ítem. Por encima de 500 archivos, la ventana pregunta primero, porque GDAL abre cada archivo para crear el VRT.

El VRT va a la misma carpeta temporal que los GeoPackages, y el complemento lo elimina con la última capa que lo lee. Un proyecto guardado conserva las URL de los COG y el recorte, y el complemento vuelve a crear el VRT al abrir el proyecto.

## Descargar

**Descargar**, en **Añadir al mapa**, guarda el nodo elegido y todo lo que hay debajo de él en el área de interés, después de mostrar el número de archivos y el tamaño conocido. Omite cada ítem cuya extensión queda fuera del área, e indica cuántos omitió. Con una extensión del mapa o un área, guarda cada archivo GeoParquet como un GeoPackage que contiene solo los objetos espaciales del área. El GeoPackage toma el nombre del archivo con la extensión `.gpkg`.

**Descargar seleccionados**, en **Todos los archivos**, guarda completos los archivos que usted seleccionó, sea cual sea el área.

Un recurso GeoParquet particionado se descarga como cada archivo que coincide con su glob, que DuckDB lista primero. Con una extensión del mapa o un área, se descarga como un solo GeoPackage, con el nombre de la carpeta que contiene las particiones.

La barra cuenta los bytes descargados cuando el catálogo da el tamaño de cada archivo, y si no, los archivos. Muestra el tiempo restante.

El complemento guarda cada archivo en la carpeta que usted elige, en la misma ruta relativa que en el catálogo, así que la copia se abre como un catálogo STAC local. Después de cada descarga, el complemento verifica el archivo con su `file:checksum` cuando el catálogo lo da. Una segunda ejecución omite los archivos que ya están completos, así que reanuda una descarga interrumpida.

## Informar de un problema

**Informar de un problema ↗**, al final de **Detalles**, abre el formulario de problemas con conjuntos de datos del registro en GitHub. El formulario llega con el ID del catálogo, el documento STAC y su página del navegador, la fecha de hoy y las versiones del complemento y de QGIS. Elija el tipo de problema y luego indique qué está mal, cómo lo encontró y por qué importa. El registro envía entonces un correo a la persona que registró el catálogo. El complemento no envía nada por sí mismo. El botón solo se muestra para un catálogo que tiene un ID en el registro.

**Informar de un error**, en **Web > Portolan Registry** y en la cabecera de la ventana, abre el formulario de problemas del complemento en GitHub. Sus campos cubren qué salió mal, los pasos en QGIS y por qué importa. El campo **Version** llega con las versiones del complemento, QGIS, Qt, Python, GDAL y DuckDB, y el sistema operativo. Necesita una cuenta de GitHub para enviar cualquiera de los dos formularios.

## Usar otro registro u otro navegador

El complemento lee la exportación del registro de Portolan en `exports/catalogs.json` y enlaza al navegador en `https://browser.portolan-sdi.org/`. Para dirigir el complemento a una réplica, abra **Configuración > Opciones > Avanzado**. Defina `PortolanRegistry/registry_url` para el registro o `PortolanRegistry/browser_url` para el navegador, y luego vuelva a abrir la ventana.
