#!/usr/bin/env python3
"""
Vigila tres fuentes de datos abiertos de la Plataforma de Contratación (PLACSP) que el
buscador principal no cubre:

  1. Contratos menores adjudicados (sindicación 1143): quién compra software y a quién.
     -> contratos_menores_software.csv   (toda España, software/TIC)
     -> ranking_menores_galicia.csv      (órganos gallegos que más contratan software por menor)
  2. Consultas preliminares de mercado (sindicación 1403): la administración pide propuestas
     antes de licitar. Cualquiera puede participar, sin solvencia.
     -> consultas_preliminares_tic.csv
  3. Licitaciones de plataformas autonómicas agregadas (sindicación 1044: Galicia, Madrid,
     Cataluña, Euskadi, Andalucía...) que no salen en el feed principal.
     -> licitaciones_autonomicas.csv      (abiertas, TIC, hasta --max-importe)

Guarda el histórico filtrado en estado_extra.json para trabajar de forma incremental.
Solo usa la librería estándar y reutiliza funciones de buscar_licitaciones.py.
"""
import argparse, collections, csv, datetime as dt, io, json, re, sys, time
import xml.etree.ElementTree as ET

import buscar_licitaciones as bl

FEEDS = {
    "menores": "https://contrataciondelestado.es/sindicacion/sindicacion_1143/contratosMenoresPerfilesContratantes.atom",
    "cpm": "https://contrataciondelestado.es/sindicacion/sindicacion_1403/CPM_SectorPublico.atom",
    "agregadas": "https://contrataciondelestado.es/sindicacion/sindicacion_1044/PlataformasAgregadasSinMenores.atom",
}

CPV_TIC = ("72", "48")
PALABRAS_TIC = re.compile(
    r"software|aplicaci[oó]n(es)? (inform[aá]tica|web|m[oó]vil)|aplicativo|\bapps?\b|p[aá]x?gina web|portal web|"
    r"sitio web|p[aá]xina web|desarroll\w* (de |dun |de un |dunha |de una )?(software|aplicaci|web|app|"
    r"plataforma|portal|m[oó]dulo|conector|herramienta inform|sistema)|programaci[oó]n inform|sede electr|"
    r"licencias? (de |do |del )?software|mantemento (da |do )?(aplicaci|plataforma|software)|"
    r"mantenimiento (de |del )?(la )?(aplicaci|plataforma|software|portal)|plataforma (digital|web|tecnol|electr)|"
    r"intranet|cuadros? de mando|datos abiertos|datos abertos|inteligencia artificial|\bIA\b|\bAPI\b", re.I)
FORMAS_JURIDICAS = re.compile(
    r"\b(s\.?\s?l\.?\s?u?\.?|s\.?\s?a\.?\s?u?\.?|sociedad|s\.?\s?coop|cooperativa|ltd|gmbh|b\.?v\.?|"
    r"s\.?\s?l\.?\s?l\.?|asociaci[oó]n|fundaci[oó]n|consorcio|universidad|ute\b|"
    r"slp|s\.?\s?c\.?|c\.?\s?b\.?|limited|inc\b|sarl|sas\b|spa\b|consulting|soluciones|tecnolog|systems|software)",
    re.I)
FIN_PALABRA = re.compile(r"(?=$|[\s,.;:)\]-])")


def ahora():
    return dt.datetime.now(dt.timezone.utc)


def textos(el, nombre):
    return [(e.text or "").strip() for e in bl.descendientes(el, nombre) if (e.text or "").strip()]


def nombre_adjudicatario(nombre):
    """No guarda nombres de personas físicas (el repositorio es público)."""
    if not nombre:
        return ""
    for m in FORMAS_JURIDICAS.finditer(nombre):
        if FIN_PALABRA.match(nombre, m.end()) or len(m.group(0)) > 5:
            return nombre
    return "(persona física)"


