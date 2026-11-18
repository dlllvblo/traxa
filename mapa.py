"""Geovisualizador de actividades.

Pinta cada municipio según cuántas actividades tiene en el rango elegido, usando
las columnas ESTADO y MUNICIPIO del Sheet y la capa INEGI que ya carga app.py.
Cada categoría de Conteo es una capa (mismo criterio de clasificación).

Se conecta desde app.py con:
    import mapa
    mapa.registrar(app, sys.modules[__name__])
"""
import re
from collections import Counter, defaultdict

from flask import jsonify, render_template, request

from reporte import _fecha, _hora, _rango, _fecha_larga

N = None            # módulo principal (app.py)
_IDX_EM = None      # (estado, municipio) normalizados -> índice en la capa INEGI
_IDX_M = None       # municipio normalizado -> [índices] (para cuando falta el estado)
_POR_EDO = None     # estado normalizado -> [(municipio normalizado, índice)]

ALIAS_EDO = {
    "coahuila": "coahuila de zaragoza", "coah": "coahuila de zaragoza",
    "veracruz": "veracruz de ignacio de la llave", "ver": "veracruz de ignacio de la llave",
    "michoacan": "michoacan de ocampo", "mich": "michoacan de ocampo",
    "estado de mexico": "mexico", "edo de mexico": "mexico", "edo mex": "mexico",
    "edomex": "mexico", "mex": "mexico",
    "cdmx": "ciudad de mexico", "distrito federal": "ciudad de mexico", "df": "ciudad de mexico",
    "nl": "nuevo leon", "slp": "san luis potosi", "qro": "queretaro", "gto": "guanajuato",
    "jal": "jalisco", "hgo": "hidalgo", "tamps": "tamaulipas", "sin": "sinaloa",
}

# Nombres de ciudad que mandan en lugar del municipio INEGI.
ALIAS_MUN = {
    "santiago de queretaro": "queretaro",
}

