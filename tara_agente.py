"""Tara con herramientas: Claude decide qué consultar y el código hace las cuentas.

Claude (Haiku) recibe la pregunta y llama herramientas de solo lectura sobre el Sheet
(contar, listar, detalle, comparar). Las cifras de la respuesta deben salir de esas
consultas: si aparece una que no, se le pide corregir y, si insiste, se usa el flujo de siempre.

Se usa desde tara_consultas.responder:  tara_agente.responder(texto, hist, hoy)
"""
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

import requests

from reporte import MESES, _fecha, _hora

N = None                      # módulo principal (app.py); lo asigna tara_consultas.registrar
MAX_VUELTAS = 6               # llamadas a Claude por pregunta
MAX_LISTA = 20
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_NOMEN = re.compile(r"\b[A-Z]{2}-[A-Z]{2,4}-P?\d{2,4}(?:-\d{1,3})?\b")
AGRUPAR = ["proyecto", "mes", "semana", "dia", "dia_semana", "accion", "categoria", "dependencia",
           "estado", "municipio", "nucleo", "frente", "tipo_propiedad", "solicitante", "nomenclatura"]


def _n(s):
    return re.sub(r"\s+", " ", N._sin_acentos(str(s or "")).lower()).strip()


# =========================
# FILAS DEL SHEET, YA PREPARADAS
# =========================
def _filas(crudas=None):
    """Filas del Sheet con fecha, clave de proyecto y campos calculados (acciones, categorías, dependencias)."""
    inv = {v: k for k, v in N.mapa_proyectos.items()}
    salida = []
    for i, f in enumerate(crudas if crudas is not None else N._filas_hoja_principal()):
        d = _fecha(f)
        if d is None:
            continue
        tipo = (f.get("TIPO DE SOLICITUD") or "").strip()
        detalle = (f.get("ACTIVIDADES DESARROLLADAS") or "").strip()
        deps_celda = N._sin_acentos(str(f.get("DEPENDENCIAS PARTICIPANTES", "")).upper())
        texto = " ".join(str(f.get(k) or "") for k in (
            "TIPO DE SOLICITUD", "ACTIVIDADES DESARROLLADAS", "NÚCLEO AGRARIO", "EJIDO", "MUNICIPIO", "ESTADO",
            "PROPIETARIOS PROPIEDAD PRIVADA", "POLÍGONO", "DEPENDENCIAS PARTICIPANTES"))
        salida.append({
            "id": i + 2,                                   # fila en el Sheet (con encabezado en la 1)
            "fecha": d, "hora": _hora(f) or "",
            "proyecto": inv.get((f.get("PROYECTO FERROVIARIO") or "").strip(), "OTRO"),
            "actividad": tipo, "detalle": detalle,
            "estado": (f.get("ESTADO") or "").strip(), "municipio": (f.get("MUNICIPIO") or "").strip(),
            "nucleo": (f.get("NÚCLEO AGRARIO") or f.get("EJIDO") or "").strip(),
            "frente": (f.get("FRENTE") or "").strip(), "tipo_propiedad": (f.get("TIPO DE PROPIEDAD") or "").strip(),
            "solicitante": (f.get("SOLICITANTE") or "").strip(),
            "dependencias_txt": (f.get("DEPENDENCIAS PARTICIPANTES") or "").strip(),
            "propietarios": (f.get("PROPIETARIOS PROPIEDAD PRIVADA") or "").strip(),
            "ubicacion": (f.get("UBICACIÓN") or "").strip(),
            "acciones": N.acciones_en_texto(tipo),
            "categorias": sorted({re.sub(r"^\d+\.\s*", "", c) for c, _ in N.clasificar_actividad(tipo, detalle)}),
            "dependencias": [c for c, pats in N._DEP_PATRONES.items() if any(p.search(deps_celda) for p in pats)],
            "nomenclaturas": sorted(set(_NOMEN.findall(texto.upper()))),
            "_texto": _n(texto),
        })
    return salida


