#!/usr/bin/env python3
"""
Descarga los pliegos y demás documentos de las licitaciones del CSV y extrae su texto.

Entrada : licitaciones_filtradas.csv + estado_licitaciones.json (los crea buscar_licitaciones.py)
Salida  : <salida>/pdfs/<licitacion>/...      documentos originales (NO se suben al repositorio)
          <salida>/texto/<licitacion>/...     texto extraído + _ficha.md (resumen de la licitación)
          <salida>/manifest.json              registro de lo descargado, para no repetir

Solo usa la librería estándar. Para extraer texto de PDF usa `pypdf` o `pdftotext` si existen.

Ejemplos:
  python descargar_pliegos.py
  python descargar_pliegos.py --solo-desarrollo      (solo filas marcadas si / quizá)
"""
import argparse, csv, hashlib, io, json, os, re, shutil, subprocess, sys, time
import urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 80 * 1024 * 1024      # no descargar nada de más de 80 MB
MAX_INTENTOS = 3                  # veces que se reintenta un documento que falla
UA = "buscador-licitaciones/1.0"


def seguro(texto, largo=60):
    t = re.sub(r"[^A-Za-z0-9._-]+", "_", texto).strip("_")
    return (t or "sin_nombre")[:largo]


def firma_corta(texto):
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()[:8]


def descargar(uri, espera=60):
    """Devuelve (bytes, nombre_sugerido) o lanza excepción."""
    req = urllib.request.Request(uri, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=espera) as r:
        nombre = ""
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
        if m:
            nombre = urllib.parse.unquote(m.group(1))
        datos = r.read(MAX_BYTES + 1)
        if len(datos) > MAX_BYTES:
            raise ValueError("documento de más de 80 MB, se omite")
        return datos, nombre


