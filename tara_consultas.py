"""Tara responde preguntas sobre la agenda por Telegram.

Las cuentas las hace el código sobre el Sheet, nunca la IA. Cada respuesta dice
qué periodo y qué filtros entendió, para que se pueda verificar de un vistazo.

Se conecta desde app.py con:
    import tara_consultas
    tara_consultas.registrar(app, sys.modules[__name__])
y una línea dentro de _procesar_buffer:
    if tara_consultas.atender(chat_id, texto): return
"""
import hmac
import os

import requests
import random
import re
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from flask import jsonify, request

from reporte import MESES, _fecha, _hora, _titulo

N = None                 # módulo principal (app.py)
MAX_LISTA = 25           # actividades que caben en un mensaje de lista
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_RE_MES = "|".join(MESES)

ALIAS_EDO = {
    "coahuila": "coahuila de zaragoza", "veracruz": "veracruz de ignacio de la llave",
    "michoacan": "michoacan de ocampo", "edomex": "mexico", "estado de mexico": "mexico",
    "cdmx": "ciudad de mexico",
}

# Ciudades que se llaman distinto a su municipio.
ALIAS_MUN = {"santiago de queretaro": "queretaro"}

# Nombres de estado (comparables) para separarlos de los municipios en la celda MUNICIPIO.
ESTADOS = {
    "aguascalientes", "baja california", "baja california sur", "campeche", "chiapas", "chihuahua",
    "ciudad de mexico", "coahuila", "coahuila de zaragoza", "colima", "durango", "guanajuato",
    "guerrero", "hidalgo", "jalisco", "mexico", "estado de mexico", "edomex", "michoacan",
    "michoacan de ocampo", "morelos", "nayarit", "nuevo leon", "oaxaca", "puebla", "queretaro",
    "quintana roo", "san luis potosi", "sinaloa", "sonora", "tabasco", "tamaulipas", "tlaxcala",
    "veracruz", "veracruz de ignacio de la llave", "yucatan", "zacatecas", "cdmx",
}

AYUDA = (
    "Hola, soy Tara y puedo ayudarte con:\n\n"
    "📤📅 Subir la agenda del día, solamente reenvíame el mensaje tal cual y me encargo.\n\n"
    "🚄🧐 Consultar la agenda de cualquier tramo, periodo, estado o municipio:\n"
    "• Qué actividades hay (hoy, mañana, una fecha o un rango de fechas)\n"
    "• Cuántas hubo de un tipo (firmas, reuniones, levantamientos…) o con una dependencia\n"
    "• Resumen por proyecto, tipo de actividad y estado\n"
    "• Lista de actividades de un periodo o proyecto específico\n"
    "• Resumen por día, semana o mes\n"
    "Después puedes seguir con «¿y mañana?» o «¿y en TMQ?».\n\n"
    "O platicar de lo que gustes…\n\n"
    "Atajos: /hoy  /manana  /semana  /mes  /ayuda"
)

SYSTEM_REESCRIBIR = (
    "Reescribes una pregunta sobre una agenda de actividades de campo en UNA sola línea, "
    "usando solo este vocabulario:\n"
    "- Verbo: 'cuantas', 'lista' o 'resumen'.\n"
    "- Periodo: 'hoy', 'ayer', 'mañana', 'esta semana', 'semana pasada', 'este mes', "
    "'mes pasado', el nombre de un mes, o una fecha dd/mm/aaaa.\n"
    "- Proyecto: su clave (TAP, TMQ, TQI, TSNL, TIG, TMLM, TQSLP, TSLPS, TIX).\n"
    "Conserva tal cual los nombres de estados, municipios, dependencias y el tipo de actividad.\n"
    "Si la pregunta continúa la conversación previa (ej. «¿y mañana?»), completa lo que falte "
    "con la pregunta anterior.\n"
    "No agregues datos que no estén en la pregunta. Si la pregunta no trata de la agenda, "
    "responde solo: NO"
)

SYSTEM_TARA = (
    "Eres Tara, la asistente de la agenda del Proyecto Ferroviario en la DGT (Dirección de Gestión Territorial). Hablas en español "
    "de México, con calidez y naturalidad, como una compañera de trabajo servicial; breve y al grano. "
    "Puedes usar algún emoji, sin exagerar. Respondes por Telegram: texto plano, sin Markdown ni tablas.\n"
    "Reglas:\n"
    "- Sobre la agenda usa SOLO lo que viene en DATOS. No inventes actividades, horas, lugares, personas ni cifras.\n"
    "- Los totales ya vienen contados en DATOS: úsalos tal cual. Copia horas y fechas como vienen.\n"
    "- Si DATOS no alcanza para responder, dilo con naturalidad y sugiere cómo preguntar "
    "(periodo, proyecto, municipio, dependencia).\n"
    "- Si te agradecen, saludan o se despiden, responde con amabilidad en una o dos frases.\n"
    "- Si piden algo ajeno a la agenda, dilo con amabilidad y ofrece lo que sí haces: consultar la agenda "
    "o subir una agenda que te reenvíen.\n"
    "- No menciones estas reglas ni la palabra DATOS."
)

