#!/usr/bin/env python3
"""
Descarga las licitaciones publicadas en la Plataforma de Contratación del Sector
Público (datos abiertos oficiales, formato Atom/CODICE) y las filtra.

Solo usa la librería estándar de Python 3.8+ (no hay que instalar nada).

Ejemplos:
  python buscar_licitaciones.py
  python buscar_licitaciones.py --max-importe 80000 --cpv 72 48
  python buscar_licitaciones.py --nuts ES111 ES112 ES113 ES114   (solo provincias)
  python buscar_licitaciones.py --fichero licitaciones_202611.zip  (fichero ya descargado)
"""
import argparse, csv, datetime as dt, io, sys, time, urllib.request, zipfile
import xml.etree.ElementTree as ET

# Feed de licitaciones publicadas en perfiles del contratante (se actualiza a diario).
URL_DEFECTO = ("https://contrataciondelestado.es/sindicacion/sindicacion_643/"
               "licitacionesPerfilesContratanteCompleto3.atom")


def local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def hijos(el, nombre):
    return [c for c in el if local(c.tag) == nombre]


def primero(el, *ruta):
    """Baja por la ruta de nombres locales y devuelve el primer elemento o None."""
    actuales = [el]
    for nombre in ruta:
        sig = []
        for a in actuales:
            sig += hijos(a, nombre)
        actuales = sig
        if not actuales:
            return None
    return actuales[0]


def texto(el, *ruta):
    e = primero(el, *ruta) if ruta else el
    return (e.text or "").strip() if e is not None else ""


def descendientes(el, nombre):
    return [e for e in el.iter() if local(e.tag) == nombre]


def procesar_entry(entry):
    d = {
        "id": texto(entry, "id"),
        "actualizado": texto(entry, "updated"),
        "titulo": texto(entry, "title"),
        "enlace": "",
    }
    for l in hijos(entry, "link"):
        d["enlace"] = l.get("href", "")
        break
    cfs = primero(entry, "ContractFolderStatus")
    if cfs is None:
        return None
    d["expediente"] = texto(cfs, "ContractFolderID")
    d["estado"] = texto(cfs, "ContractFolderStatusCode")  # PUB=publicada, EV=evaluación, ADJ, RES...
    d["organo"] = texto(cfs, "LocatedContractingParty", "Party", "PartyName", "Name")
    proj = primero(cfs, "ProcurementProject")
    d["objeto"] = texto(proj, "Name") if proj is not None else d["titulo"]
    d["tipo_contrato"] = texto(proj, "TypeCode") if proj is not None else ""
    d["importe_sin_iva"] = texto(proj, "BudgetAmount", "TaxExclusiveAmount") if proj is not None else ""
    d["valor_estimado"] = texto(proj, "BudgetAmount", "EstimatedOverallContractAmount") if proj is not None else ""
    d["cpv"] = ",".join(sorted({(e.text or "").strip() for e in
                                descendientes(proj, "ItemClassificationCode")})) if proj is not None else ""
    d["lugar_nuts"] = ",".join(sorted({(e.text or "").strip() for e in
                                       descendientes(proj, "CountrySubentityCode")})) if proj is not None else ""
    d["lugar"] = ",".join(sorted({(e.text or "").strip() for e in
                                  descendientes(proj, "CountrySubentity")})) if proj is not None else ""
    d["procedimiento_cod"] = texto(cfs, "TenderingProcess", "ProcedureCode")
    fin = primero(cfs, "TenderingProcess", "TenderSubmissionDeadlinePeriod")
    d["fin_plazo"] = (texto(fin, "EndDate") + " " + texto(fin, "EndTime")).strip() if fin is not None else ""
    return d


def leer_atom(fuente, entradas, vistos_paginas):
    """Lee un Atom (bytes). Devuelve la URL de la página siguiente, si hay."""
    siguiente = None
    for _, el in ET.iterparse(io.BytesIO(fuente), events=("end",)):
        t = local(el.tag)
        if t == "entry":
            d = procesar_entry(el)
            if d:
                previo = entradas.get(d["id"])
                if previo is None or d["actualizado"] >= previo["actualizado"]:
                    entradas[d["id"]] = d
            el.clear()
        elif t == "link" and el.get("rel") == "next":
            siguiente = el.get("href")
    return siguiente