def tipo_por_contenido(datos):
    if datos[:5] == b"%PDF-":
        return ".pdf"
    if datos[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(datos)) as z:
                if "word/document.xml" in z.namelist():
                    return ".docx"
        except zipfile.BadZipFile:
            pass
        return ".zip"
    if datos[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".doc"
    if datos[:100].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return ".html"
    return ".bin"


def texto_pdf(ruta):
    try:
        import pypdf
        lector = pypdf.PdfReader(ruta)
        partes = []
        for i, pag in enumerate(lector.pages, 1):
            partes.append(f"\n\n--- página {i} ---\n" + (pag.extract_text() or ""))
        t = "".join(partes)
        if len(t.strip()) > 200:
            return t
    except Exception:
        pass
    if shutil.which("pdftotext"):
        try:
            return subprocess.run(["pdftotext", "-layout", ruta, "-"], capture_output=True,
                                  text=True, timeout=180).stdout
        except Exception:
            pass
    return ""


def texto_docx(datos):
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            raiz = ET.fromstring(z.read("word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        parrafos = []
        for p in raiz.iter(ns + "p"):
            parrafos.append("".join(t.text or "" for t in p.iter(ns + "t")))
        return "\n".join(parrafos)
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="licitaciones_filtradas.csv")
    ap.add_argument("--estado", default="estado_licitaciones.json")
    ap.add_argument("--salida", default="pliegos")
    ap.add_argument("--solo-desarrollo", action="store_true", help="solo filas con desarrollo = si o quizá")
    ap.add_argument("--max-minutos", type=float, default=40, help="tiempo máximo (def. 40)")
    ap.add_argument("--pausa", type=float, default=0.7, help="segundos entre descargas (def. 0.7)")
    a = ap.parse_args()

    with open(a.estado, encoding="utf-8") as f:
        entradas = json.load(f)["entradas"]
    por_id = entradas
    por_enlace = {d["enlace"]: d for d in entradas.values() if d.get("enlace")}

    with open(a.csv, encoding="utf-8-sig") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    if a.solo_desarrollo:
        filas = [r for r in filas if r.get("desarrollo") in ("si", "quizá")]
    print(f"Licitaciones a procesar: {len(filas)}", file=sys.stderr)

    dir_pdfs = os.path.join(a.salida, "pdfs")
    dir_txt = os.path.join(a.salida, "texto")
    os.makedirs(dir_pdfs, exist_ok=True)
    os.makedirs(dir_txt, exist_ok=True)
    ruta_manifest = os.path.join(a.salida, "manifest.json")
    try:
        manifest = json.load(open(ruta_manifest, encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}

    limite = time.time() + a.max_minutos * 60
    n_ok = n_err = n_sin_docs = 0
    for fila in filas:
        entrada = por_id.get(fila.get("id", "")) or por_enlace.get(fila.get("enlace", ""))
        docs = (entrada or {}).get("docs", [])
        carpeta = f"{seguro(fila['expediente'], 40)}_{firma_corta(fila.get('id') or fila['enlace'])}"
        os.makedirs(os.path.join(dir_pdfs, carpeta), exist_ok=True)
        os.makedirs(os.path.join(dir_txt, carpeta), exist_ok=True)
        lineas = [f"# {fila['objeto']}", "",
                  f"- Órgano: {fila['organo']}",
                  f"- Expediente: {fila['expediente']}",
                  f"- Importe sin IVA: {fila['importe_sin_iva']} €",
                  f"- Fin de plazo: {fila['fin_plazo']}",
                  f"- CPV: {fila['cpv']}",
                  f"- Desarrollo (clasificación automática): {fila.get('desarrollo', '')}",
                  f"- Ficha en la plataforma: {fila['enlace']}", "", "## Documentos", ""]
        if not docs:
            n_sin_docs += 1
            lineas.append("_El feed no trae enlaces a documentos para esta licitación._")
        for k, doc in enumerate(docs, 1):
            uri = doc["uri"]
            reg = manifest.get(uri, {})
            if reg.get("estado") == "ok" or reg.get("intentos", 0) >= MAX_INTENTOS:
                pass
            elif time.time() > limite:
                print("Tiempo máximo alcanzado: el resto se hará en la próxima ejecución.", file=sys.stderr)
                reg = {"estado": "pendiente"}
            else:
                try:
                    datos, sugerido = descargar(uri)
                    ext = tipo_por_contenido(datos)
                    base = seguro(os.path.splitext(doc["nombre"] or sugerido or f"documento{k}")[0], 50)
                    archivo = f"{k:02d}_{doc['tipo']}_{base}{ext}"
                    ruta = os.path.join(dir_pdfs, carpeta, archivo)
                    with open(ruta, "wb") as f:
                        f.write(datos)
                    t = texto_pdf(ruta) if ext == ".pdf" else texto_docx(datos) if ext == ".docx" else ""
                    if t:
                        with open(os.path.join(dir_txt, carpeta, archivo + ".txt"), "w", encoding="utf-8") as f:
                            f.write(t)
                    reg = {"estado": "ok", "archivo": archivo, "bytes": len(datos), "texto": bool(t),
                           "carpeta": carpeta}
                    n_ok += 1
                    print(f"  OK  {fila['expediente'][:25]:25} {archivo} ({len(datos)//1024} KB"
                          f"{'' if t else ', sin texto'})", file=sys.stderr, flush=True)
                except Exception as e:
                    reg = {"estado": "error", "error": str(e)[:200], "intentos": reg.get("intentos", 0) + 1}
                    n_err += 1
                    print(f"  ERR {fila['expediente'][:25]:25} {uri[:80]}: {e}", file=sys.stderr, flush=True)
                time.sleep(a.pausa)
                manifest[uri] = reg
            estado = reg.get("estado", "?")
            if estado == "ok":
                extra = f"{reg['archivo']} ({reg['bytes']//1024} KB" + ("" if reg.get("texto") else ", sin texto extraído") + ")"
            else:
                extra = f"{estado} {reg.get('error', '')}"
            lineas.append(f"- [{doc['tipo']}] {doc['nombre'] or '(sin nombre)'}: {extra}")
        with open(os.path.join(dir_txt, carpeta, "_ficha.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")
        with open(ruta_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"Documentos descargados: {n_ok} | con error: {n_err} | licitaciones sin documentos en el feed: {n_sin_docs}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