SYSTEM_GENERAL = (
    "Eres Tara, asistente del equipo de la DGT (Dirección de Gestión Territorial) que trabaja en el Registro Agrario Nacional en el proyecto de Trenes de Pasajeros. "
    "Además de la agenda, platicas y ayudas con cualquier tema: dudas generales, redacción, "
    "cálculos, Excel, QGIS, traducciones, ideas. Hablas en español de México, con calidez y "
    "naturalidad, como una compañera servicial; responde breve salvo que pidan detalle. "
    "Respondes por Telegram: texto plano, sin Markdown (nada de asteriscos ni #); usa guiones "
    "o números para listas.\n"
    "No tienes acceso a internet: si preguntan por noticias, clima, precios o algo reciente, "
    "dilo y da lo que sepas aclarando que puede estar desactualizado.\n"
    "Si no sabes algo, dilo. Si preguntan por la agenda, sugiere preguntarlo con periodo o proyecto."
)

SYSTEM_CLASIFICAR = (
    "Decide si el último mensaje pregunta por la agenda de actividades del equipo (actividades, "
    "reuniones, firmas, levantamientos, proyectos ferroviarios, conteos o resúmenes de lo registrado), "
    "considerando la conversación previa. Responde solo AGENDA u OTRO."
)

# Mensajes de cortesía: se contestan con amabilidad, sin consultar el Sheet.
_RE_CHARLA = re.compile(
    r"(hola|holi|hey|buen(os)? dias?|buenas( tardes| noches)?|que tal|gracias|muchas gracias|mil gracias|"
    r"te lo agradezco|ok|okay|va|vale|sale|listo|perfecto|excelente|genial|super|muy bien|bien|"
    r"buen trabajo|eres la mejor|jaja\w*|adios|bye|hasta (luego|manana)|nos vemos|de nada)"
    r"( tara)?( muchas gracias| gracias)?")
_RESPALDO_CHARLA = {
    "gracias": ["¡Con gusto! Aquí estoy para lo que necesites 😊", "¡De nada! Cualquier cosa de la agenda, me dices.",
                "¡Para eso estoy! 🙌"],
    "saludo": ["¡Hola! ¿Qué necesitas saber de la agenda?", "¡Hola! 👋 Pregúntame lo que quieras de la agenda."],
    "otro": ["¡Va! Si necesitas algo más de la agenda, aquí sigo."],
}

# Conversación reciente por chat (para «¿y mañana?»). Vive en memoria; se pierde al reiniciar.
_HIST = {}
_ULTIMA = {}
HIST_TURNOS = 6
HIST_MINUTOS = 30


# Hoy en horario del centro de México (el servidor corre en UTC).
def hoy_mx():
    return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