def _a_fecha(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Fecha inválida: {s} (usa AAAA-MM-DD)")


def _filtrar(filas, f):
    """Aplica los filtros comunes de todas las herramientas."""
    f = f or {}
    desde, hasta = _a_fecha(f.get("desde")), _a_fecha(f.get("hasta"))
    proys = {p.upper() for p in f.get("proyectos") or []}
    acciones = {_n(a) for a in f.get("acciones") or []}
    cats = [_n(c) for c in f.get("categorias") or []]
    deps = {d.upper() for d in f.get("dependencias") or []}
    palabras = [_n(w) for w in (f.get("texto") or "").split() if len(w) > 1]
    salida = []
    for r in filas:
        if desde and r["fecha"] < desde or hasta and r["fecha"] > hasta:
            continue
        if proys and r["proyecto"] not in proys:
            continue
        if acciones and not acciones & {_n(a) for a in r["acciones"]}:
            continue
        if cats and not any(c in _n(x) or _n(x).startswith(c) for c in cats for x in r["categorias"]):
            continue
        if deps and not deps <= {d.upper() for d in r["dependencias"]}:
            continue
        if f.get("estado") and _n(f["estado"]) not in _n(r["estado"]):
            continue
        if f.get("municipio") and _n(f["municipio"]) not in _n(r["municipio"]):
            continue
        if f.get("nucleo") and _n(f["nucleo"]) not in _n(r["nucleo"]) and _n(f["nucleo"]) not in r["_texto"]:
            continue
        if f.get("frente") and re.sub(r"\D", "", str(f["frente"])) not in re.findall(r"\d+", r["frente"]):
            continue
        if f.get("tipo_propiedad") and _n(f["tipo_propiedad"]) not in _n(r["tipo_propiedad"]):
            continue
        if f.get("nomenclatura") and str(f["nomenclatura"]).upper() not in " ".join(r["nomenclaturas"]):
            continue
        if palabras and not all(w in r["_texto"] for w in palabras):
            continue
        salida.append(r)
    return salida


def _claves(r, campo):
    """Valores de una fila para agrupar (una fila puede caer en varios, p. ej. dependencias)."""
    d = r["fecha"]
    if campo == "mes":
        return [f"{MESES[d.month - 1]} {d.year}"]
    if campo == "semana":
        lunes = d - timedelta(days=d.weekday())
        return [f"semana del {lunes.strftime('%d/%m/%Y')}"]
    if campo == "dia":
        return [d.strftime("%d/%m/%Y")]
    if campo == "dia_semana":
        return [DIAS[d.weekday()]]
    if campo == "accion":
        return r["acciones"] or ["(sin acción reconocida)"]
    if campo == "categoria":
        return r["categorias"] or ["Sin clasificar"]
    if campo == "dependencia":
        return r["dependencias"] or ["(sin dependencia)"]
    if campo == "nomenclatura":
        return r["nomenclaturas"]
    v = r.get(campo, "")
    return [N._sin_acentos(v).upper().strip() if campo in ("estado", "municipio", "nucleo") else v or "(vacío)"]


def _agrupar(filas, campo, top=15):
    if campo not in AGRUPAR:
        raise ValueError(f"agrupar_por debe ser uno de: {', '.join(AGRUPAR)}")
    c = Counter(k for r in filas for k in dict.fromkeys(_claves(r, campo)))
    if not c:
        return {"grupos": [], "grupos_distintos": 0}
    if campo in ("mes", "semana", "dia"):                  # en orden de calendario y completos
        clave = (lambda kv: (int(kv[0].split()[-1]), MESES.index(kv[0].split()[0]))) if campo == "mes" \
            else (lambda kv: datetime.strptime(kv[0][-10:], "%d/%m/%Y"))
        orden, top = sorted(c.items(), key=clave), 60
    else:
        orden = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    tope = max(c.values())
    empatados = [k for k, v in orden if v == tope]           # todos los que empatan en el máximo
    return {"grupos": [{"valor": k, "n": v} for k, v in orden[:top]], "grupos_distintos": len(c),
            "mayor": {"valor": ", ".join(empatados), "n": tope, "empates": len(empatados)}}


def _corta(r):
    return {"id": r["id"], "fecha": r["fecha"].strftime("%d/%m/%Y"), "dia": DIAS[r["fecha"].weekday()],
            "hora": r["hora"], "proyecto": r["proyecto"], "actividad": r["actividad"][:220],
            "lugar": ", ".join(x for x in (r["nucleo"], r["municipio"], r["estado"]) if x),
            "dependencias": r["dependencias_txt"][:120]}


# =========================
# HERRAMIENTAS
# =========================
def h_contar(filas, filtros=None, agrupar_por=None, top=15):
    sel = _filtrar(filas, filtros)
    res = {"total": len(sel)}
    if sel:
        res["primera_fecha"] = min(r["fecha"] for r in sel).strftime("%d/%m/%Y")
        res["ultima_fecha"] = max(r["fecha"] for r in sel).strftime("%d/%m/%Y")
    if agrupar_por and sel:
        res.update(_agrupar(sel, agrupar_por, min(int(top or 15), 40)))
    return res


def h_listar(filas, filtros=None, orden="reciente", limite=10):
    sel = _filtrar(filas, filtros)
    sel.sort(key=lambda r: (r["fecha"], r["hora"] or "99:99"), reverse=(orden != "antiguo"))
    limite = max(1, min(int(limite or 10), MAX_LISTA))
    return {"total": len(sel), "mostradas": min(limite, len(sel)), "actividades": [_corta(r) for r in sel[:limite]]}


def h_detalle(filas, ids):
    por_id = {r["id"]: r for r in filas}
    salida = []
    for i in list(ids or [])[:5]:
        r = por_id.get(int(i))
        if r:
            d = _corta(r)
            d.update({"desarrollo": r["detalle"][:800], "frente": r["frente"], "tipo_propiedad": r["tipo_propiedad"],
                      "propietarios": r["propietarios"][:300], "ubicacion": r["ubicacion"],
                      "solicitante": r["solicitante"], "nomenclaturas": r["nomenclaturas"],
                      "acciones": r["acciones"], "categorias": r["categorias"]})
            salida.append(d)
    return {"actividades": salida, "no_encontradas": [i for i in ids or [] if int(i) not in por_id]}


def h_comparar(filas, periodo_a, periodo_b, filtros=None, agrupar_por=None):
    res = {}
    for nombre, p in (("a", periodo_a), ("b", periodo_b)):
        f = dict(filtros or {}, desde=p.get("desde"), hasta=p.get("hasta"))
        res[nombre] = h_contar(filas, f, agrupar_por)
        res[nombre]["periodo"] = f"{p.get('desde')} a {p.get('hasta')}"
    a, b = res["a"]["total"], res["b"]["total"]
    res["diferencia_b_menos_a"] = b - a
    res["cambio_porcentual"] = round((b - a) * 100 / a, 1) if a else None
    return res


HERRAMIENTAS = {"contar": h_contar, "listar": h_listar, "detalle": h_detalle, "comparar": h_comparar}

_FILTROS = {"type": "object", "description": "Filtros opcionales; todos se combinan (Y).", "properties": {
    "desde": {"type": "string", "description": "AAAA-MM-DD"}, "hasta": {"type": "string", "description": "AAAA-MM-DD"},
    "proyectos": {"type": "array", "items": {"type": "string"}, "description": "Claves de tramo: TMQ, TAP, TQI…"},
    "acciones": {"type": "array", "items": {"type": "string"}, "description": "Acciones del catálogo (Reunión, Firma, Sensibilización…)"},
    "categorias": {"type": "array", "items": {"type": "string"}, "description": "Categorías (Levantamientos, Firmas…)"},
    "dependencias": {"type": "array", "items": {"type": "string"}, "description": "Siglas: SICT, INDAABIN, INAH…"},
    "estado": {"type": "string"}, "municipio": {"type": "string"},
    "nucleo": {"type": "string", "description": "Ejido o núcleo agrario"},
    "frente": {"type": "string", "description": "Número de frente"},
    "tipo_propiedad": {"type": "string", "description": "SOCIAL o PRIVADA"},
    "nomenclatura": {"type": "string", "description": "Polígono, p. ej. MQ-COL-005"},
    "texto": {"type": "string", "description": "Palabras que deben aparecer en el texto de la actividad (nombres, PK, lugares)"}}}

DEF_HERRAMIENTAS = [
    {"name": "contar", "description": "Cuenta actividades que cumplen los filtros y, si se pide, las agrupa "
     "(rankings: qué tramo, ejido, dependencia, mes, día de la semana… tiene más). Devuelve total, primera y última fecha.",
     "input_schema": {"type": "object", "properties": {
         "filtros": _FILTROS, "agrupar_por": {"type": "string", "enum": AGRUPAR},
         "top": {"type": "integer", "description": "Cuántos grupos devolver (máx. 40)"}}}},
    {"name": "listar", "description": "Lista actividades (fecha, hora, tramo, texto, lugar, dependencias, id). "
     "Úsala para «cuáles», «la última», «la primera», «qué hay programado».",
     "input_schema": {"type": "object", "properties": {
         "filtros": _FILTROS, "orden": {"type": "string", "enum": ["reciente", "antiguo"]},
         "limite": {"type": "integer", "description": f"Máx. {MAX_LISTA}"}}}},
    {"name": "detalle", "description": "Todo lo registrado de actividades por su id (desarrollo, propietarios, ubicación/liga, "
     "frente, nomenclaturas). Úsala después de listar cuando pidan acuerdos, dónde fue o quiénes.",
     "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
                      "required": ["ids"]}},
    {"name": "comparar", "description": "Compara dos periodos con los mismos filtros (totales, diferencia, % y grupos).",
     "input_schema": {"type": "object", "properties": {
         "periodo_a": {"type": "object", "properties": {"desde": {"type": "string"}, "hasta": {"type": "string"}}},
         "periodo_b": {"type": "object", "properties": {"desde": {"type": "string"}, "hasta": {"type": "string"}}},
         "filtros": _FILTROS, "agrupar_por": {"type": "string", "enum": AGRUPAR}},
         "required": ["periodo_a", "periodo_b"]}},
]


