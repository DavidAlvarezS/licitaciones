# Licitaciones de software en Galicia

Cada día, GitHub Actions descarga los datos abiertos de la Plataforma de Contratación del
Sector Público, filtra las licitaciones abiertas de servicios informáticos (CPV 72) en Galicia
de hasta 60.000 € y guarda el resultado en `licitaciones_filtradas.csv`.

Para lanzarlo a mano: pestaña **Actions** > "Buscar licitaciones" > **Run workflow**.
Los filtros se cambian en `.github/workflows/licitaciones.yml`.