# Texto comparable: sin acentos ni mayúsculas, puntuación como espacio.
def _n(s):
    s = N._sin_acentos(str(s or "")).lower()
    s = re.sub(r"[¿?¡!.,;:()\"'“”«»]", " ", s)
    s = re.sub(r"[-–—]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Estado en forma comparable y única ("Coahuila" = "Coahuila de Zaragoza").
def _edo(s):
    e = _n(s)
    return ALIAS_EDO.get(e, e)


def _palabra(frase, texto):
    return re.search(r"(?<!\w)" + re.escape(frase) + r"(?!\w)", texto) is not None


# True si el mensaje parece una pregunta o un comando y no una agenda.
def es_consulta(texto):
    t = (texto or "").strip()
    if not t or len(t) > 1500 or t.count("\n") > 12:
        return False
    if re.search(r"(?im)^\s*\d+[\.\-]\s|\bHora\s*:|https?://", t):
        return False
    if t.startswith("/"):
        return True
    # Una hora (10:00) o un encabezado "Agenda ..." / "Proyecto ..." son de agenda, no de pregunta.
    if re.search(r"\b\d{1,2}:\d{2}\b", t) or re.match(r"(proyecto\b|agenda\s+(?!de\s+(hoy|manana|ayer)\b))", _n(t)):
        return False
    return True   # mensaje corto sin forma de agenda: le toca a Tara


def _anio(s, hoy):
    if not s:
        return hoy.year
    return int(s) + 2000 if len(s) <= 2 else int(s)


class FechaInvalida(ValueError):
    """La pregunta trae una fecha que no existe (ej. 31/02)."""


def _dia(a, m, d):
    try:
        return date(a, m, d)
    except ValueError:
        raise FechaInvalida(f"{d}/{m}/{a}")


def _fin_de_mes(a, m):
    return (date(a + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)


# Periodo que pide la pregunta: (desde, hasta) o None si no menciona ninguno.
def periodo(n, hoy):
    n = re.sub(r"\b(por|en|a|de|durante) la manana\b", " ", n)   # "en la mañana" es parte del día

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*(?:al|a|hasta el|hasta)\s*"
                  r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", n)
    if m:
        a2 = _anio(m.group(6), hoy)
        d1 = _dia(_anio(m.group(3), hoy) if m.group(3) else a2, int(m.group(2)), int(m.group(1)))
        d2 = _dia(a2, int(m.group(5)), int(m.group(4)))
        if d1 and d2:
            return min(d1, d2), max(d1, d2)

    m = re.search(rf"\b(\d{{1,2}})\s*(?:al|a)\s*(\d{{1,2}})\s+de\s+({_RE_MES})(?:\s+(?:de|del)\s+(\d{{4}}))?", n)
    if m:
        a, mes = _anio(m.group(4), hoy), MESES.index(m.group(3)) + 1
        d1, d2 = _dia(a, mes, int(m.group(1))), _dia(a, mes, int(m.group(2)))
        if d1 and d2:
            return min(d1, d2), max(d1, d2)

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", n)
    if m:
        d = _dia(_anio(m.group(3), hoy), int(m.group(2)), int(m.group(1)))
        if d:
            return d, d

    m = re.search(rf"\b(\d{{1,2}})\s+de\s+({_RE_MES})(?:\s+(?:de|del)\s+(\d{{4}}))?", n)
    if m:
        d = _dia(_anio(m.group(3), hoy), MESES.index(m.group(2)) + 1, int(m.group(1)))
        if d:
            return d, d

    if _palabra("pasado manana", n):
        return hoy + timedelta(days=2), hoy + timedelta(days=2)
    if _palabra("hoy", n) and _palabra("manana", n):
        return hoy, hoy + timedelta(days=1)
    if _palabra("manana", n):
        return hoy + timedelta(days=1), hoy + timedelta(days=1)
    if re.search(r"\b(antier|anteayer|antes de ayer)\b", n):
        return hoy - timedelta(days=2), hoy - timedelta(days=2)
    if _palabra("ayer", n):
        return hoy - timedelta(days=1), hoy - timedelta(days=1)
    if _palabra("hoy", n):
        return hoy, hoy

    m = re.search(r"\bultimos (\d{1,3}) dias\b", n)
    if m:
        return hoy - timedelta(days=int(m.group(1)) - 1), hoy
    lunes = hoy - timedelta(days=hoy.weekday())
    if re.search(r"\bsemana (pasada|anterior)\b", n):
        return lunes - timedelta(days=7), lunes - timedelta(days=1)
    if re.search(r"\b(proxima semana|siguiente semana|semana que viene|semana proxima|semana entrante)\b", n):
        return lunes + timedelta(days=7), lunes + timedelta(days=13)
    if _palabra("ultima semana", n):
        return hoy - timedelta(days=6), hoy
    if _palabra("semana", n):
        return lunes, lunes + timedelta(days=6)

    if re.search(r"\bmes (pasado|anterior)\b", n):
        fin = hoy.replace(day=1) - timedelta(days=1)
        return fin.replace(day=1), fin
    if re.search(r"\b(proximo mes|siguiente mes|mes que viene|mes proximo|mes entrante)\b", n):
        ini = _fin_de_mes(hoy.year, hoy.month) + timedelta(days=1)
        return ini, _fin_de_mes(ini.year, ini.month)
    if _palabra("ultimo mes", n):
        return hoy - timedelta(days=30), hoy
    m = re.search(rf"\b({_RE_MES})(?:\s+(?:de|del)\s+(\d{{4}}))?\b", n)
    if m:
        a, mes = _anio(m.group(2), hoy), MESES.index(m.group(1)) + 1
        return date(a, mes, 1), _fin_de_mes(a, mes)
    if _palabra("mes", n):
        return hoy.replace(day=1), _fin_de_mes(hoy.year, hoy.month)

    if re.search(r"\bano (pasado|anterior)\b", n):
        return date(hoy.year - 1, 1, 1), date(hoy.year - 1, 12, 31)
    m = re.search(r"\b(20\d\d)\b", n)
    if m:
        return date(int(m.group(1)), 1, 1), date(int(m.group(1)), 12, 31)
    if _palabra("ano", n):
        return date(hoy.year, 1, 1), hoy
    return None


def _fmt(d):
    return f"{DIAS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def _etiqueta_periodo(desde, hasta):
    if desde == hasta:
        return _fmt(desde)
    return f"{desde.strftime('%d/%m/%Y')} – {hasta.strftime('%d/%m/%Y')}"


# Proyectos mencionados (por clave o por nombre) y el texto sin las palabras de ese nombre.
def _proyectos(texto, n):
    claves = []
    for c in N._CLAVES_RE.findall(texto):
        if c.upper() not in claves:
            claves.append(c.upper())
    for alias, real in getattr(N, "_ALIAS_CLAVE", {}).items():
        if _palabra(alias, n) and real.upper() not in claves:
            claves.append(real.upper())
    inv = {v: k for k, v in N.mapa_proyectos.items()}
    palabras = set(n.split())
    resto = n
    for clave_nombre, nombre in N._PROY_POR_NOMBRE.items():
        if clave_nombre <= palabras:
            if inv[nombre] not in claves:
                claves.append(inv[nombre])
            for w in clave_nombre:
                resto = re.sub(r"(?<!\w)" + re.escape(w) + r"(?!\w)", " ", resto)
    return claves, resto


# Municipios de una celda «Polotitlán, Estado de México; Aculco y Amealco, Querétaro»,
# sin el estado que va al final de cada grupo. Devuelve [(comparable, como se muestra)].
def _municipios(celda):
    salida = []
    for grupo in str(celda or "").split(";"):
        partes = [x.strip(" .") for x in re.split(r",|\s+y\s+", grupo) if x.strip(" .")]
        if len(partes) >= 2 and _n(partes[-1]) in ESTADOS:
            partes = partes[:-1]
        salida += [(_n(x), _titulo(x)) for x in partes]
    return salida


# Estado y municipio mencionados, tomados de los que existen en el Sheet.
# Devuelve (estado, municipio) comparables y sus nombres tal como se muestran.
def _lugar(n, filas):
    nombres_edo, nombres_mun = {}, {}
    for f in filas:
        if (f.get("ESTADO") or "").strip():
            nombres_edo.setdefault(_edo(f.get("ESTADO")), _titulo(f.get("ESTADO")))
        for comparable, nombre in _municipios(f.get("MUNICIPIO")):
            nombres_mun.setdefault(comparable, nombre)
    muns = sorted(nombres_mun, key=len, reverse=True)

    # «Municipio de Querétaro» o «Santiago de Querétaro»: el municipio, no todo el estado.
    for alias, real in ALIAS_MUN.items():
        if _palabra(alias, n):
            n = n.replace(alias, "municipio de " + real)
    m = re.search(r"\b(?:municipio|ciudad|cd) de (?!mexico\b)(.+)", n)
    if m:
        municipio = next((x for x in muns if re.match(re.escape(x) + r"(?!\w)", m.group(1))), "")
        if municipio:
            return "", municipio, "", nombres_mun[municipio]

    edos = sorted(nombres_edo, key=len, reverse=True)
    if _palabra("ciudad de mexico", n):          # no confundir con el Estado de México
        estado = "ciudad de mexico"
        nombres_edo.setdefault(estado, "Ciudad de México")
    else:
        estado = next((e for e in edos if _palabra(e, n)), "")
    if not estado:
        estado = next((real for alias, real in ALIAS_EDO.items()
                       if _palabra(alias, n) and real in nombres_edo), "")
    resto = n
    if estado:
        for frase in [estado] + [al for al, r in ALIAS_EDO.items() if r == estado]:
            resto = resto.replace(frase, " ")
    municipio = next((m for m in muns if len(m) >= 5 and _palabra(m, resto)), "")
    return estado, municipio, nombres_edo.get(estado, ""), nombres_mun.get(municipio, "")


# Interpreta la pregunta. Devuelve un diccionario con la intención y los filtros.
def interpretar(texto, filas, hoy):
    n = _n(texto)
    claves, sin_proy = _proyectos(texto, n)
    rango = periodo(n, hoy)
    estado, municipio, estado_txt, municipio_txt = _lugar(sin_proy, filas)
    categorias = sorted({re.sub(r"^\d+\.\s*", "", c) for c, _ in N.clasificar_actividad(texto)})
    celda = N._sin_acentos(texto.upper())
    deps = [canon for canon, pats in N._DEP_PATRONES.items() if any(p.search(celda) for p in pats)]

    if _palabra("resumen", n):
        intencion = "resumen"
    elif re.search(r"\b(cuant[oa]s?|total|numero de)\b", n):
        intencion = "contar"
    elif re.search(r"\b(que hay|que tenemos|que se tiene|cuales|lista|listado|listame|muestrame|muestra|detalle|agenda)\b", n):
        intencion = "listar"
    else:
        intencion = ""

    entendido = bool(rango or claves or estado or municipio or categorias or deps or intencion)
    explicita = bool(intencion)
    if not intencion:
        intencion = "listar" if rango and (rango[1] - rango[0]).days <= 7 else "contar"
    if rango:
        desde, hasta, por_defecto = rango[0], rango[1], ""
    elif intencion == "listar":
        desde, hasta, por_defecto = hoy, hoy, " (no indicaste periodo; tomé hoy)"
    else:
        desde, hasta, por_defecto = date(hoy.year, 1, 1), hoy, " (no indicaste periodo; tomé lo que va del año)"
    return {"intencion": intencion, "desde": desde, "hasta": hasta, "nota_periodo": por_defecto,
            "proyectos": claves, "estado": estado, "municipio": municipio,
            "estado_txt": estado_txt, "municipio_txt": municipio_txt,
            "categorias": categorias, "dependencias": deps, "entendido": entendido,
            "con_periodo": bool(rango), "intencion_explicita": explicita}


# Filas del Sheet que cumplen el periodo y los filtros.
def filtrar(q, filas):
    inv = {v: k for k, v in N.mapa_proyectos.items()}
    salida = []
    for f in filas:
        d = _fecha(f)
        if d is None or d < q["desde"] or d > q["hasta"]:
            continue
        clave = inv.get((f.get("PROYECTO FERROVIARIO") or "").strip(), "OTRO")
        if q["proyectos"] and clave not in q["proyectos"]:
            continue
        if q["estado"] and _edo(f.get("ESTADO")) != q["estado"]:
            continue
        if q["municipio"] and q["municipio"] not in {c for c, _ in _municipios(f.get("MUNICIPIO"))}:
            continue
        cats = {re.sub(r"^\d+\.\s*", "", c) for c, _ in N.clasificar_actividad(
            f.get("TIPO DE SOLICITUD", ""), f.get("ACTIVIDADES DESARROLLADAS", ""))}
        if q["categorias"] and not (cats & set(q["categorias"])):
            continue
        if q["dependencias"]:
            celda = N._sin_acentos(str(f.get("DEPENDENCIAS PARTICIPANTES", "")).upper())
            if not all(any(p.search(celda) for p in N._DEP_PATRONES[c]) for c in q["dependencias"]):
                continue
        salida.append({"fecha": d, "hora": _hora(f), "proyecto": clave, "cats": cats,
                       "estado": (f.get("ESTADO") or "").strip(),
                       "municipio": (f.get("MUNICIPIO") or "").strip(),
                       "actividad": (f.get("TIPO DE SOLICITUD") or "").strip(),
                       "detalle": (f.get("ACTIVIDADES DESARROLLADAS") or "").strip(),
                       "nucleo": (f.get("NÚCLEO AGRARIO") or "").strip(),
                       "dependencias": (f.get("DEPENDENCIAS PARTICIPANTES") or "").strip()})
    salida.sort(key=lambda a: (a["fecha"], a["hora"] or "99:99"))
    return salida


def _plural(n):
    return f"{n} actividad" if n == 1 else f"{n:,} actividades"


def _encabezado(q):
    lineas = [f"Periodo: {_etiqueta_periodo(q['desde'], q['hasta'])}{q['nota_periodo']}"]
    filtros = []
    if q["proyectos"]:
        filtros.append("proyecto " + ", ".join(q["proyectos"]))
    if q["estado"]:
        filtros.append("estado " + q["estado_txt"])
    if q["municipio"]:
        filtros.append("municipio " + q["municipio_txt"])
    if q["categorias"]:
        filtros.append("categoría " + " / ".join(_corto(c) for c in q["categorias"]))
    if q["dependencias"]:
        filtros.append("con " + " y ".join(q["dependencias"]))
    if filtros:
        lineas.append("Filtros: " + " · ".join(filtros))
    return "\n".join(lineas)


# Nombres cortos de las categorías, solo para mostrar.
CORTO = {
    "Atención a Reuniones/Asambleas/Asesorías": "Reuniones y asambleas",
    "Caminamientos/Inspecciones": "Caminamientos",
    "Levantamientos Agenda (Topográficos/BDTs Agroforestales y Construcción)": "Levantamientos (topografía/BDT)",
    "Firmas de convenios y actas": "Firmas",
    "Acercamientos/Sensibilización/Negociación": "Acercamientos",
    "Visitas/Revisiones/Verificaciones": "Visitas y verificaciones",
    "Consultas y búsquedas registrales": "Consultas registrales",
    "Peritajes/Avalúos/Acompañamientos": "Peritajes y avalúos",
    "Marcaje/Delimitación": "Marcaje",
    "Mesas sociales/Convocatorias/Videoconferencias": "Mesas y convocatorias",
    "Censos/Reubicaciones": "Censos",
    "Presentaciones/Entregas": "Presentaciones",
}


def _corto(cat):
    return CORTO.get(cat, cat)


# Bloque de conteo en renglones: «• Nombre  12», del mayor al menor.
def _bloque(titulo, conteo, sin=0, etiqueta_sin="Sin dato"):
    lineas = [titulo]
    for k, v in conteo.most_common():
        lineas.append(f"• {k}: {v}")
    if sin:
        lineas.append(f"• {etiqueta_sin}: {sin}")
    return "\n".join(lineas)


def _por_proyecto(acts):
    c = Counter(a["proyecto"] for a in acts)
    return " · ".join(f"{k} {v}" for k, v in c.most_common())


# Arma el texto de respuesta a partir de la pregunta interpretada.
def redactar(q, acts):
    cab = _encabezado(q)
    if not acts:
        return f"No encontré actividades registradas.\n{cab}"

    if q["intencion"] == "contar":
        texto = f"📊 {_plural(len(acts))}\n{cab}"
        if not q["proyectos"] and len({a['proyecto'] for a in acts}) > 1:
            texto += f"\nPor proyecto: {_por_proyecto(acts)}"
        return texto

    if q["intencion"] == "resumen":
        proyectos = Counter(a["proyecto"] for a in acts)
        partes = [f"📋 Resumen · {_etiqueta_periodo(q['desde'], q['hasta'])}{q['nota_periodo']}\n"
                  f"{_plural(len(acts))} en {len(proyectos)} proyecto{'s' if len(proyectos) != 1 else ''}"]
        if "\n" in cab:
            partes[0] += "\n" + cab.split("\n", 1)[1]    # filtros, si los hay

        # Por proyecto, con el desglose de qué se hizo en cada uno.
        bloque = ["🚆 Por proyecto"]
        for clave, total in proyectos.most_common():
            cats = Counter(_corto(c) for a in acts if a["proyecto"] == clave for c in a["cats"])
            bloque.append(f"• {clave}: {total}")
            if cats:
                bloque.append("   " + ", ".join(f"{k} {v}" for k, v in cats.most_common()))
        partes.append("\n".join(bloque))

        cats = Counter(_corto(c) for a in acts for c in a["cats"])
        if cats:
            partes.append(_bloque("🗂 Por tipo de actividad", cats,
                                  sum(1 for a in acts if not a["cats"]), "Sin clasificar"))
        edos = Counter(_titulo(a["estado"]) for a in acts if a["estado"])
        if edos:
            partes.append(_bloque("📍 Por estado", edos,
                                  sum(1 for a in acts if not a["estado"]), "Sin ubicación"))
        return "\n\n".join(partes)

    lineas = [f"🗓 {_plural(len(acts))}", cab]
    dia = None
    for a in acts[:MAX_LISTA]:
        if a["fecha"] != dia:
            dia = a["fecha"]
            lineas.append(f"\n— {_fmt(dia)} —")
        lugar = _titulo(a["municipio"]) or _titulo(a["estado"])
        actividad = a["actividad"] if len(a["actividad"]) <= 90 else a["actividad"][:87] + "…"
        lineas.append(f"• {a['hora'] or '--:--'} [{a['proyecto']}] " + (f"{lugar} · " if lugar else "") + actividad)
    if len(acts) > MAX_LISTA:
        lineas.append(f"\n… y {len(acts) - MAX_LISTA} más. Acota con un proyecto o un periodo más corto.")
    return "\n".join(lineas)


_COMANDOS = {
    "/hoy": "que hay hoy", "/manana": "que hay manana", "/mañana": "que hay manana",
    "/semana": "resumen de esta semana", "/mes": "resumen de este mes",
}


# Respuesta completa a un mensaje de consulta.
# natural=True: Haiku la redacta con calidez sobre los datos que contó el código.
def responder(texto, hoy=None, chat_id=None, natural=False):
    hoy = hoy or hoy_mx()
    t = (texto or "").strip()
    if t.startswith("/"):
        comando = t.split()[0].split("@")[0].lower()
        if comando in ("/start", "/ayuda", "/help"):
            return AYUDA
        if comando not in _COMANDOS:
            return "No conozco ese comando.\n\n" + AYUDA
        t = _COMANDOS[comando] + " " + " ".join(t.split()[1:])
    if _n(t) == "ayuda":
        return AYUDA
    hist = _historial(chat_id)
    if _RE_CHARLA.fullmatch(_n(t)):
        return (natural and _natural(t, "", hist)) or _respaldo_charla(t)

    filas = N._filas_hoja_principal()
    try:
        q = interpretar(t, filas, hoy)
    except FechaInvalida:
        return "Esa fecha no existe 🤔 Escríbela como dd/mm/aaaa, por ejemplo 12/11/2026."
    q = _heredar(q, chat_id, t)
    if natural and not (q["proyectos"] or q["dependencias"]) and not _es_de_agenda(t, hist):
        # «¿Cuántos estados tiene México?» trae palabras de agenda pero no lo es.
        return _general(t, hist) or "Ahorita no pude contestarte 😅 Intenta de nuevo en un momento."
    if not q["entendido"]:
        # Haiku reescribe la pregunta con palabras que sí entiendo; las cuentas siguen siendo del código.
        previa = f"Conversación previa:\n{hist}\n\nPregunta: " if hist else ""
        nueva = N._llamar_haiku(SYSTEM_REESCRIBIR, previa + t, 80, 0, "tara")
        if natural and nueva and nueva.strip().upper() == "NO":
            # No es de la agenda: plática libre.
            return _general(t, hist) or "Ahorita no pude contestarte 😅 Intenta de nuevo en un momento."
        if nueva and nueva.strip().upper() != "NO" and len(nueva) < 200 and "\n" not in nueva.strip():
            try:
                q = interpretar(nueva.strip(), filas, hoy)
            except FechaInvalida:
                pass
    if not q["entendido"]:
        if not natural:
            return "No entendí la pregunta.\n\n" + AYUDA
        # Pregunta libre: Tara busca en la agenda de una semana atrás a una adelante.
        q = interpretar("lista", filas, hoy)
        q["desde"], q["hasta"] = hoy - timedelta(days=7), hoy + timedelta(days=7)
        q["nota_periodo"] = " (revisé una semana atrás y una adelante)"
        acts = filtrar(q, filas)
        return _natural(t, _datos(q, acts, redactar(q, acts)), hist) or ("No entendí la pregunta.\n\n" + AYUDA)
    if chat_id:
        _ULTIMA[chat_id] = (time.time(), q)
    acts = filtrar(q, filas)
    base = redactar(q, acts)
    if q["intencion"] == "resumen":
        return base          # el resumen ya viene estructurado; Haiku no lo reescribe
    return (natural and _natural(t, _datos(q, acts, base), hist)) or base


# «¿Y mañana?», «¿y en TSNL?»: completa lo que falte con la pregunta anterior del mismo chat.
def _heredar(q, chat_id, texto):
    previa = _ULTIMA.get(chat_id)
    if not previa or time.time() - previa[0] > HIST_MINUTOS * 60:
        return q
    if not re.match(r"(y|e|tambien|ahora|entonces|y si|igual)\b", _n(texto)):
        return q
    p = dict(previa[1])
    if q["con_periodo"]:
        p.update(desde=q["desde"], hasta=q["hasta"], nota_periodo="", con_periodo=True)
    if q["intencion_explicita"]:
        p["intencion"] = q["intencion"]
    for k in ("proyectos", "categorias", "dependencias"):
        if q[k]:
            p[k] = q[k]
    if q["estado"] or q["municipio"]:
        p.update({k: q[k] for k in ("estado", "municipio", "estado_txt", "municipio_txt")})
    p["entendido"] = True
    return p


# Lo que Haiku puede usar: la respuesta contada por el código y el detalle de cada actividad.
def _datos(q, acts, base, maximo=40, limite=9000):
    lineas = [base, "", "DETALLE:"]
    for a in acts[:maximo]:
        partes = [f"{a['fecha'].strftime('%d/%m/%Y')} {a['hora'] or '--:--'} [{a['proyecto']}]",
                  ", ".join(x for x in (_titulo(a["municipio"]), _titulo(a["estado"])) if x),
                  a["actividad"]]
        if a.get("nucleo"):
            partes.append("Núcleo: " + a["nucleo"])
        if a.get("dependencias"):
            partes.append("Dependencias: " + a["dependencias"])
        if a.get("detalle"):
            partes.append("Desarrollo: " + a["detalle"][:250])
        lineas.append(" | ".join(p for p in partes if p))
    if len(acts) > maximo:
        lineas.append(f"(y {len(acts) - maximo} más que no se detallan)")
    return "\n".join(lineas)[:limite]


# Haiku redacta la respuesta. Si aparece una cifra que no está en los datos, se descarta.
def _natural(pregunta, datos, hist):
    mensaje = (f"CONVERSACIÓN PREVIA:\n{hist or '(ninguna)'}\n\n"
               f"DATOS:\n{datos or '(no hace falta consultar la agenda)'}\n\n"
               f"MENSAJE DEL USUARIO:\n{pregunta}")
    r = N._llamar_haiku(SYSTEM_TARA, mensaje, 700, 0.5, "tara-charla")
    if not r:
        return None
    permitidas = set(re.findall(r"\d+", " ".join((datos, pregunta, hist))))
    extra = [x for x in re.findall(r"\d+", r) if x not in permitidas]
    if extra:
        print(f"[tara] respuesta descartada, cifras fuera de los datos: {extra[:5]}", flush=True)
        return None
    return r[:3800]


# Haiku dice si el mensaje es de la agenda; si falla, se asume que sí (como antes).
def _es_de_agenda(pregunta, hist):
    previa = f"Conversación previa:\n{hist}\n\n" if hist else ""
    r = N._llamar_haiku(SYSTEM_CLASIFICAR, previa + "Mensaje: " + pregunta, 5, 0, "tara-tema")
    return not (r and r.strip().upper().startswith("OTRO"))


# Plática libre con Haiku (temas fuera de la agenda); sin datos del Sheet.
def _general(pregunta, hist):
    mensaje = f"CONVERSACIÓN PREVIA:\n{hist or '(ninguna)'}\n\nMENSAJE DEL USUARIO:\n{pregunta}"
    r = N._llamar_haiku(SYSTEM_GENERAL, mensaje, 900, 0.7, "tara-general", timeout=30)
    if not r:
        return None
    r = re.sub(r"(?m)\*\*|^#+\s*", "", r)    # Telegram va sin formato
    return r[:3800]


def _respaldo_charla(texto):
    n = _n(texto)
    tipo = "gracias" if "gracias" in n or "agradezco" in n else (
        "saludo" if re.match(r"(hola|holi|hey|buen|que tal)", n) else "otro")
    return random.choice(_RESPALDO_CHARLA[tipo])


# Últimos turnos del chat como texto, para dar contexto a Haiku.
def _historial(chat_id):
    if not chat_id:
        return ""
    limite = time.time() - HIST_MINUTOS * 60
    turnos = [x for x in _HIST.get(chat_id, []) if x[0] > limite]
    _HIST[chat_id] = turnos
    return "\n".join(f"{quien}: {txt}" for _, quien, txt in turnos)


def _recordar(chat_id, quien, texto):
    turnos = _HIST.setdefault(chat_id, [])
    turnos.append((time.time(), quien, texto[:500]))
    del turnos[:-HIST_TURNOS]


# Muestra «escribiendo…» en Telegram mientras Tara piensa.
def _escribiendo(chat_id):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendChatAction",
                          json={"chat_id": chat_id, "action": "typing"}, timeout=5)
        except requests.RequestException:
            pass


# Punto de entrada desde el webhook: True si el mensaje era una consulta y ya se respondió.
def atender(chat_id, texto):
    if not es_consulta(texto):
        return False
    _escribiendo(chat_id)
    try:
        respuesta = responder(texto, chat_id=chat_id, natural=True)
    except Exception:
        print(traceback.format_exc(), flush=True)
        respuesta = "❌ No pude consultar el Sheet en este momento. Intenta de nuevo en un minuto."
    _recordar(chat_id, "Usuario", texto)
    _recordar(chat_id, "Tara", respuesta)
    N.telegram_enviar(chat_id, respuesta)
    return True


# Conecta Tara a la app principal y agrega la ruta del resumen diario.
def registrar(app, nucleo):
    global N
    N = nucleo
    N._RUTAS_PUBLICAS.add("tara_resumen_diario")     # no pide contraseña: trae su propio secreto

    # La llama un programador externo cada mañana; manda la agenda del día a los chats autorizados.
    @app.route("/tara/resumen-diario", methods=["GET", "POST"])
    def tara_resumen_diario():
        secreto = os.environ.get("CRON_SECRET", "")
        if not secreto or not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secreto):
            return "", 403
        try:
            texto = "☀️ Buenos días. Agenda de hoy:\n\n" + responder("que hay hoy")
        except Exception:
            print(traceback.format_exc(), flush=True)
            return jsonify({"error": "No se pudo leer el Sheet"}), 500
        for chat_id in N.TELEGRAM_CHATS_OK:
            N.telegram_enviar(chat_id, texto)
        return jsonify({"enviados": len(N.TELEGRAM_CHATS_OK)})