# Texto comparable: sin acentos, puntos ni mayúsculas.
def _norm(s):
    s = N._sin_acentos(str(s or "")).lower()
    s = re.sub(r"[.\"'“”‘’]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _indices():
    global _IDX_EM, _IDX_M, _POR_EDO
    if _IDX_EM is None:
        em, m, por_edo = {}, defaultdict(list), defaultdict(list)
        for i, p in enumerate(N.MUN_PROPS):
            e, mun = _norm(p["NOM_ENT"]), _norm(p["NOM_MUN"])
            em[(e, mun)] = i
            m[mun].append(i)
            por_edo[e].append((mun, i))
        _IDX_EM, _IDX_M, _POR_EDO = em, m, por_edo
    return _IDX_EM, _IDX_M, _POR_EDO


# Busca un nombre de municipio en la capa INEGI; devuelve su índice o None.
def _buscar(estado, nombre):
    em, m, por_edo = _indices()
    nombre = re.sub(r"^(municipios? de|mpio de|mpio)\s+", "", _norm(nombre))
    nombre = ALIAS_MUN.get(nombre, nombre)
    if not nombre:
        return None
    if estado:
        if (estado, nombre) in em:
            return em[(estado, nombre)]
        # Nombre corto de uso común: "Silao" -> "Silao de la Victoria", "Tlaquepaque" -> "San Pedro Tlaquepaque"
        if len(nombre) >= 4:
            parecidos = [i for mun, i in por_edo.get(estado, [])
                         if mun.startswith(nombre + " ") or mun.endswith(" " + nombre)]
            if len(parecidos) == 1:
                return parecidos[0]
        return None
    candidatos = m.get(nombre, [])
    return candidatos[0] if len(candidatos) == 1 else None      # sin estado: solo si el nombre es único en el país


# Nombre de estado normalizado si el texto es un estado («Estado de México» -> «mexico»); si no, "".
def _como_estado(texto):
    t = _norm(texto)
    t = ALIAS_EDO.get(t, t)
    return t if t in _indices()[2] else ""


# Municipios (índices INEGI) a los que corresponde una fila; [] si no se pudo ubicar.
# «Polotitlán, Estado de México; San Juan del Río y Pedro Escobedo, Querétaro»: cada grupo usa su propio estado.
def ubicar(estado, municipio):
    e = _norm(estado)
    e = ALIAS_EDO.get(e, e)
    entero = _buscar(e, municipio)
    if entero is not None:
        return [entero]
    hallados = []
    for grupo in str(municipio or "").split(";"):
        partes = [p for p in re.split(r"\s*[,/]\s*|\s+y\s+|\s+e\s+", grupo) if p.strip()]
        edo_grupo = _como_estado(partes[-1]) if len(partes) >= 2 else ""
        if edo_grupo:
            partes = partes[:-1]                   # el último es el estado del grupo
        for p in partes:
            i = _buscar(edo_grupo or e, p)
            if i is None and not edo_grupo:
                i = _buscar("", p)                 # el ESTADO de la fila no cuadra: nombre único en el país
            if i is not None and i not in hallados:
                hallados.append(i)
    return hallados

def _sin_numero(nombre):
    return re.sub(r"^\d+\.\s*", "", nombre)


# Capas del geovisualizador en el orden de Conteo: categorías, subcategorías de la 3 y sin clasificar.
def _capas_base():
    cats = sorted([n for n, _ in N.CATEGORIAS_CONTEO] + [N.CAT3_NOMBRE],
                  key=lambda n: int(re.match(r"\d+", n).group()))
    subs = [n for n, _ in N.CAT3_SUB] + [N.CAT3_GENERAL[0]]
    capas = []
    for n in cats:
        capas.append({"clave": _sin_numero(n), "nombre": _sin_numero(n), "tipo": "categoria"})
        if n == N.CAT3_NOMBRE:
            capas += [{"clave": "sub|" + sn, "nombre": sn, "tipo": "sub"} for sn in subs]
    capas.append({"clave": "sin", "nombre": "Sin clasificar", "tipo": "sin"})
    return capas


# Claves de capa de una actividad (mismo criterio que la pantalla de Conteo).
def _capas_de(f):
    hits = N.clasificar_actividad(f.get("TIPO DE SOLICITUD", ""), f.get("ACTIVIDADES DESARROLLADAS", ""))
    if not hits:
        return ["sin"]
    claves = []
    for cat, sub in hits:
        claves.append(_sin_numero(cat))
        if sub:
            claves.append("sub|" + sub)
    return claves


# Lee el Sheet y arma el mapa para el rango (y proyecto) pedidos.
def datos(desde, hasta, proyecto=""):
    inv = {v: k for k, v in N.mapa_proyectos.items()}
    por_mun = defaultdict(lambda: {"n": 0, "proy": Counter(), "acts": [], "capas": Counter()})
    por_proy, no_ubicados, por_capa = Counter(), Counter(), Counter()
    total = ubicadas = 0

    for f in N._filas_hoja_principal():
        d = _fecha(f)
        if d is None or d < desde or d > hasta:
            continue
        clave = inv.get((f.get("PROYECTO FERROVIARIO") or "").strip(), "OTRO")
        por_proy[clave] += 1                       # para el selector: cuenta antes de filtrar por proyecto
        if proyecto and clave != proyecto:
            continue
        total += 1
        capas = _capas_de(f)
        por_capa.update(capas)
        estado, municipio = (f.get("ESTADO") or "").strip(), (f.get("MUNICIPIO") or "").strip()
        idxs = ubicar(estado, municipio)
        if not idxs:
            etiqueta = ", ".join(x for x in (municipio, estado) if x) or "(sin municipio ni estado)"
            no_ubicados[etiqueta] += 1
            continue
        ubicadas += 1
        for i in idxs:
            m = por_mun[i]
            m["n"] += 1
            m["proy"][clave] += 1
            m["capas"].update(capas)
            m["acts"].append((d, _hora(f), clave, (f.get("TIPO DE SOLICITUD") or "").strip(), set(capas)))

    municipios = []
    for i, m in sorted(por_mun.items(), key=lambda kv: -kv[1]["n"]):
        p, g = N.MUN_PROPS[i], N.MUN_GEOMS[i]
        ordenadas = sorted(m["acts"], key=lambda a: (a[0], a[1]), reverse=True)
        fmt = lambda a: {"fecha": a[0].strftime("%d/%m/%Y"), "hora": a[1], "proyecto": a[2], "actividad": a[3]}
        recientes = {"todas": [fmt(a) for a in ordenadas[:5]]}
        for c in m["capas"]:                     # las 5 más recientes de cada capa
            recientes[c] = [fmt(a) for a in ordenadas if c in a[4]][:5]
        punto = g.representative_point()
        municipios.append({
            "id": i, "cvegeo": p["CVEGEO"], "municipio": p["NOM_MUN"], "estado": p["NOM_ENT"], "n": m["n"],
            "bbox": [round(v, 5) for v in g.bounds], "punto": [round(punto.x, 5), round(punto.y, 5)],
            "proyectos": dict(m["proy"].most_common()),
            "capas": dict(m["capas"]),
            "recientes": recientes,
        })

    return {
        "ventana": {"desde": desde.strftime("%d/%m/%Y"), "hasta": hasta.strftime("%d/%m/%Y"),
                    "desde_iso": desde.isoformat(), "hasta_iso": hasta.isoformat(),
                    "desde_txt": _fecha_larga(desde), "hasta_txt": _fecha_larga(hasta)},
        "proyecto": proyecto,
        "total": total,
        "ubicadas": ubicadas,
        "sin_ubicar": total - ubicadas,
        "no_ubicados": [{"nombre": k, "n": v} for k, v in no_ubicados.most_common(12)],
        "proyectos": [{"clave": k, "n": v} for k, v in por_proy.most_common()],
        "capas": [dict(c, n=por_capa.get(c["clave"], 0)) for c in _capas_base()],
        "municipios": municipios,
    }


# Conecta las rutas del mapa a la app principal.
def registrar(app, nucleo):
    global N
    N = nucleo

    # Página del geovisualizador.
    @app.route("/mapa")
    def mapa_pagina():
        return render_template("mapa.html")

    # API: municipios con actividades en el rango pedido (?desde&hasta&proyecto).
    @app.route("/api/mapa")
    def api_mapa():
        try:
            proyecto = (request.args.get("proyecto") or "").strip().upper()
            if proyecto and proyecto != "OTRO" and proyecto not in N.mapa_proyectos:
                return jsonify({"error": "Proyecto no reconocido"}), 400
            return jsonify(datos(*_rango(), proyecto))
        except ValueError:
            return jsonify({"error": "Fecha inválida en el filtro"}), 400
        except Exception:
            return N._error_api()
