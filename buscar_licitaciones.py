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
import argparse, collections, csv, datetime as dt, io, json, re, sys, time, urllib.request, zipfile
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


def leer_atom(fuente, entradas, vistos_paginas=None):
    """Lee un Atom (bytes). Mezcla en `entradas` y devuelve (URL siguiente, entradas de la página)."""
    siguiente = None
    pagina = []
    for _, el in ET.iterparse(io.BytesIO(fuente), events=("end",)):
        t = local(el.tag)
        if t == "entry":
            d = procesar_entry(el)
            if d:
                pagina.append(d)
                previo = entradas.get(d["id"])
                if previo is None or d["actualizado"] >= previo["actualizado"]:
                    entradas[d["id"]] = d
            el.clear()
        elif t == "link" and el.get("rel") == "next":
            siguiente = el.get("href")
    return siguiente, pagina


def parse_dt(s):
    try:
        d = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def cargar_estado(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            j = json.load(f)
        return j.get("entradas", {}), j.get("meta", {})
    except (OSError, ValueError):
        return {}, {}


def guardar_estado(ruta, entradas, meta):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "entradas": entradas}, f, ensure_ascii=False, separators=(",", ":"))


def relevante(d, a):
    if a.cpv and not any(c.startswith(tuple(a.cpv)) for c in d["cpv"].split(",") if c):
        return False
    if a.nuts and not any(n.startswith(tuple(a.nuts)) for n in d["lugar_nuts"].split(",") if n):
        return False
    return True


CPV_DESARROLLO = ("72200", "72210", "72211", "72212", "72213", "72214", "72230", "72262", "72413", "72415", "72421")
PALABRAS = re.compile(r"desarroll|aplicaci|\bapp\b|\bweb\b|plataforma|software|portal|sede electr|programaci|"
                      r"sistema de informaci|a medida|digitaliza|gestor|herramienta", re.I)


def es_desarrollo(d):
    if any(c.startswith(CPV_DESARROLLO) for c in d["cpv"].split(",") if c):
        return "si"
    return "quizá" if PALABRAS.search(d["objeto"]) else "no"


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