def procesar(entry, fuente):
    """Extrae un registro de un <entry>. Tolerante con estructuras distintas entre feeds."""
    d = bl.procesar_entry(entry)
    if d is None:   # estructura distinta (p. ej. consultas preliminares): extracción genérica
        d = {"id": bl.texto(entry, "id"), "actualizado": bl.texto(entry, "updated"),
             "titulo": bl.texto(entry, "title"), "enlace": "", "expediente": "", "estado": "",
             "organo": "", "objeto": bl.texto(entry, "title"), "tipo_contrato": "",
             "importe_sin_iva": "", "valor_estimado": "", "lugar_nuts": "", "lugar": "",
             "procedimiento_cod": "", "fin_plazo": "", "docs": []}
        for l in bl.hijos(entry, "link"):
            d["enlace"] = l.get("href", "")
            break
        d["cpv"] = ",".join(sorted(set(textos(entry, "ItemClassificationCode"))))
        nombres = textos(entry, "Name")
        d["organo"] = nombres[0] if nombres else ""
        d["lugar_nuts"] = ",".join(sorted(set(textos(entry, "CountrySubentityCode"))))
        ids = textos(entry, "ContractFolderID")
        d["expediente"] = ids[0] if ids else ""
        fechas = textos(entry, "EndDate")
        d["fin_plazo"] = fechas[0] if fechas else ""
    d.pop("docs", None)
    d["fuente"] = fuente
    d["resumen"] = bl.texto(entry, "summary")[:400]
    # Datos de adjudicación (contratos menores)
    ganadores = []
    for wp in bl.descendientes(entry, "WinningParty"):
        n = bl.texto(wp, "PartyName", "Name")
        if n:
            ganadores.append(nombre_adjudicatario(n))
    d["adjudicatario"] = " | ".join(dict.fromkeys(ganadores))
    importes = []
    for at in bl.descendientes(entry, "AwardedTenderedProject"):
        v = bl.texto(at, "LegalMonetaryTotal", "TaxExclusiveAmount")
        if v:
            importes.append(v)
    d["importe_adjudicado"] = importes[0] if importes else ""
    fechas = textos(entry, "AwardDate")
    d["fecha_adjudicacion"] = fechas[0] if fechas else ""
    return d


def es_tic(d):
    """TIC si algún CPV es 72 (servicios TI) o 48 (software); si no, solo con palabras claras de software."""
    cpvs = [c for c in d.get("cpv", "").split(",") if c]
    if any(c.startswith(CPV_TIC) for c in cpvs):
        return True
    return bool(PALABRAS_TIC.search(f"{d.get('objeto', '')} {d.get('titulo', '')}"))


def importe(d):
    for k in ("importe_adjudicado", "importe_sin_iva", "valor_estimado"):
        try:
            return float(d.get(k) or "")
        except ValueError:
            continue
    return None