def _sistema(hoy):
    proyectos = "; ".join(f"{k} = {v}" for k, v in N.mapa_proyectos.items())
    acciones = ", ".join(a for _, acc in N.CATALOGO_ACTIVIDADES for a, _ in acc)
    categorias = ", ".join(re.sub(r"^\d+\.\s*", "", c) for c, _ in N.CATALOGO_ACTIVIDADES)
    return (
        "Eres Tara, la asistente de la agenda del Proyecto Ferroviario en la DGT (Dirección de Gestión Territorial, RAN). "
        "Respondes por Telegram en español de México, con calidez y al grano: texto plano, sin Markdown ni tablas; "
        "si piden una lista, usa guiones.\n"
        f"Hoy es {DIAS[hoy.weekday()]} {hoy.strftime('%d/%m/%Y')}. Semanas de lunes a domingo; «este año» va del 1 de enero a hoy.\n"
        f"Tramos (proyecto): {proyectos}.\nCategorías: {categorias}.\nAcciones: {acciones}.\n"
        "Reglas:\n"
        "- Para cualquier dato de la agenda USA LAS HERRAMIENTAS. Nunca inventes actividades, fechas, lugares, personas ni cifras.\n"
        "- Toda cifra que escribas debe venir tal cual de un resultado de herramienta; no calcules tú (para diferencias usa «comparar»).\n"
        "- Puedes llamar varias herramientas: primero ubica (listar/contar) y luego profundiza (detalle).\n"
        "- Si un nombre puede ser ejido, municipio o texto, prueba con «nucleo» y si no hay resultados con «texto».\n"
        "- «La última», «la más reciente» o «cuándo fue»: filtra hasta hoy. «Programado», «próxima», «qué sigue»: desde hoy.\n"
        "- Responde en un párrafo corto (o lista si la piden). Al final agrega una línea: «Consulté: <periodo> · <filtros>».\n"
        "- Si no hay resultados, dilo y sugiere cómo preguntar. Si la pregunta es ambigua, elige lo más razonable y dilo."
    )