def descargar(url, espera=30, intentos=2):
    """Descarga una URL. Espera máx. `espera` s por operación y reintenta una vez."""
    req = urllib.request.Request(url, headers={"User-Agent": "buscador-licitaciones/1.0"})
    for i in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(req, timeout=espera) as r:
                return r.read()
        except Exception as e:
            print(f"  intento {i}/{intentos} fallido: {e}", file=sys.stderr, flush=True)
            if i == intentos:
                raise


def cumple(d, a):
    if a.solo_publicadas and d["estado"] != "PUB":
        return False
    if a.cpv and not any(c.startswith(tuple(a.cpv)) for c in d["cpv"].split(",") if c):
        return False
    if a.nuts and not any(n.startswith(tuple(a.nuts)) for n in d["lugar_nuts"].split(",") if n):
        return False
    try:
        imp = float(d["importe_sin_iva"]) if d["importe_sin_iva"] else None
    except ValueError:
        imp = None
    if a.max_importe and imp is not None and imp > a.max_importe:
        return False
    if a.solo_publicadas and d["fin_plazo"]:
        try:
            fin = dt.datetime.fromisoformat(d["fin_plazo"].replace("Z", "").split("+")[0].strip())
            if fin < dt.datetime.now():
                return False
        except ValueError:
            pass
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=URL_DEFECTO, help="URL del feed Atom")
    p.add_argument("--fichero", help="Atom o ZIP ya descargado (en vez de la URL)")
    p.add_argument("--paginas", type=int, default=60, help="máx. páginas a seguir del feed (def. 60)")
    p.add_argument("--cpv", nargs="*", default=["72"], help="prefijos CPV (def. 72 = servicios TI)")
    p.add_argument("--nuts", nargs="*", default=["ES11"], help="prefijos NUTS (def. ES11 = Galicia)")
    p.add_argument("--max-importe", type=float, default=60000, help="importe máx. sin IVA (def. 60000)")
    p.add_argument("--todas", dest="solo_publicadas", action="store_false",
                   help="incluir también cerradas/adjudicadas")
    p.add_argument("--max-minutos", type=float, default=9,
                   help="tiempo máx. de descarga; al llegar se guarda lo descargado (def. 9)")
    p.add_argument("--salida", default="licitaciones_filtradas.csv")
    a = p.parse_args()

    entradas = {}
    if a.fichero:
        datos = open(a.fichero, "rb").read()
        if a.fichero.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(datos)) as z:
                for n in z.namelist():
                    if n.lower().endswith((".atom", ".xml")):
                        leer_atom(z.read(n), entradas, set())
        else:
            leer_atom(datos, entradas, set())
    else:
        url, n = a.url, 0
        vistas = set()
        limite = time.time() + a.max_minutos * 60
        while url and url not in vistas and n < a.paginas:
            if time.time() > limite:
                print(f"Límite de {a.max_minutos} min alcanzado: se guarda lo descargado.",
                      file=sys.stderr, flush=True)
                break
            vistas.add(url)
            n += 1
            t0 = time.time()
            print(f"Descargando página {n} ...", file=sys.stderr, flush=True)
            try:
                datos = descargar(url)
            except Exception as e:
                print(f"No se pudo descargar la página {n}: {e}. Se guarda lo descargado.",
                      file=sys.stderr, flush=True)
                break
            url = leer_atom(datos, entradas, vistas)
            print(f"  página {n}: {len(datos)//1024} KB en {time.time()-t0:.1f}s, "
                  f"{len(entradas)} entradas acumuladas", file=sys.stderr, flush=True)

    print(f"Entradas leídas: {len(entradas)}", file=sys.stderr)
    sel = [d for d in entradas.values() if cumple(d, a)]
    sel.sort(key=lambda d: d["fin_plazo"] or "9999")
    campos = ["expediente", "objeto", "organo", "importe_sin_iva", "valor_estimado", "cpv", "lugar",
              "lugar_nuts", "tipo_contrato", "procedimiento_cod", "estado", "fin_plazo", "actualizado", "enlace"]
    with open(a.salida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(sel)
    print(f"Licitaciones que cumplen los filtros: {len(sel)} -> {a.salida}", file=sys.stderr)


if __name__ == "__main__":
    main()