def leer_feed(nombre, url, guardadas, meta, a, limite_tiempo):
    """Recorre el feed desde lo más nuevo hasta lo ya visto (o hasta --dias atrás)."""
    m = meta.setdefault(nombre, {})
    desde = bl.parse_dt(m.get("ultimo_updated", "")) or (ahora() - dt.timedelta(days=a.dias))
    nuevo_max = m.get("ultimo_updated", "")
    siguiente, paginas, leidas, relevantes = url, 0, 0, 0
    completo = False
    mas_antigua_total = None
    while siguiente and paginas < a.paginas:
        if time.time() > limite_tiempo:
            print(f"[{nombre}] tiempo agotado tras {paginas} páginas", file=sys.stderr)
            break
        try:
            datos = bl.descargar(siguiente)
        except Exception as e:
            print(f"[{nombre}] no se pudo descargar {siguiente}: {e}", file=sys.stderr)
            break
        paginas += 1
        sig, mas_antigua = None, None
        for _, el in ET.iterparse(io.BytesIO(datos), events=("end",)):
            t = bl.local(el.tag)
            if t == "entry":
                d = procesar(el, nombre)
                el.clear()
                leidas += 1
                act = bl.parse_dt(d["actualizado"])
                if act and (mas_antigua is None or act < mas_antigua):
                    mas_antigua = act
                if d["actualizado"] > nuevo_max:
                    nuevo_max = d["actualizado"]
                if not es_tic(d):
                    continue
                clave = f"{nombre}:{d['id']}"
                previo = guardadas.get(clave)
                if previo is None or d["actualizado"] >= previo["actualizado"]:
                    guardadas[clave] = d
                    relevantes += 1
            elif t == "link" and el.get("rel") == "next":
                sig = el.get("href")
        if paginas == 1:
            print(f"[{nombre}] primera página: {leidas} entradas", file=sys.stderr, flush=True)
        if mas_antigua and (mas_antigua_total is None or mas_antigua < mas_antigua_total):
            mas_antigua_total = mas_antigua
        if mas_antigua and mas_antigua < desde:
            completo = True
            break
        siguiente = sig
        if paginas % 10 == 0:
            print(f"[{nombre}] {paginas} páginas, {leidas} entradas, {relevantes} TIC, "
                  f"llegando a {mas_antigua}", file=sys.stderr, flush=True)
    enlazado = completo or not siguiente
    if nuevo_max:
        # Avanzamos siempre: lo leído cubre desde lo más nuevo hasta `mas_antigua_total`.
        # Si no hemos enlazado con la lectura anterior queda un hueco, que se anota.
        if not enlazado and m.get("ultimo_updated"):
            m.setdefault("huecos", []).append([m["ultimo_updated"], str(mas_antigua_total)])
        if not m.get("cobertura_desde") or not m.get("ultimo_updated"):
            m["cobertura_desde"] = str(mas_antigua_total)
        m["ultimo_updated"] = nuevo_max
    m["ultima_ejecucion"] = ahora().isoformat(timespec="seconds")
    m["ultima_lectura"] = {"paginas": paginas, "entradas": leidas, "tic_nuevas": relevantes,
                           "enlazado": enlazado}
    print(f"[{nombre}] fin: {paginas} páginas, {leidas} entradas, {relevantes} TIC nuevas/actualizadas, "
          f"enlazado={enlazado}", file=sys.stderr, flush=True)


def escribir_csv(ruta, filas, columnas):
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columnas, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    print(f"  {ruta}: {len(filas)} filas", file=sys.stderr)