def _llamar(clave, mensajes, sistema):
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=40, headers={
        "x-api-key": clave, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": N.HAIKU_MODEL, "max_tokens": 1200, "system": sistema,
              "tools": DEF_HERRAMIENTAS, "messages": mensajes})
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {r.text[:200]}")
    return r.json()


def _cifras(texto):
    return set(re.findall(r"\d+", texto))


def responder(pregunta, hist="", hoy=None, filas_sheet=None):
    """Respuesta del agente, o None si no se pudo (sin clave, error o cifras sin respaldo)."""
    clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not clave or N is None:
        return None
    hoy = hoy or date.today()
    inicio = time.time()
    try:
        filas = _filas(filas_sheet)
        sistema = _sistema(hoy)
        contenido = (f"Conversación previa:\n{hist}\n\n" if hist else "") + f"Pregunta: {pregunta}"
        mensajes = [{"role": "user", "content": contenido}]
        respaldo = _cifras(pregunta + " " + hist + " " + hoy.strftime("%d/%m/%Y %Y-%m-%d"))
        usadas, corregido = 0, False
        for _ in range(MAX_VUELTAS):
            r = _llamar(clave, mensajes, sistema)
            bloques = r.get("content", [])
            llamadas = [b for b in bloques if b.get("type") == "tool_use"]
            if llamadas:
                mensajes.append({"role": "assistant", "content": bloques})
                resultados = []
                for b in llamadas:
                    try:
                        salida = HERRAMIENTAS[b["name"]](filas, **(b.get("input") or {}))
                    except Exception as e:
                        salida = {"error": str(e)}
                    texto = json.dumps(salida, ensure_ascii=False, default=str)
                    respaldo |= _cifras(texto)
                    usadas += 1
                    resultados.append({"type": "tool_result", "tool_use_id": b["id"], "content": texto[:12000]})
                mensajes.append({"role": "user", "content": resultados})
                continue
            texto = "".join(b.get("text", "") for b in bloques).strip()
            if not texto:
                return None
            if usadas == 0:                                   # contestó sin consultar: no se confía
                print("[tara-agente] respondió sin usar herramientas; se usa el flujo de siempre", flush=True)
                return None
            extra = sorted(_cifras(texto) - respaldo)
            if extra and not corregido:                       # una oportunidad para corregir
                corregido = True
                mensajes.append({"role": "assistant", "content": bloques})
                mensajes.append({"role": "user", "content":
                                 f"Estas cifras no salen de las consultas: {', '.join(extra[:10])}. "
                                 "Corrige la respuesta usando solo cifras de los resultados (o consulta lo que falte)."})
                continue
            if extra:
                print(f"[tara-agente] cifras sin respaldo: {extra[:5]}; se usa el flujo de siempre", flush=True)
                return None
            print(f"[tara-agente] ok en {time.time() - inicio:.1f}s con {usadas} consultas", flush=True)
            return re.sub(r"(?m)\*\*|^#+\s*", "", texto)[:3800]
        return None
    except Exception as e:
        print(f"[tara-agente] {type(e).__name__}: {e}", flush=True)
        return None