def diagnostico(entradas, a):
    """Resumen para entender cuántas licitaciones pasan cada filtro."""
    v = list(entradas.values())
    if not v:
        return
    err = sys.stderr
    print("--- Diagnóstico ---", file=err)
    print("Fechas 'updated': de", min(d["actualizado"] for d in v), "a", max(d["actualizado"] for d in v), file=err)
    print("Por estado:", dict(collections.Counter(d["estado"] for d in v)), file=err)
    con_cpv = [d for d in v if any(c.startswith(tuple(a.cpv)) for c in d["cpv"].split(",") if c)]
    con_nuts = [d for d in v if any(n.startswith(tuple(a.nuts)) for n in d["lugar_nuts"].split(",") if n)]
    ambos = [d for d in con_cpv if d in con_nuts]
    print(f"Sin CPV: {sum(1 for d in v if not d['cpv'])} | Sin NUTS: {sum(1 for d in v if not d['lugar_nuts'])} "
          f"| Sin importe: {sum(1 for d in v if not d['importe_sin_iva'])} | Sin fin de plazo: {sum(1 for d in v if not d['fin_plazo'])}", file=err)
    print(f"CPV {a.cpv}: {len(con_cpv)} | NUTS {a.nuts}: {len(con_nuts)} | ambos: {len(ambos)}", file=err)
    print("Ejemplos de NUTS:", collections.Counter(n for d in v for n in d["lugar_nuts"].split(",") if n).most_common(8), file=err)
    print("Ejemplos de fin_plazo:", [d["fin_plazo"] for d in v[:4]], file=err)
    print("Ejemplo de entrada:", {k: v[0][k] for k in ("expediente", "estado", "cpv", "lugar_nuts", "importe_sin_iva", "fin_plazo")}, file=err)
    campos = ["expediente", "objeto", "organo", "importe_sin_iva", "cpv", "lugar", "lugar_nuts", "estado", "fin_plazo", "actualizado"]
    with open("muestra_entradas.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows((ambos or con_cpv or con_nuts or v)[:300])
    print("Muestra guardada en muestra_entradas.csv", file=err)


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
    p.add_argument("--estado", default="estado_licitaciones.json",
                   help="fichero donde se acumula el historial (def. estado_licitaciones.json)")
    p.add_argument("--hasta-dias", type=int, default=45,
                   help="hasta cuántos días atrás recorrer el feed la primera vez (def. 45)")
    p.add_argument("--salida", default="licitaciones_filtradas.csv")
    a = p.parse_args()

    entradas = {}
    estado, meta = {}, {}
    en_web = not a.fichero
    if a.fichero:
        datos = open(a.fichero, "rb").read()
        if a.fichero.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(datos)) as z:
                for n in z.namelist():
                    if n.lower().endswith((".atom", ".xml")):
                        leer_atom(z.read(n), entradas)
        else:
            leer_atom(datos, entradas)
    else:
        estado, meta = cargar_estado(a.estado)
        ahora = dt.datetime.now(dt.timezone.utc)
        hasta_dt = ahora - dt.timedelta(days=a.hasta_dias)
        cob_prev, ult_prev = parse_dt(meta.get("cobertura_desde")), parse_dt(meta.get("ultimo_updated"))
        if cob_prev is None or ult_prev is None or cob_prev > hasta_dt:
            objetivo, modo = hasta_dt, "completo"
        else:
            objetivo, modo = ult_prev - dt.timedelta(days=1), "incremental"
        print(f"Historial previo: {len(estado)} licitaciones. Modo {modo}: se recorre el feed hasta {objetivo:%Y-%m-%d %H:%M}.",
              file=sys.stderr, flush=True)
        url, n = a.url, 0
        vistas = set()
        limite = time.time() + a.max_minutos * 60
        alcanzado = nuevo_max = None
        terminado = False
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
            url, pagina = leer_atom(datos, entradas)
            fechas = [f for f in (parse_dt(d["actualizado"]) for d in pagina) if f]
            pmin = None
            if fechas:
                pmin, pmax = min(fechas), max(fechas)
                alcanzado = pmin if alcanzado is None else min(alcanzado, pmin)
                nuevo_max = pmax if nuevo_max is None else max(nuevo_max, pmax)
            print(f"  página {n}: {len(datos)//1024} KB en {time.time()-t0:.1f}s, {len(entradas)} entradas, "
                  f"fecha más antigua {pmin:%Y-%m-%d %H:%M}" if pmin else f"  página {n}: sin fechas",
                  file=sys.stderr, flush=True)
            if pmin and pmin < objetivo:
                terminado = True
                print("Se alcanzó la fecha objetivo.", file=sys.stderr, flush=True)
                break
            if n % 15 == 0:   # guardado parcial por si se corta la ejecución
                parcial = dict(estado)
                for d in entradas.values():
                    if relevante(d, a) and (d["id"] not in parcial or d["actualizado"] >= parcial[d["id"]]["actualizado"]):
                        parcial[d["id"]] = d
                guardar_estado(a.estado, parcial, meta)
        # fusionar lo nuevo con el historial
        for d in entradas.values():
            if relevante(d, a) and (d["id"] not in estado or d["actualizado"] >= estado[d["id"]]["actualizado"]):
                estado[d["id"]] = d
        contiguo = bool(ult_prev and alcanzado and alcanzado <= ult_prev)
        cob = min(cob_prev, alcanzado) if (contiguo and cob_prev) else alcanzado
        ult = max(x for x in (ult_prev, nuevo_max) if x) if (ult_prev or nuevo_max) else None
        meta = {"cobertura_desde": cob.isoformat() if cob else None,
                "ultimo_updated": ult.isoformat() if ult else None,
                "ultima_ejecucion": ahora.isoformat(), "modo": modo, "completo": terminado}
        guardar_estado(a.estado, estado, meta)
        print(f"Historial guardado: {len(estado)} licitaciones. Cobertura desde {meta['cobertura_desde']}.",
              file=sys.stderr, flush=True)

    print(f"Entradas leídas: {len(entradas)}", file=sys.stderr)
    diagnostico(entradas, a)
    universo = dict(estado)
    for d in entradas.values():
        if d["id"] not in universo or d["actualizado"] >= universo[d["id"]]["actualizado"]:
            universo[d["id"]] = d
    sel = [d for d in universo.values() if cumple(d, a)]
    for d in sel:
        d["desarrollo"] = es_desarrollo(d)
    orden = {"si": 0, "quizá": 1, "no": 2}
    sel.sort(key=lambda d: (orden[d["desarrollo"]], d["fin_plazo"] or "9999"))
    campos = ["desarrollo", "expediente", "objeto", "organo", "importe_sin_iva", "valor_estimado", "cpv", "lugar",
              "lugar_nuts", "tipo_contrato", "procedimiento_cod", "estado", "fin_plazo", "actualizado", "enlace"]
    with open(a.salida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(sel)
    print(f"Licitaciones que cumplen los filtros: {len(sel)} -> {a.salida}", file=sys.stderr)


if __name__ == "__main__":
    main()