def galicia(d):
    return "ES11" in d.get("lugar_nuts", "") or re.search(
        r"galicia|coruña|corunha|lugo|ourense|pontevedra|vigo|santiago de compostela|ferrol",
        f"{d.get('organo', '')} {d.get('lugar', '')}", re.I)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fuentes", default="menores,cpm,agregadas")
    ap.add_argument("--estado", default="estado_extra.json")
    ap.add_argument("--dias", type=int, default=120, help="días hacia atrás en la primera lectura (def. 120)")
    ap.add_argument("--paginas", type=int, default=300, help="máximo de páginas por fuente")
    ap.add_argument("--max-minutos", type=float, default=60, help="tiempo total máximo")
    ap.add_argument("--max-importe", type=float, default=60000)
    a = ap.parse_args()

    try:
        with open(a.estado, encoding="utf-8") as f:
            j = json.load(f)
    except (OSError, ValueError):
        j = {}
    guardadas = j.get("entradas", {})
    meta = j.get("meta", {})

    fuentes = [s.strip() for s in a.fuentes.split(",") if s.strip() in FEEDS]
    inicio = time.time()
    total = a.max_minutos * 60
    reparto = {"menores": 0.55, "cpm": 0.15, "agregadas": 0.30}
    acumulado = 0.0
    for nombre in fuentes:
        acumulado += reparto.get(nombre, 1 / len(fuentes))
        leer_feed(nombre, FEEDS[nombre], guardadas, meta, a, inicio + total * acumulado)
        with open(a.estado, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "entradas": guardadas}, f, ensure_ascii=False, separators=(",", ":"))

    hoy = ahora()
    limite_hist = hoy - dt.timedelta(days=400)
    for k in [k for k, d in guardadas.items() if (bl.parse_dt(d["actualizado"]) or hoy) < limite_hist]:
        del guardadas[k]

    todas = [d for d in guardadas.values() if es_tic(d)]
    for d in todas:
        if d.get("adjudicatario"):
            d["adjudicatario"] = " | ".join(nombre_adjudicatario(x) if x != "(persona física)" else x
                                            for x in d["adjudicatario"].split(" | "))
    # 1. Contratos menores de software
    menores = sorted((d for d in todas if d["fuente"] == "menores"),
                     key=lambda d: d.get("fecha_adjudicacion") or d["actualizado"], reverse=True)
    for d in menores:
        d["importe"] = importe(d) or ""
        d["galicia"] = "si" if galicia(d) else ""
    escribir_csv("contratos_menores_software.csv", menores,
                 ["galicia", "fecha_adjudicacion", "organo", "objeto", "importe", "adjudicatario",
                  "cpv", "lugar_nuts", "lugar", "expediente", "actualizado", "enlace"])
    rk = collections.defaultdict(lambda: {"n": 0, "total": 0.0, "adj": set(), "ultima": "", "ejemplos": []})
    for d in menores:
        if not d["galicia"]:
            continue
        r = rk[d["organo"] or "(sin órgano)"]
        r["n"] += 1
        r["total"] += importe(d) or 0
        if d["adjudicatario"]:
            r["adj"].add(d["adjudicatario"])
        r["ultima"] = max(r["ultima"], d.get("fecha_adjudicacion") or d["actualizado"][:10])
        if len(r["ejemplos"]) < 3:
            r["ejemplos"].append(d["objeto"][:90])
    ranking = [{"organo": o, "contratos": r["n"], "importe_total": round(r["total"], 2),
                "proveedores_distintos": len(r["adj"]), "ultima_adjudicacion": r["ultima"],
                "proveedores": " / ".join(sorted(r["adj"]))[:300], "ejemplos": " / ".join(r["ejemplos"])}
               for o, r in rk.items()]
    ranking.sort(key=lambda x: (-x["contratos"], -x["importe_total"]))
    escribir_csv("ranking_menores_galicia.csv", ranking,
                 ["organo", "contratos", "importe_total", "proveedores_distintos", "ultima_adjudicacion",
                  "proveedores", "ejemplos"])
    # 2. Consultas preliminares de mercado TIC
    cpm = sorted((d for d in todas if d["fuente"] == "cpm"), key=lambda d: d["actualizado"], reverse=True)
    for d in cpm:
        d["galicia"] = "si" if galicia(d) else ""
    escribir_csv("consultas_preliminares_tic.csv", cpm,
                 ["galicia", "actualizado", "organo", "objeto", "fin_plazo", "cpv", "lugar_nuts",
                  "expediente", "resumen", "enlace"])
    # 3. Licitaciones autonómicas agregadas abiertas
    abiertas = []
    for d in todas:
        if d["fuente"] != "agregadas" or d.get("estado") != "PUB":
            continue
        fin = bl.parse_dt((d.get("fin_plazo") or "").replace(" ", "T"))
        if fin and fin < hoy:
            continue
        imp = importe(d)
        if imp is not None and imp > a.max_importe:
            continue
        d["importe"] = imp if imp is not None else ""
        d["galicia"] = "si" if galicia(d) else ""
        d["desarrollo"] = bl.es_desarrollo(d)
        abiertas.append(d)
    orden = {"si": 0, "quizá": 1, "no": 2}
    abiertas.sort(key=lambda d: (d["galicia"] != "si", orden.get(d["desarrollo"], 3), d.get("fin_plazo") or "9"))
    escribir_csv("licitaciones_autonomicas.csv", abiertas,
                 ["galicia", "desarrollo", "fin_plazo", "organo", "objeto", "importe", "cpv",
                  "procedimiento_cod", "lugar_nuts", "expediente", "enlace"])

    with open(a.estado, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "entradas": guardadas}, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
