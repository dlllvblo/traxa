from flask import Flask, request, jsonify, render_template, render_template_string, session, redirect, url_for
import hmac
import time
import re
from datetime import datetime, timedelta, timezone

MX = timezone(timedelta(hours=-6))   # hora del centro de México (el servidor corre en UTC)
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import requests

HAIKU_MODEL = os.environ.get("HAIKU_MODEL", "claude-haiku-5-5")

from shapely.geometry import shape, Point
from shapely.strtree import STRtree
import unicodedata
import difflib
import threading
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
# Sesión firmada; sin SECRET_KEY en Render las sesiones se pierden en cada reinicio.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Rutas que no piden contraseña (Tara tiene su propio secreto).
_RUTAS_PUBLICAS = {"login", "service_worker", "telegram_webhook", "static"}

LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TRAXA</title>
<link rel="icon" type="image/png" href="/static/icono.png">
<style>body{font-family:sans-serif;background:#000;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
form{background:#fff;border-radius:16px;padding:28px;width:min(320px,90vw);box-sizing:border-box}
h2{margin:0 0 16px;color:#691B4F;text-align:center;letter-spacing:2px}input,button{width:100%;padding:12px;border-radius:10px;font-size:16px;box-sizing:border-box}
input{border:1px solid #ccc;margin-bottom:12px}button{border:0;background:#691B4F;color:#fff;cursor:pointer}
.err{color:#b00020;margin:0 0 12px}</style></head><body>
<form method="post"><h2>TRAXA</h2>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<input type="password" name="clave" placeholder="Contraseña" autofocus required>
<button type="submit">Entrar</button></form></body></html>"""

# Pide la contraseña antes de cualquier ruta que no sea pública.
@app.before_request
def _exigir_login():
    if request.endpoint in _RUTAS_PUBLICAS or session.get("ok"):
        return None
    if request.path.startswith("/api/") or request.path == "/procesar":
        return jsonify({"error": "Sesión expirada: recarga la página y vuelve a entrar"}), 401
    sig = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=sig))

# Pantalla de acceso con una contraseña compartida (APP_PASSWORD en Render).
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        clave = os.environ.get("APP_PASSWORD", "")
        if clave and hmac.compare_digest(request.form.get("clave", ""), clave):
            session.clear()
            session.permanent = True
            session["ok"] = True
            sig = request.args.get("next", "/")
            return redirect(sig if sig.startswith("/") and not sig.startswith("//") else "/")
        time.sleep(1)                        # frena intentos repetidos
        error = "Contraseña incorrecta"
    return render_template_string(LOGIN_HTML, error=error)

# Cierra la sesión en este dispositivo.
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# MAPA PROYECTOS: el catálogo vive en proyectos.json (clave → nombre, Sheet del jefe, director, alias…)
with open(os.path.join(os.path.dirname(__file__), "proyectos.json"), encoding="utf-8") as _f:
    PROYECTOS = json.load(_f)
mapa_proyectos = {clave: p["nombre"] for clave, p in PROYECTOS.items()}


# HOJAS
SPREADSHEET_ID_JEFE = os.environ.get("SPREADSHEET_ID_JEFE", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")

PROYECTOS_JEFE = {p["nombre"] for p in PROYECTOS.values() if p.get("sheet_jefe")}
DIRECTOR_TRAMO = {p["nombre"]: p["director"] for p in PROYECTOS.values() if p.get("director")}

# Normaliza texto para comparar: sin número inicial, acentos, guiones ni mayúsculas.
def _norm(s):
    s = s or ""
    s = re.sub(r'^\s*\d+\.\s*', '', s)                       
    s = ''.join(c for c in unicodedata.normalize('NFD', s)   
                if unicodedata.category(c) != 'Mn')
    s = s.lower()
    s = re.sub(r'[-–—−]', ' ', s)                            
    s = re.sub(r'\s+', ' ', s).strip()                       
    return s

# Quita acentos y diacríticos.
def _sin_acentos(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or "")
                   if unicodedata.category(c) != 'Mn')

DEP_ALIASES = {
    "SEDATU":   ["SEDATU", "EDATU", "DGCAM"],      # DGCAM es de SEDATU
    "RAN":      ["RAN", "REGISTRO AGRARIO NACIONAL"],
    "SEDENA":   ["SEDENA", "DEFENSA", "SDN"],   # DEFENSA == SEDENA
    "SICT":     ["SICT", "VINCULACION"],        # Vinculación es unidad de SICT
    "ATTRAPI":  ["ATTRAPI", "ATRAPPI", "ATRAPI"],
    "PA":       ["PA", "P.A.", "PROCURADURIA AGRARIA"],
    "FIFONAFE": ["FIFONAFE"],
    "INDAABIN": ["INDAABIN", "INDABIN"],
    "GOB. ESTATAL":   ["GOBIERNO DEL ESTADO", "GOBIERNO ESTATAL", "GOB. ESTATAL", "DESARROLLO URBANO ESTATAL"],
    "GOB. MUNICIPAL": ["GOBIERNO MUNICIPAL", "AYUNTAMIENTO", "PRESIDENCIA MUNICIPAL", "MUNICIPAL",
                       "MUNICIPALES", "MUNICIPIO", "MUNICIPIOS", "CATASTRO"],
    # OTRAS
    "ARTF":     ["ARTF"],
    "SIAL":     ["SIAL"],          
    "LIDERVIC": ["LIDERVIC"],      
    "CONAVI":   ["CONAVI"],
    "CONAGUA":  ["CONAGUA"],
    "CFE":      ["CFE"],
    "INPI":     ["INPI"],
    "INAH":     ["INAH"],
    "SEMARNAT": ["SEMARNAT"],
    "SEGOB":    ["SEGOB"],
    "CENAGAS":  ["CENAGAS"],
    "CENAPRED": ["CENAPRED", "CENEPRED"],
    "SALUD":    ["SALUD"],
    "IMEVIS":   ["IMEVIS"],
}

DEP_NUCLEO = {"SEDATU","SEDENA","RAN","SICT","ATTRAPI","PA","FIFONAFE","INDAABIN","GOB. ESTATAL","GOB. MUNICIPAL"}

# Empresas y supervisión de obra: se cuentan aparte, no son dependencias.
EMPRESAS_ALIASES = {
    "Supervisión de obra": ["SUPERVISION DE OBRA", "SUPERVISION EXTERNA", "SUPERVISION", "RESIDENCIA DE OBRA"],
    "Mota-Engil":          ["MOTA-ENGIL", "MOTA - ENGIL", "MOTA ENGIL", "ME-M"],
    "CARSO":               ["CARSO"],
    "DOZZER":              ["DOZZER", "DOZER"],
}

# Arma la regex que reconoce una dependencia (sin acentos, puntos opcionales).
def _dep_pat(alias):
    a = _sin_acentos(alias.upper())
    cuerpo = r'\s+'.join(re.escape(w) for w in a.split()).replace(r'\.', r'\.?')
    return r'(?<![A-Z0-9])' + cuerpo + r'(?![A-Z0-9])'

_DEP_PATRONES = {canon: [re.compile(_dep_pat(al)) for al in als]
                 for canon, als in DEP_ALIASES.items()}
# «Gobierno de Tizayuca» es municipal; «Gobierno de Hidalgo», estatal.
_EDOS_GOB = r"HIDALGO|QUERETARO|GUANAJUATO|JALISCO|SAN LUIS POTOSI|COAHUILA|NUEVO LEON|TAMAULIPAS|MEXICO|SINALOA"
_DEP_PATRONES["GOB. ESTATAL"].append(re.compile(r"(?<![A-Z0-9])GOBIERNO\s+DE\s+(?:" + _EDOS_GOB + r")(?![A-Z0-9])"))
_DEP_PATRONES["GOB. MUNICIPAL"].append(re.compile(r"(?<![A-Z0-9])GOBIERNO\s+DE\s+(?!(?:" + _EDOS_GOB + r")(?![A-Z0-9]))[A-Z]"))
_EMP_PATRONES = {canon: [re.compile(_dep_pat(al)) for al in als]
                 for canon, als in EMPRESAS_ALIASES.items()}

_PROY_POR_CLAVE  = {_norm(k): v for k, v in mapa_proyectos.items()}
_PROY_POR_NOMBRE = {frozenset(_norm(v).split()): v for v in mapa_proyectos.values()}
_ALIAS_NOMBRE_PROYECTO = {frozenset(_norm(alt).split()): p["nombre"]          # «SAN LUIS - SALTILLO»
                          for p in PROYECTOS.values() for alt in p.get("nombres_alternos", [])}
_PROY_POR_NOMBRE.update(_ALIAS_NOMBRE_PROYECTO)
_VOCAB_PROY = sorted({w for k in _PROY_POR_NOMBRE for w in k})

# Corrige una palabra al término más parecido de la lista; sin cambio si no hay uno claro.
def _mas_parecida(palabra, opciones):
    puntos = sorted(((difflib.SequenceMatcher(None, palabra, o).ratio(), o) for o in opciones), reverse=True)
    if puntos and puntos[0][0] >= 0.8 and (len(puntos) == 1 or puntos[0][0] > puntos[1][0]):
        return puntos[0][1]
    return palabra

# Busca el proyecto por nombre tolerando errores de dedo (Ej. POSTOSI → POTOSI).
def _proyecto_por_nombre(texto):
    palabras = [w for w in _norm(texto).split() if not re.fullmatch(r't\d+|tramo|\d+', w)]
    exacto = _PROY_POR_NOMBRE.get(frozenset(palabras))
    if exacto:
        return exacto
    return _PROY_POR_NOMBRE.get(frozenset(_mas_parecida(w, _VOCAB_PROY) for w in palabras))
_ALIAS_CLAVE = {_norm(alt): _norm(clave)                                     # «TQM» → TMQ
                for clave, p in PROYECTOS.items() for alt in p.get("claves_alternas", [])}
_CLAVES_RE = re.compile(
    r'\b(' + '|'.join(sorted(mapa_proyectos, key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)
ABREV_TRAMO = {ab.upper(): clave for clave, p in PROYECTOS.items() for ab in p.get("abreviaturas", [])}   # «MEX-QRO» → TMQ
_ABREV_TRAMO_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k).replace(r'\-', r'\s*-\s*')
                       for k in sorted(ABREV_TRAMO, key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)
def _resolver_abrev_tramo(texto):
    """Busca una abreviatura de tramo (ej. 'MEX-QRO') y devuelve su clave (TMQ), o None."""
    m = _ABREV_TRAMO_RE.search(texto)
    if not m:
        return None
    clave = re.sub(r'\s*-\s*', '-', m.group(1).upper())
    return ABREV_TRAMO.get(clave)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

meses = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
}

# CAPA MUNICIPIOS INEGI
# Carga los municipios INEGI (geojson) en un índice espacial.
def cargar_capas():
    base = os.path.join(os.path.dirname(__file__), "data")
    with open(os.path.join(base, "municipios_mx.geojson"), encoding="utf-8") as f:
        mun_gj = json.load(f)

    geoms = [shape(feat["geometry"]) for feat in mun_gj["features"]]
    props = [feat["properties"] for feat in mun_gj["features"]]
    return STRtree(geoms), geoms, props

ARBOL_MUN, MUN_GEOMS, MUN_PROPS = cargar_capas()


from urllib.parse import unquote
_COORDS_CACHE = {}

def extraer_coordenadas(url_maps):
    """Caché: evita re-expandir el mismo link corto (10s de timeout cada uno)."""
    if url_maps in _COORDS_CACHE:
        return _COORDS_CACHE[url_maps]
    res = _extraer_coordenadas_raw(url_maps)
    if res is None:
        res = (None, None)
    _COORDS_CACHE[url_maps] = res
    return res


def _extraer_coordenadas_raw(url_maps):
    """Extrae lat, lng de un link de Google Maps (corto o expandido)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }

    # Busca lat/lng en los distintos formatos de URL de Google Maps.
    def buscar_coords(url):
        # Decodificar URL-encoding (doble, por el parámetro continue= de Google)
        url = unquote(unquote(url))
        # 1. Pin real del lugar: !3d{lat}!4d{lng}
        match = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        # 2. Formato ?q=lat,lng
        match = re.search(r'[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        # 3. Búsqueda por coordenadas: /maps/search/lat,+lng
        match = re.search(r'/maps/search/(-?\d+\.\d+),\s*\+?(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        # 4. Links de ruta: destino como !1d{lng}!2d{lat}
        if "/dir/" in url:
            match = re.search(r'!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)', url)
            if match:
                return float(match.group(2)), float(match.group(1))
        # 5. Último recurso: @lat,lng (centro del encuadre, puede diferir del punto)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', url)
        if match:
            return float(match.group(1)), float(match.group(2))
        return None

    # Intentar sobre la URL original
    coords = buscar_coords(url_maps)
    if coords:
        return coords

    # Expandir el link corto y buscar en la URL final 
    try:
        resp = requests.get(url_maps, allow_redirects=True, timeout=10, headers=headers)
        coords = buscar_coords(resp.url)
        if coords:
            return coords
    except requests.RequestException:
        pass
    return None, None


def obtener_estado_municipio(lat, lng):
    """Point-in-polygon contra capa municipal INEGI local"""
    punto = Point(lng, lat)
    for idx in ARBOL_MUN.query(punto):
        if MUN_GEOMS[idx].contains(punto):
            p = MUN_PROPS[idx]
            return p["NOM_ENT"], p["NOM_MUN"]
    # Fallback: punto en hueco de la simplificación → municipio más cercano (<~1 km)
    idx = ARBOL_MUN.nearest(punto)
    if idx is not None and MUN_GEOMS[idx].distance(punto) < 0.01:
        p = MUN_PROPS[idx]
        return p["NOM_ENT"], p["NOM_MUN"]
    return "", ""

def _es_url_maps(url):
    """True solo si es un enlace de mapa geocodificable.
       Excluye Meet/Docs/Calendar que comparten dominio google.com pero no llevan coords."""
    if not url:
        return False
    if any(x in url for x in ("meet.google.com", "docs.google.com", "calendar.google.com")):
        return False
    return any(d in url for d in ("goo.gl", "google.com", "share.google"))

# Llama a Haiku y devuelve su texto; None si falla (el error queda en el log).
def _llamar_haiku(system, mensaje, max_tokens, temperature, etiqueta, timeout=8):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"[haiku {etiqueta}] Falta ANTHROPIC_API_KEY; se usa el texto base.", flush=True)
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": HAIKU_MODEL,
                "max_tokens": max_tokens,
                "thinking": {"type": "disabled"},          # Haiku 5.5 piensa por defecto y se come los max_tokens
                "system": system,
                "messages": [{"role": "user", "content": mensaje}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        cuerpo = resp.json()
        texto = "".join(b.get("text", "") for b in cuerpo.get("content", []) if b.get("type") == "text").strip()
        if not texto:                                     # sin bloque de texto: deja en el log qué llegó
            tipos = [b.get("type") for b in cuerpo.get("content", [])]
            print(f"[haiku {etiqueta}] sin texto · bloques={tipos} · stop_reason={cuerpo.get('stop_reason')}", flush=True)
            avisar_admin("haiku", f"Haiku respondió sin texto (stop_reason={cuerpo.get('stop_reason')}). Los resúmenes salen con el texto base.")
            return None
        return texto
    except requests.HTTPError as e:
        print(f"[haiku {etiqueta}] HTTP {e.response.status_code}: {e.response.text[:300]}", flush=True)
        credito = "credit" in e.response.text.lower()
        avisar_admin("haiku", ("Haiku se quedó sin crédito: recarga en console.anthropic.com." if credito
                               else f"Haiku falló (HTTP {e.response.status_code}).") + " Los resúmenes salen con el texto base.")
    except Exception as e:
        print(f"[haiku {etiqueta}] {type(e).__name__}: {e}", flush=True)
        avisar_admin("haiku", f"Haiku no respondió ({type(e).__name__}). Los resúmenes salen con el texto base.")
    return None

_RES_LOCAL = threading.local()       # por hilo: resúmenes pendientes (ensayo) y ya redactados


def resumir_actividad(linea, detalle="", personal=""):
    """Resume la actividad en UNA línea técnica y concisa, combinando línea principal
       y campo Actividad si existe. Fallback: texto original."""
    base = (linea or "").strip()
    extra = (detalle or "").strip()
    if extra and extra != base:
        base = f"{base}. {extra}" if base else extra
    if not base:
        return base
    clave = (linea, detalle, personal)
    if getattr(_RES_LOCAL, "pendientes", None) is not None:   # ensayo: solo se anota lo que se pediría
        _RES_LOCAL.pendientes.append(clave)
        return base
    listos = getattr(_RES_LOCAL, "listos", None)
    if listos and clave in listos:                             # ya lo redactó Haiku en paralelo
        return listos[clave]
    mensaje = f"Actividad: {base}" + (f"\nPersonal SEDATU: {personal}" if personal else "")
    tope = 320 if personal else 220                    # los nombres del personal alargan la línea
    resumen = _llamar_haiku(SYSTEM_RESUMEN, mensaje, 170 if personal else 120, 0.7, "resumen")
    # Si Haiku falló o pidió datos en vez de redactar, se usa el texto base.
    malo = (
        not resumen
        or "\n" in resumen
        or len(resumen) > tope
        or resumen.rstrip().endswith(":")
        or "?" in resumen
        or re.search(r'(?i)(necesito que|proporci|por favor|no puedo redactar|no cuento con|datos espec[ií]ficos)', resumen)
    )
    return base if malo else corregir_siglas(resumen)

CORRECCIONES = {
    "palacio municipal": "Palacio Municipal",
    "presidencia municipal": "Presidencia Municipal",
    "comisariado ejidal": "Comisariado Ejidal",
    "comisariado de bienes comunales": "Comisariado de Bienes Comunales",
    "nucleo agrario": "Núcleo Agrario",
    "núcleo agrario": "Núcleo Agrario",
    "registro agrario nacional": "Registro Agrario Nacional",
    "municipios": "Municipios",
    "municipios de": "Municipios de",
    "municipio de": "Municipio de",
}

# =========================
# CORRECCIÓN DE SIGLAS
# =========================
CORRECCIONES_SIGLAS = {
    r'Bienes\s+Dominiales\s+del\s+Tren': 'Bienes Distintos a la Tierra',
}

# Corrige siglas que Haiku suele expandir mal
def corregir_siglas(texto):
    for patron, correcto in CORRECCIONES_SIGLAS.items():
        texto = re.sub(patron, correcto, texto, flags=re.IGNORECASE)
    return texto

SYSTEM_RESUMEN = (
    "Redactas UNA sola línea de reporte institucional para actividades de campo "
    "de proyectos ferroviarios de SEDATU.\n"
    "Reglas:\n"
    "- En pasado, formal, técnica pero clara y directa; concisa (máx. 25 palabras).\n"
    "- Elige el verbo según la acción REAL, no uses siempre la misma fórmula. Guía: "
    "firma→'Se firmó'; reunión→'Se celebró reunión'; recorrido→'Se realizó recorrido'; "
    "acercamiento→'Se efectuó acercamiento'; entrega→'Se entregó'; notificación→'Se notificó'; "
    "verificación→'Se verificó'; levantamiento→'Se levantó'.\n"
    "- Conserva TEXTUAL: números de parcela (ej. MQ-SBA-P109), PKs (ej. pk 44+900), "
    "nombres de personas, ejidos/núcleos agrarios y dependencias.\n"
    "- No inventes datos que no estén. No agregues introducción ni explicación.\n"
    "- Responde SOLO con la línea, sin comillas.\n"
    "- El ejecutor SIEMPRE es SEDATU y las dependencias participantes; NUNCA un particular.\n"
    "- Las personas nombradas (entre paréntesis, como 'propietario:' o 'titular', o con Sr./Sra./C.) son los "
    "propietarios o afectados, no quien ejecuta. Si hay propietario, refiérelo como "
    "'del C. X'; NUNCA deduzcas Sr. o Sra. por el nombre. Si el texto ya trae Sr. o Sra., respétalo.\n"
    "- Si viene 'Personal SEDATU:', esos nombres son servidores públicos de SEDATU que ejecutaron la actividad "
    "(NUNCA propietarios ni afectados): empieza la línea con ellos y el verbo según la acción, ej. "
    "'Juan Pérez y Ana López, personal de SEDATU, se reunieron con el propietario del polígono MQ-SJR-130.' "
    "Conserva los nombres textuales; en ese caso puedes usar hasta 35 palabras.\n"
    "- NUNCA pidas más datos, NUNCA hagas preguntas, NUNCA pidas aclaraciones.\n"
    "- Aunque la actividad sea muy escueta (ej. 'Caminamiento Ejidal'), redáctala "
    "igual en pasado con lo que haya (ej. 'Se realizó caminamiento ejidal'). "
    "SIEMPRE devuelves UNA línea de reporte, jamás una solicitud de información.\n"
    "GLOSARIO DE SIGLAS (usa estos significados EXACTOS):\n"
    "- BDT / BDTs / BDT's: Bienes Distintos a la Tierra (construcciones, cultivos, árboles, cosechas u otros bienes sobre el terreno, distintos de la tierra misma)\n"
    "- COP / COPs / COP's / cop / cops (en cualquier combinación de mayúsculas y minúsculas): Convenio de Ocupación Previa. En plural: Convenios de Ocupación Previa.\n"
    "- IFREM / ifrem: Instituto de la Función Registral del Estado de México\n"
    "- Regla: si encuentras una sigla que NO está en este glosario, consérvala tal cual aparece. NUNCA inventes ni deduzcas el significado de una sigla."
)

SYSTEM_PUNTO_ENCUENTRO = (
    "Redactas UNA sola frase en español, tono institucional pero natural, que explique el "
    "punto de encuentro de una actividad de campo de un proyecto ferroviario de SEDATU, a "
    "partir de una nota cruda (a veces en mayúsculas, con redacción telegráfica).\n"
    "Reglas:\n"
    "- Una sola oración, en tiempo futuro: empieza con 'El punto de encuentro será...'.\n"
    "- Si la nota trae una hora (HH:MM), inclúyela tal cual con 'a las HH:MM'.\n"
    "- Si la nota da un motivo (ej. falta de señal), consérvalo como razón con 'debido a que...'.\n"
    "- Conserva TEXTUAL los nombres propios de lugares (ejidos, núcleos agrarios, salones, comunidades).\n"
    "- No inventes datos que no estén en la nota. No agregues introducción ni explicación.\n"
    "- Responde SOLO con la frase, sin comillas.\n"
    "- NUNCA pidas más datos, NUNCA hagas preguntas, NUNCA pidas aclaraciones."
)

def formatear_punto_encuentro(texto_ubic):
    """Reformula el texto crudo de 'Punto de encuentro'/'Punto de reunión' en una frase
       natural ('El punto de encuentro será... a las HH:MM debido a que...'), vía Haiku.
       Fallback: texto crudo con solo la primera letra en mayúscula, si no hay API key
       o la llamada falla."""
    base = (texto_ubic or "").strip()
    if not base:
        return ""
    fallback = base[0].upper() + base[1:].lower()
    frase = _llamar_haiku(SYSTEM_PUNTO_ENCUENTRO, f"Nota: {base}", 100, 0.5, "punto")
    malo = (
        not frase
        or "\n" in frase
        or len(frase) > 220
        or "?" in frase
        or re.search(r'(?i)(necesito que|proporci|por favor|no puedo redactar|no cuento con)', frase)
    )
    return fallback if malo else frase

# Capitaliza frases institucionales y nombres tras Municipio/Ejido.
def normalizar_capitalizacion(texto):
    if not texto:
        return texto
    # frases institucionales conocidas (case-insensitive)
    for mal, bien in CORRECCIONES.items():
        texto = re.sub(r'\b' + re.escape(mal) + r'\b', bien, texto, flags=re.IGNORECASE)
    # capitalizar minúsculas
    texto = re.sub(
        r'\b(Palacio Municipal|Presidencia Municipal|Municipio(?: de)?|Ejido(?: de)?)\s+(?!(?:de|del)\b)([a-záéíóúñ]+)',
        lambda m: f"{m.group(1)} {m.group(2).capitalize()}",
        texto
    )
    return texto

_PALABRAS_MINUSCULAS_NOMBRE = {"de", "del", "la", "las", "los", "y", "en"}

# Pasa un nombre en MAYÚSCULAS a Tipo Título (de, del, la... en minúscula).
def capitalizar_nombre_propio(texto):
    if not texto:
        return texto
    letras = [c for c in texto if c.isalpha()]
    if not letras or not all(c.isupper() for c in letras):
        return texto
    palabras = texto.split(' ')
    resultado = []
    for i, palabra in enumerate(palabras):
        base = palabra.lower()
        if i > 0 and base in _PALABRAS_MINUSCULAS_NOMBRE:
            resultado.append(base)
        else:
            resultado.append(base[:1].upper() + base[1:] if base else base)
    return ' '.join(resultado)

_EJIDO_PALABRA = r'[A-ZÁÉÍÓÚÑ]{2,}'
_EJIDO_CONECTOR = r'(?i:de|del|la|las|los)'
_NOMBRE_EJIDO_SUB_RE = re.compile(
    r'(?i:\b(?:Comisariado\s+Ejidal\s+de|Ejidal\s+de|Ejidos?\s+de|Ejidos?))\s+'
    r'(["\u2018\u2019\u201c\u201d\']?)'
    rf'({_EJIDO_PALABRA}(?:\s+(?:{_EJIDO_CONECTOR}\s+){{0,2}}{_EJIDO_PALABRA})*)'
)

# Capitaliza el nombre que sigue a 'ejido' dentro de un texto.
def normalizar_nombres_ejido(texto):
    if not texto:
        return texto

    # Reemplaza solo el nombre capitalizado, conservando el prefijo.
    def _reemplazo(m):
        nombre_cap = capitalizar_nombre_propio(m.group(2))
        return m.group(0)[:m.start(2) - m.start(0)] + nombre_cap

    return _NOMBRE_EJIDO_SUB_RE.sub(_reemplazo, texto)

CAMPOS = {
    "hora":         r'Hora',
    "horario":      r'Horario',
    "descripcion":  r'Descripci[oó]n|Asunto',
    "frente":       r'Frente|F',
    "bdts":         r'BDTs?',
    "nomenclatura": r'Nomenclaturas?',
    "poligono":     r'Pol[ií]gonos?',
    "asistentes":   r'Asistentes?|Asisten?|Participa(?:n|ntes)?',
    "ubicacion":    r'Ubicaci[oó]n(?:es)?|Punto de reuni[oó]n|Punto de encuentro',
    "ejido":        r'Ejido',
    "municipio":    r'Municipio',
    "parcelas":     r'Parcelas',
}

def frontera_campos(excluir=()):
    """Lookahead que cae justo antes de la etiqueta ':' de cualquier campo,
       excluyendo opcionalmente el campo que estás capturando."""
    pats = [p for k, p in CAMPOS.items() if k not in excluir]
    return r'(?:' + '|'.join(f'(?:{p})' for p in pats) + r'):'

# Precalculadas una sola vez (evita rearmar la cadena en cada bloque)
_FRONT_ALL    = frontera_campos()
_FRONT_BDTS   = frontera_campos(excluir=('bdts',))
_FRONT_FRENTE = frontera_campos(excluir=('frente',))
_FRONT_ASIS   = frontera_campos(excluir=('asistentes',))

def _frontera_fuera_parentesis(texto, patron):
    """Como re.search, pero ignora coincidencias que caigan dentro de un
       paréntesis todavía sin cerrar (ej. '(Frente 12 - Municipio: Querétaro)'
       donde 'Municipio:' es una nota entre paréntesis, no una etiqueta de
       campo nueva). Devuelve el índice de inicio de la primera coincidencia
       "real" (fuera de paréntesis), o None si no hay ninguna así."""
    for m in re.finditer(patron, texto, re.IGNORECASE):
        antes = texto[:m.start()]
        if antes.count('(') <= antes.count(')'):
            return m.start()
    return None

# =========================
# NOMENCLATURAS
# =========================
NOMEN_RE = re.compile(
    r'(?im)^\s*(?:Nomenclaturas?|Pol[ií]gonos?)\s*:\s*(.+?)\s*$'
)

INLINE_NOM_RE = re.compile(r'\b[A-Z]{2,4}-[A-Z]{2,4}-P?\d{1,4}(?:-\d+)?\b')

def extraer_nomenclaturas(bloque):
    """Extrae códigos de 'Nomenclaturas:/Polígonos:', ya sea en la MISMA línea
       (Nomenclaturas: MQ-.., MQ-..) o con la etiqueta sola y los códigos en las
       líneas de abajo (formato TMQ de BDTs). Ignora el sufijo '(09:30 hrs)'."""
    codigos = []
    for m in re.finditer(r'(?im)^\s*(?:Nomenclaturas?|Pol[ií]gonos?)\s*:(.*)$', bloque):
        region = m.group(1)                       # lo que va tras el ':' en la misma línea
        # anexa líneas siguientes hasta la próxima etiqueta de campo o línea vacía
        for linea in bloque[m.end():].splitlines():
            if not linea.strip():
                continue
            if re.match(rf'(?i)\s*{_FRONT_ALL}', linea):
                break
            region += "\n" + linea
        if region.strip().upper() == "N/A":
            continue
        codigos.extend(INLINE_NOM_RE.findall(region))
    # dedup conservando orden
    vistos = set()
    return [c for c in codigos if not (c in vistos or vistos.add(c))]

_NOM_HORA_RE = re.compile(
    r'\b([A-Z]{2,4}-[A-Z]{2,4}-P?\d{1,4}(?:-\d+)?)\b[^\n(]*\((\d{1,2}):(\d{2})\s*(?:hrs?)?\)'
)

def extraer_nomenclaturas_inline(texto):
    """Códigos tipo MQ-SMM-P081 que vienen dentro del texto de la actividad."""
    vistos = set()
    return [c for c in INLINE_NOM_RE.findall(texto or "")
            if not (c in vistos or vistos.add(c))]

PARCELA_SUELTA_RE = re.compile(r'(?m)^\s*(P-\d+[A-Z]?)\s*(?:\(([^)]+)\))?\s*$')

PARCELA_PUNTO_RE = re.compile(r'\bP\.\s?\d+[A-Z]?\b')

def extraer_parcelas_punto(texto):
    """Códigos de parcela tipo 'P.815' (con punto) dentro del texto de la actividad."""
    vistos = set()
    return [m.group(0).replace('. ', '.') for m in PARCELA_PUNTO_RE.finditer(texto or "")
            if not (m.group(0) in vistos or vistos.add(m.group(0)))]

def extraer_parcelas_sueltas(bloque):
    """Extrae líneas sueltas tipo 'P-117' o 'P-116 (Minuta)'."""
    out = []
    for m in PARCELA_SUELTA_RE.finditer(bloque):
        cod = m.group(1)
        if m.group(2):
            cod += f" ({m.group(2)})"
        out.append(cod)
    vistos = set()
    return [c for c in out if not (c in vistos or vistos.add(c))]

# Agrega al resumen los códigos que Haiku haya omitido.
def _integrar_codigos(resumen, codigos, singular, plural):
    if not codigos:
        return resumen
    faltantes = [c for c in codigos if c not in resumen]
    if not faltantes:
        return resumen
    resumen = resumen.rstrip(' .')
    lista = (faltantes[0] if len(faltantes) == 1
             else ", ".join(faltantes[:-1]) + " y " + faltantes[-1])
    if re.search(r'(?i)(predios?|pol[ií]gonos?|parcelas?|nomenclaturas?|inmuebles?)$', resumen):
        return f"{resumen}: {lista}"
    sufijo = singular if len(faltantes) == 1 else plural
    return f"{resumen} en {sufijo} {lista}."

# Asegura que las nomenclaturas aparezcan en el resumen.
def integrar_nomenclaturas(resumen, noms):
    return _integrar_codigos(resumen, noms, "la nomenclatura", "las nomenclaturas")

# Asegura que las parcelas aparezcan en el resumen.
def integrar_parcelas(resumen, parcelas):
    return _integrar_codigos(resumen, parcelas, "la parcela", "las parcelas")

# =========================
# HOMOLOGACIÓN DEL TIPO DE SOLICITUD (acentos, erratas y formas de BDTs/COPs)
# =========================
_HOMOLOGAR_PALABRAS = {
    'reunion': 'reunión', 'poligono': 'polígono', 'poligonos': 'polígonos', 'poligino': 'polígono',
    'busqueda': 'búsqueda', 'busquedas': 'búsquedas', 'area': 'área', 'areas': 'áreas',
    'tecnico': 'técnico', 'tecnicos': 'técnicos', 'tecnica': 'técnica', 'tecnicas': 'técnicas',
    'topografico': 'topográfico', 'topograficos': 'topográficos', 'topografica': 'topográfica',
    'topografia': 'topografía', 'cartografia': 'cartografía', 'infografia': 'infografía',
    'avaluo': 'avalúo', 'avaluos': 'avalúos', 'asesoria': 'asesoría', 'asesorias': 'asesorías',
    'tramite': 'trámite', 'tramites': 'trámites', 'publico': 'público', 'publica': 'pública',
    'rancheria': 'ranchería', 'notaria': 'notaría',
    'asercamiento': 'acercamiento', 'asercamientos': 'acercamientos', 'asamble': 'asamblea',
    'agroforetales': 'agroforestales', 'agroforetal': 'agroforestal',
    'levantamineto': 'levantamiento', 'distribucuion': 'distribución', 'distribucuión': 'distribución',
    'yconstrucción': 'y construcción', 'yconstruccion': 'y construcción',
}
_HOMOLOGAR_RE = re.compile(r'(?<!\w)(' + '|'.join(sorted(map(re.escape, _HOMOLOGAR_PALABRAS), key=len, reverse=True)) + r')(?!\w)', re.IGNORECASE)
_SIN_TILDE_CION_RE = re.compile(r'(?<!\w)(\w+?)([cs])ion(?!\w)', re.IGNORECASE)  # medicion → medición
_SIGLAS_PLURAL_RE = re.compile(r'(?<!\w)(BDT|COP)\s*(?:[´\'’`]\s*[sS]|S)(?!\w)', re.IGNORECASE)  # BDT´s → BDTs

# Respeta mayúsculas de la palabra original: «Medicion» → «Medición», «REUNION» → «REUNIÓN».
def _como_original(orig, nueva):
    if orig.isupper() and len(orig) > 1:
        return nueva.upper()
    return nueva[0].upper() + nueva[1:] if orig[0].isupper() else nueva

def homologar_tipo(txt):
    t = re.sub(r'[​-‍⁠﻿]', '', str(txt or ''))
    t = re.sub(r'\s+', ' ', t).strip().rstrip('.').strip()
    t = _HOMOLOGAR_RE.sub(lambda m: _como_original(m.group(1), _HOMOLOGAR_PALABRAS[m.group(1).lower()]), t)
    t = _SIN_TILDE_CION_RE.sub(lambda m: _como_original(m.group(0), (m.group(1) + m.group(2) + 'ión').lower()), t)
    t = _SIGLAS_PLURAL_RE.sub(lambda m: m.group(1).upper() + 's', t)
    return t[:1].upper() + t[1:]

# =========================
# CONTEO DE ACTIVIDADES POR CATEGORÍA (abril → hoy)
# =========================
# Catálogo homologado: categoría → acciones → patrones (texto en minúsculas y sin acentos).
# La acción es el nivel fino que se consulta («cuántas sensibilizaciones en junio»).
CATALOGO_ACTIVIDADES = [
    ("1. Atención a Reuniones/Asambleas/Asesorías", [
        ("Reunión", [r'reunion(?:es)?', r'encuentros?']),
        ("Asamblea", [r'asambleas?']),
        ("Asesoría", [r'asesorias?']),
        ("Mesa de trabajo", [r'mesas?\s+de\s+trabajo', r'mesas?\s+tecnicas?']),
    ]),
    ("2. Caminamientos/Inspecciones/Reconocimientos", [
        ("Caminamiento", [r'caminamientos?']),
        ("Inspección", [r'inspecci(?:on|ones)']),
        ("Recorrido", [r'recorridos?', r'visitas?\s+de\s+campo']),
        ("Reconocimiento", [r'reconocimientos?']),
        ("Vuelo de dron", [r'vuelos?\s+de\s+drones?', r'drones?']),
    ]),
    ("3. Levantamientos Agenda (Topográficos/BDTs Agroforestales y Construcción)", [
        ("Medición", [r'medici(?:on|ones)']),
        ("Levantamiento", [r'levantamientos?', r'topografic[oa]s?', r'poligonal(?:es)?', r'bdts?',
                           r'construcci(?:on|ones)', r'agroforestal(?:es)?', r'agricolas?']),
        ("Replanteo", [r'replante(?:os?|amientos?)']),
        ("Rectificación de medidas", [r'rectificaci(?:on|ones)\s+de\s+medidas']),
    ]),
    ("4. Gabinete (Cartografía/Infografía/Fichas BDT)", [
        ("Ficha BDT", [r'elaboraci(?:on|ones)\s+de\s+(?:las?\s+)?fichas?', r'fichas?']),
        ("Plano", [r'elaboraci(?:on|ones)\s+de(?:l|\s+los?)?\s+planos?', r'planos?']),
        ("Infografía", [r'elaboraci(?:on|ones)\s+de\s+(?:las?\s+)?infografias?', r'infografias?']),
        ("Cartografía", [r'elaboraci(?:on|ones)\s+de\s+(?:las?\s+|los?\s+)?(?:cartografias?|mapas?)',
                         r'cartografias?', r'mapas?']),
    ]),
    ("5. Firmas de convenios y actas", [
        ("Firma", [r'firmas?']),
        ("Minuta", [r'minutas?']),
    ]),
    ("6. Acercamientos/Sensibilización/Negociación", [
        ("Acercamiento", [r'a[cs]ercamientos?']),
        ("Sensibilización", [r'sensibilizaci(?:on|ones)']),
        ("Negociación", [r'negociaci(?:on|ones)']),
        ("Atención a afectados", [r'atenci(?:on|ones)', r'citas?\s+con']),
        ("Contacto con propietarios", [r'contactos?\s+con', r'localizaci(?:on|ones)\s+de\s+(?:los?\s+)?propietarios?']),
        ("Socialización", [r'socializaci(?:on|ones)',
                           r'explicaci(?:on|ones)\s+de\s+(?:la\s+|las\s+)?afectaci(?:on|ones)']),
    ]),
    ("7. Visitas/Revisiones/Verificaciones", [
        ("Visita", [r'visitas?']),
        ("Revisión", [r'revisi(?:on|ones)', r'revisar']),
        ("Verificación", [r'verificaci(?:on|ones)', r'verificar(?:a|an)?', r'evaluaci(?:on|ones)']),
        ("Identificación", [r'identificaci(?:on|ones)', r'ubicaci(?:on|ones)\s+de']),
    ]),
    ("8. Consultas y trámites registrales", [
        ("Consulta registral", [r'consultas?', r'catastro', r'notarias?', r'expedientes?']),
        ("Búsqueda de antecedentes", [r'busquedas?', r'antecedentes']),
        ("Recopilación de documentación", [r'recopilaci(?:on|ones)', r'recabar\s+documentaci(?:on|ones)']),
        ("Inscripción en RPP", [r'rpp', r'registro\s+publico', r'inscripci(?:on|ones)',
                                r'ingresos?\s+de\s+(?:convenios|solicitudes|documentos?)']),
        ("Certificación", [r'certificaci(?:on|ones)']),
        ("Seguimiento de trámite", [r'seguimientos?\s+(?:a|de)\s+(?:las?\s+|los?\s+)?(?:solicitudes|oficios?)']),
    ]),
    ("9. Peritajes/Avalúos/Acompañamientos", [
        ("Peritaje", [r'peritajes?', r'peritos?', r'levantamientos?\s+de\s+peritajes?']),
        ("Avalúo", [r'avaluos?']),
        ("Acompañamiento", [r'acompanamientos?']),
        ("Asistencia técnica", [r'asistencias?\s+tecnicas?']),
    ]),
    ("10. Marcaje/Delimitación", [
        ("Marcaje", [r'marcajes?']),
        ("Delimitación", [r'delimitaci(?:on|ones)']),
    ]),
    ("11. Mesas sociales/Convocatorias/Videoconferencias", [
        ("Mesa de atención social", [r'mesas?\s+(?:de\s+atencion|social(?:es)?)(?:\s+social(?:es)?)?']),
        ("Videoconferencia", [r'videoconferencias?']),
        ("Convocatoria", [r'convocatorias?']),
        ("Conciliación", [r'conciliaci(?:on|ones)']),
    ]),
    ("12. Censos/Reubicaciones", [
        ("Censo", [r'censos?', r'levantamientos?\s+de\s+(?:los?\s+)?censos?']),
        ("CIS", [r'cis', r'cedulas?\s+de\s+informacion',
                 r'(?:levantamientos?|aplicaci(?:on|ones))\s+de\s+(?:las?\s+)?(?:cis|cedulas?)']),
        ("Reubicación", [r'reubicaci(?:on|ones)', r'reubicar', r'desocupaci(?:on|ones)']),
        ("Inventario de bienes", [r'inventarios?']),
    ]),
    ("13. Presentaciones/Entregas", [
        ("Presentación", [r'presentaci(?:on|ones)']),
        ("Entrega", [r'(?<!acta\s)(?<!actas\s)(?<!acta\sde\s)(?<!actas\sde\s)entregas?', r'recepci(?:on|ones)']),
        ("Notificación", [r'notificaci(?:on|ones)']),
    ]),
    ("14. Seguimiento de afectaciones", [
        ("Seguimiento de afectaciones", [r'seguimientos?\s+(?:a\s+|de\s+|en\s+)?(?:las?\s+|los?\s+)?afectaci(?:on|ones)',
                                         r'seguimientos?\s*[-–:]', r'seguimientos?\s+a\s+las\s+areas']),
    ]),
    ("15. Anuencias y permisos", [
        ("Anuencia", [r'anuencias?']),
        ("Permiso", [r'permisos?']),
    ]),
]

CAT3_NOMBRE = "3. Levantamientos Agenda (Topográficos/BDTs Agroforestales y Construcción)"
CAT3_SUB = [
    ("BDTs Construcción",  [r'construcci(?:on|ones)']),
    ("BDTs Agroforestal",  [r'agroforestal(?:es)?', r'agricolas?']),
]
CAT3_GENERAL = ("Topográficos/Mediciones", [])
# Lista (nombre, patrones) de las categorías fuera de la 3, como la leen el conteo y el mapa.
CATEGORIAS_CONTEO = [(c, [p for _, ps in acc for p in ps]) for c, acc in CATALOGO_ACTIVIDADES if c != CAT3_NOMBRE]

# Compila los patrones de una categoría como palabras completas.
def _compilar_cat(pats):
    return [re.compile(r'(?<!\w)' + p + r'(?!\w)') for p in pats]

_CAT3_SUB_PAT = [(n, _compilar_cat(p)) for n, p in CAT3_SUB]
# Sustantivos que nombran el objeto, no la acción: no abren una acción coordinada («firma de COP y BDT»).
_OBJETOS = {r'bdts?', r'fichas?', r'planos?', r'mapas?', r'construcci(?:on|ones)', r'agroforestal(?:es)?', r'agricolas?'}
# (categoría, acción, patrón compilado, ¿puede abrir una acción coordinada?)
_PATRONES_ACCION = [(c, a, re.compile(r'(?<!\w)' + p + r'(?!\w)'), p not in _OBJETOS)
                    for c, acc in CATALOGO_ACTIVIDADES for a, ps in acc for p in ps]
ACCIONES = [a for _, acc in CATALOGO_ACTIVIDADES for a, _ in acc]

# Separadores entre acciones: «y», «así como», «;», «,», guion, paréntesis, saltos y listas numeradas.
_SEP_ACCION = re.compile(r'\s+(?:y|e|asi\s+como)\s+|\s*[;,\n()]\s*|\s+[-–]\s+|(?<!\w)\d+[.)]\s+')
# Palabras que pueden ir antes del verbo sin cambiar la acción.
_RELLENO = re.compile(r'(?:(?:y|e|de|del|la|las|el|los|a|al|en|se|realizar|continuacion\s+de|'
                      r'primera|primer|segunda|segundo|tercera|tercer|nueva|nuevo)\s+)*')

# Acción de un texto: la coincidencia más temprana (y la más larga si empatan) → (inicio, categoría, acción).
def _accion(txt, secundaria=False):
    mejor = None
    for cat, acc, p, abre in _PATRONES_ACCION:
        if secundaria and not abre:
            continue
        m = p.search(txt)
        if m and (mejor is None or (m.start(), -len(m.group())) < (mejor[0], -mejor[1])):
            mejor = (m.start(), len(m.group()), cat, acc)
    return mejor and (mejor[0], mejor[2], mejor[3])

# Acciones de una actividad: la principal y las que se coordinan con «y», «,», «;»…
# (una palabra suelta a media frase no suma otra acción). → [(categoría, acción)]
def acciones_actividad(tipo_solicitud):
    txt = _sin_acentos(str(tipo_solicitud or "")).lower()
    principal = _accion(txt)
    if not principal:
        return []
    salida = [principal[1:]]
    for trozo in _SEP_ACCION.split(txt)[1:]:
        trozo = trozo.strip()
        a = _accion(trozo, secundaria=True)
        if a and a[0] == _RELLENO.match(trozo).end() and a[1:] not in salida:
            salida.append(a[1:])
    return salida

# Acciones que menciona un texto, en cualquier parte: sirve para preguntas y para filtrar
# («cuántas sensibilizaciones» incluye «Reunión de sensibilización»). BDT solo no es levantamiento.
def acciones_en_texto(texto):
    txt = _sin_acentos(str(texto or "")).lower()
    return sorted({a for c, a, p, abre in _PATRONES_ACCION
                   if (abre or c != CAT3_NOMBRE) and p.search(txt)}, key=ACCIONES.index)

# Clasifica la actividad en categorías de conteo (cada una una vez) y subcategorías de la 3.
def clasificar_actividad(tipo_solicitud, detalle=""):
    cats = sorted({c for c, _ in acciones_actividad(tipo_solicitud)}, key=lambda n: int(n.split('.')[0]))
    hits = []
    for nombre in cats:
        if nombre != CAT3_NOMBRE:
            hits.append((nombre, None))
            continue
        txt_sub = _sin_acentos(f"{tipo_solicitud or ''} {detalle or ''}").lower()
        subs = [n for n, pats in _CAT3_SUB_PAT if any(p.search(txt_sub) for p in pats)]
        hits += [(CAT3_NOMBRE, s) for s in subs] or [(CAT3_NOMBRE, "Topográficos/Mediciones")]
    return hits

# Convierte hora con am/pm a formato 24 h (HH:MM).
def _hora_24(hh, mm, mer):
    h = int(hh)
    if mer:
        mer = mer.lower()
        if mer.startswith('p') and h != 12:
            h += 12
        elif mer.startswith('a') and h == 12:
            h = 0
    return f"{h:02d}:{mm}"

# Separa párrafos extra pegados al último bloque numerado.
def _separar_adicionales(bloques):
    salida = []
    for b in bloques:
        trozos = re.split(r'\n[ \t]*\n', b)
        salida.append(trozos[0])
        for extra in trozos[1:]:
            extra = extra.strip()
            if not extra:
                continue
            # Solo es actividad nueva si repite hora o punto de reunión del bloque actual.
            es_hora = re.search(r'(?i)\bHora\b|\d{1,2}:\d{2}', extra)
            es_punto = re.search(r'(?i)Punto de reuni[oó]n|Punto de encuentro', extra)
            ya_hora = re.search(r'(?i)\bHora\b|\d{1,2}:\d{2}', salida[-1])
            ya_punto = re.search(r'(?i)Punto de reuni[oó]n|Punto de encuentro|Ubicaci[oó]n', salida[-1])
            if (es_hora and ya_hora) or (es_punto and not es_hora and ya_punto):
                salida.append(extra)
            else:                       
                salida[-1] += "\n" + extra
    return salida

# Prefijo del propietario: señor o señora explícito = Sr./Sra.; propietario(a)/titular = C.
def _trato_propietario(palabra):
    p = palabra.lower()
    if p.startswith(('señora', 'senora')):
        return "Sra."
    if p.startswith(('señor', 'senor')):
        return "Sr."
    return "C."

# Nomenclaturas de propiedad privada por proyecto («MQ» → MQ-TUL-047; «QRO-URB» → QRO-URB-001).
_PREF_PRIV = [pre for p in PROYECTOS.values() for pre in p.get("privada", [])]
_COD_PRIVADA_RE = re.compile("|".join(
    rf'\b{re.escape(pre)}-\d+\b' if "-" in pre else rf'\b{re.escape(pre)}-[A-Z]{{2,4}}-P?\d{{1,4}}(?:-\d+)?\b'
    for pre in _PREF_PRIV))

# Tipo de propiedad: código MQ/AP/QRO-URB = PRIVADA; ejido = SOCIAL; dudoso (?) = vacío (hay que llenar a mano).
def detectar_tipo_propiedad(bloque, linea, nucleo_txt):
    if re.search(r'(?i)dominio\s+pleno', bloque):
        return ""
    priv = bool(_COD_PRIVADA_RE.search(bloque))
    soc = bool(nucleo_txt) or bool(re.search(r'(?i)\bejid|uso\s+com[uú]n', linea))
    if priv and soc:
        return ""
    if priv:
        return "PRIVADA"
    if soc:
        return "SOCIAL"
    return ""

# Convierte el texto de una agenda en filas para Google Sheets (una por actividad).
# Sufijo de tramo/frente al final del título: «| F4-F5», «| TMQ, F10», «| F4-F5 MEX-QRO», «| Frente 9», «| QSL», «| TQSL T1».
_SUFIJO_FRENTE = re.compile(
    r'\s*\|\s*(?:(?:[A-ZÁÉÍÓÚ]{2,6}(?:-[A-ZÁÉÍÓÚ]{2,6})?\s*,?\s*)?(?:[Ff](?:rente)?|T)\s*\d+(?:\s*(?:-|–|,|y)\s*(?:[Ff](?:rente)?|T)?\s*\d+)*'
    r'(?:\s*,?\s*[A-ZÁÉÍÓÚ]{2,6}(?:-[A-ZÁÉÍÓÚ]{2,6})?)?|[A-ZÁÉÍÓÚ]{2,6}(?:-[A-ZÁÉÍÓÚ]{2,6})?)\s*$')
# Con coma o entre paréntesis: «…MQ-HUE-079-1, F1-F3», «… (Frente 9 - San Juan del Río, Querétaro)».
_SUFIJO_FRENTE_COMA = re.compile(r'\s*(?:,\s*[Ff](?:rente)?\s*\d+(?:\s*(?:-|–|,|y)\s*(?:[Ff](?:rente)?)?\s*\d+)*'
                                 r'|\(\s*[Ff](?:rente)?\s*\d+[^()]*\))\s*\.?\s*$')


# Ubicación pegada al final del TIPO DE SOLICITUD (ya va en NÚCLEO, MUNICIPIO y ESTADO).
_UBIC_GUION_RE = re.compile(r'(?i)\s+[—–-]\s+(?:(?:el\s+)?ejidos?|n[uú]cleo\s+agrario|comunidad|municipios?\s+de|mpio\.?\s+de)\b.*$')
_UBIC_PAREN_RE = re.compile(r'(?i)\s*\(\s*(?:municipios?\s+de|mpio\.?\s+de|ejidos?)\b[^()]*\)\s*$')
_EDO_ABREV = {"hgo", "qro", "gto", "slp", "jal", "nl", "coah", "sin", "mex", "edomex", "edo mex", "edo de mex",
              "edo de mexico", "estado de mexico", "cdmx", "tamps", "mich", "ver", "coahuila", "veracruz", "michoacan"}
_LUGARES = {}

def _clave_lugar(s):
    s = re.sub(r'[.]', '', _sin_acentos(s or '').lower())
    s = re.sub(r'^(?:municipios?\s+de|mpio\s+de|mpio)\s+', '', s.strip())
    return re.sub(r'\s+', ' ', s).strip()

def _es_estado(s):
    if "edos" not in _LUGARES:
        _LUGARES["edos"] = {_clave_lugar(p["NOM_ENT"]) for p in MUN_PROPS} | _EDO_ABREV
    return _clave_lugar(s) in _LUGARES["edos"]

def _es_municipio(s, corto=False):
    if "muns" not in _LUGARES:
        _LUGARES["muns"] = {_clave_lugar(p["NOM_MUN"]) for p in MUN_PROPS}
    s = re.sub(r'(?i)^(?:del|en\s+el)\s+', '', s.strip())
    if re.match(r'(?i)municipios?\b|mpio\b', s):
        return True                                      # «Municipio (de) X» siempre es ubicación
    k = _clave_lugar(s)
    if not s[:1].isupper():
        return False
    return k in _LUGARES["muns"] or (corto and len(k) >= 4 and any(m.startswith(k + " ") for m in _LUGARES["muns"]))

def _sin_ubicacion(txt):
    """«… — Ejido La Pila (Municipio de San Luis Potosí)» o «…, Tula de Allende, Hidalgo» → sin la ubicación."""
    t = _UBIC_PAREN_RE.sub('', txt.strip().rstrip('.')).strip()
    t = _UBIC_GUION_RE.sub('', t).strip()
    for _ in range(2):                                   # «…, Municipio X, Nuevo León»: estado y luego municipio
        m = re.match(r'^(.*\S)\s*[,|]\s*([^,|]+?)\s*[,|]\s*([^,|]+?)\s*$', t)
        if m and _es_estado(m.group(3)) and (_es_municipio(m.group(2), corto=True) or _es_estado(m.group(2))):
            t = m.group(1)
            continue
        m = re.match(r'^(.*\S)\s*[,|]\s*([^,|]+?)\s*$', t)
        if m and (_es_estado(m.group(2)) or _es_municipio(m.group(2))):
            t = m.group(1)
            continue
        break
    t = t.rstrip(' ,;-–—')
    return t or txt                                      # si no queda nada, se deja como venía


def procesar_agenda(texto):
    texto = _INVISIBLES_RE.sub('', texto)
    texto = re.sub(r'(?m)^([ \t]*)\*[ \t]+', r'\1- ', texto)
    texto = texto.replace('*', '')
    texto = re.sub(r'(?m)^\s*_+|_+\s*$', '', texto)

    # --- PROYECTO ---
    # "Proyecto:" al inicio de línea tiene prioridad sobre "Agenda ..." (encabezado con fecha).
    proyecto_match = (re.search(r"(?im)^\W*Proyecto(?:\s+Ferroviario)?\s*:?\s*(.*)", texto)
                      or re.search(r"\b(?:Agenda|Proyecto)(?:\s+Ferroviario)?\s*:?\s*(.*)",
                                   texto, re.IGNORECASE))
    proyecto_raw = proyecto_match.group(1).strip() if proyecto_match else ""
    # "T1" = Segmento 1, se agrega al texto de cada actividad.
    m_seg = re.search(r'(?i)\bT(\d+)\b', proyecto_raw)
    segmento = f"Segmento {m_seg.group(1)}" if m_seg else ""
    if not proyecto_raw:
        proyecto_final = "SIN PROYECTO"
    else:
        # Clave en cualquier palabra, de derecha a izquierda ("TQSL T1" → TQSLP).
        ultimo = ""
        for tok in reversed(proyecto_raw.split()):
            cand = _ALIAS_CLAVE.get(_norm(tok), _norm(tok))
            if cand not in _PROY_POR_CLAVE:
                cand = _mas_parecida(cand, _PROY_POR_CLAVE)
            if cand in _PROY_POR_CLAVE:
                ultimo = cand
                break
        if ultimo in _PROY_POR_CLAVE:                          
            proyecto_final = _PROY_POR_CLAVE[ultimo]
        elif _proyecto_por_nombre(proyecto_raw):
            proyecto_final = _proyecto_por_nombre(proyecto_raw)
        else:
            proyecto_final = proyecto_raw                      
    if proyecto_final not in mapa_proyectos.values():
        m_clave = _CLAVES_RE.search(texto[:200])
        if m_clave:
            proyecto_final = mapa_proyectos[m_clave.group(1).upper()]
        else:
            clave_abrev = _resolver_abrev_tramo(texto[:200])
            if clave_abrev:
                proyecto_final = mapa_proyectos[clave_abrev]
    # --- FECHA ---
    fecha_num_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", texto)
    if fecha_num_match and 1 <= int(fecha_num_match.group(2)) <= 12:
        dia = fecha_num_match.group(1).zfill(2)
        mes = fecha_num_match.group(2).zfill(2)
        anio = fecha_num_match.group(3)
        fecha = f"{dia}/{mes}/{anio}"
        fecha_dt = datetime.strptime(fecha, "%d/%m/%Y")
        fecha_solicitud = (fecha_dt - timedelta(days=1)).strftime("%d/%m/%Y")
    else:
        fecha = ""
        fecha_solicitud = ""
        for m in re.finditer(r"(\d{1,2})\s+de\s+(\w+)(?:\s+de\s+(\d{4}))?", texto, re.IGNORECASE):
            mes_texto = m.group(2).lower()
            if mes_texto in meses:
                dia = m.group(1).zfill(2)
                anio = m.group(3) if m.group(3) else str(datetime.now(MX).year)
                fecha = f"{dia}/{meses[mes_texto]}/{anio}"
                fecha_dt = datetime.strptime(fecha, "%d/%m/%Y")
                fecha_solicitud = (fecha_dt - timedelta(days=1)).strftime("%d/%m/%Y")
                break

    # --- BLOQUES ---
    partes_texto = re.split(r"\n\s*\d+[ \t]*[\.-]\s*", texto)   # «1.», «1-» y «1 .»
    encabezado = partes_texto[0]
    bloques = partes_texto[1:]

    # Párrafos "adicionales" sin número que quedan pegados al último bloque numerado.
    bloques = _separar_adicionales(bloques)

    if not bloques:
        cuerpo = "\n".join(
            l for l in texto.splitlines()
            if not re.match(r"\s*Proyecto\b", l, re.IGNORECASE)
            and not re.search(r"\b\d{1,2}\s+de\s+(?:%s)\b" % "|".join(meses), l, re.IGNORECASE)
        ).strip()
        if cuerpo:
            trozos = [t.strip() for t in re.split(r'(?im)^(?=[ \t]*Hora\s*:)', cuerpo) if t.strip()]
            if len(trozos) > 1 and not re.match(r'(?i)Hora\s*:', trozos[0]):
                trozos = [trozos[0] + "\n" + trozos[1]] + trozos[2:]
            bloques = trozos if len(trozos) > 1 else [cuerpo]
    filas = []

    for bloque in bloques:
        bloque = bloque.replace('\xa0', ' ').strip()

        
        # HORA DOBLE (dos zonas horarias): "08:00 hrs (Hora Centro) / 09:00 hrs (Hora Nuevo Laredo)"
        hora_doble_match = re.search(
            r"(\d{1,2}):(\d{2})\s*hrs?\.?\s*\(\s*Hora\s+([^)]+?)\s*\)\s*/\s*"
            r"(\d{1,2}):(\d{2})\s*hrs?\.?\s*\(\s*Hora\s+([^)]+?)\s*\)",
            bloque, re.IGNORECASE
        )

        # HORA DOBLE (misma zona): "Hora: 10:00 hrs y 12:00 hrs"
        hora_doble_y_match = re.search(
            r"Hora:\s*(\d{1,2}):(\d{2})\s*(?:hrs?\.?)?\s+y\s+"
            r"(\d{1,2}):(\d{2})\s*(?:hrs?\.?)?",
            bloque, re.IGNORECASE
        )

        # HORA  
        match_hora = re.search(
            r"(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?\s*[Mm]\.?)?\s*(?:hrs?)?\s*[-–—]",
            bloque, re.IGNORECASE
        )
        hora_label_match = re.search(
            r"Hora:\s*(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?\s*[Mm]\.?)?",
            bloque, re.IGNORECASE
        )
        hora_sola_match = re.search(
            r"(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?|h(?:oras?|rs?))\b",
            bloque, re.IGNORECASE
        )
        hora_entera_match = re.search(
            r"Hora:\s*(\d{1,2})\s*(?:hrs?|horas?)\b",
            bloque, re.IGNORECASE
        )
        if re.search(r'(?i)Hora\s*:\s*(?:Por\s+confirmar|Pendiente)', bloque):
            hora_txt = "Por confirmar"
        elif hora_doble_match:
            h1 = _hora_24(hora_doble_match.group(1), hora_doble_match.group(2), None)
            z1 = hora_doble_match.group(3).strip()
            h2 = _hora_24(hora_doble_match.group(4), hora_doble_match.group(5), None)
            z2 = hora_doble_match.group(6).strip()
            hora_txt = f"{h1} (Hora {z1}) / {h2} (Hora {z2})"   # todo en un renglón
        elif hora_doble_y_match:
            h1 = _hora_24(hora_doble_y_match.group(1), hora_doble_y_match.group(2), None)
            h2 = _hora_24(hora_doble_y_match.group(3), hora_doble_y_match.group(4), None)
            hora_txt = f"{h1} y {h2}"
        elif match_hora:
            hora_txt = _hora_24(match_hora.group(1), match_hora.group(2), match_hora.group(3))
        elif hora_label_match:
            hora_txt = _hora_24(hora_label_match.group(1), hora_label_match.group(2), hora_label_match.group(3))
        elif hora_sola_match:
            hora_txt = _hora_24(hora_sola_match.group(1), hora_sola_match.group(2), hora_sola_match.group(3))
        elif hora_entera_match:
            hora_txt = _hora_24(hora_entera_match.group(1), "00", None)
        else:
            hora_txt = ""

        # LÍNEA PRINCIPAL
        match_desc = re.search(r"(?im)^\s*(?:Descripci[oó]n|Actividad|Asunto)\b\s*:?\s*(\S[^\n]*)", bloque)
        match_inline = re.search(r"\d{1,2}:\d{2}\s*(?:hrs?)?\s*[-–—]\s*(.*)", bloque, re.IGNORECASE)
        _sin_actividad_explicita = False
        if match_desc:
            linea_principal = match_desc.group(1).strip()
        elif match_inline:
            linea_principal = match_inline.group(1).strip()
        else:
            _sin_actividad_explicita = True
            linea_principal = ""
            for l in bloque.splitlines():
                l = l.strip()
                if not l:
                    continue
                m_hora_txt = re.match(
                    r'(?i)Hora\s*:\s*\d{1,2}(?::\d{2})?\s*(?:hrs?\b\.?|horas?\b|[ap]\.?\s*m\b\.?)?(?:\s*[-–—,.]\s*|\s+)'
                    r'(?!hrs?\b|horas?\b|[ap]\.?\s*m\b|y\s+\d|\(|/)([A-Za-zÁÉÍÓÚÑáéíóúñ].*)', l)
                if m_hora_txt:
                    linea_principal = m_hora_txt.group(1).strip()
                    break
                if re.match(rf'(?i){_FRONT_ALL}', l):
                    continue
                if re.match(r'https?://', l):
                    continue
                if re.fullmatch(r'\d+[\.\-]?', l):       
                    continue
                if re.fullmatch(r'\d{1,2}:\d{2}\s*(?:hrs?|horas?|h|[ap]\.?\s*m\.?)?\.?', l, re.IGNORECASE):
                    continue                             
                linea_principal = l
                break
            if not linea_principal:                      
                linea_principal = bloque.splitlines()[0].strip()
        _idx_frontera = _frontera_fuera_parentesis(linea_principal, rf'\s+(?={_FRONT_ALL})')
        if _idx_frontera is not None:
            linea_principal = linea_principal[:_idx_frontera].strip()

        linea_principal = re.sub(r'^\d{1,2}:\d{2}\s*(?:hrs?)?\.?\s*[-–—]\s*', '', linea_principal, flags=re.IGNORECASE).strip()
        linea_principal = re.sub(r'(?i)^\s*buen[oa]s?\s+(?:noches|tardes|d[ií]as)[\s,\.]*', '', linea_principal).strip()
        linea_principal = re.sub(r'(?i)^\s*adicional\s+a\s+la\s+agenda[^,]*,\s*', '', linea_principal).strip()
        linea_principal = re.sub(r'(?i)^\s*del\s+Frente\s+\d+[\s,]*', '', linea_principal).strip()
        linea_principal = re.sub(_SUFIJO_FRENTE, '', linea_principal).strip()  # «| F4-F5», «| TMQ, F10», «| F4-F5 MEX-QRO», «| QSL», «| TQSL T1»
        linea_principal = re.sub(_SUFIJO_FRENTE_COMA, '', linea_principal).strip()  # «…, F1-F3» o «… (Frente 9 - …)»
        linea_principal = re.sub(r'^[-–—\s]+', '', linea_principal).strip()
        linea_principal = re.sub(r'(?i)^(?:tipo(?:\s+de\s+(?:actividad|solicitud))?|descripci[oó]n|actividad|asunto)\s*:\s*', '', linea_principal).strip()   # «Tipo: Reunión…» sin la etiqueta
        if linea_principal[:1].islower():
            linea_principal = linea_principal[0].upper() + linea_principal[1:]
        linea_principal = re.sub(r'\.$', '', linea_principal).strip()
        linea_principal = normalizar_capitalizacion(linea_principal)
        linea_principal = normalizar_nombres_ejido(linea_principal)
        if segmento and segmento.lower() not in linea_principal.lower():
            linea_principal = f"{linea_principal} ({segmento})"

        actividad_txt = _sin_ubicacion(linea_principal)   # el resumen y el núcleo siguen usando la línea completa

        # CAMPO Actividad:
        actividad_field = re.search(r"Actividad:\s*(.*?)(?=\n\s*\w+:|$)", bloque, re.IGNORECASE | re.DOTALL)
        actividad_detalle = ""
        if actividad_field and actividad_field.group(1).strip():
            actividad_raw = actividad_field.group(1).strip()
            actividad_detalle = re.sub(r'\s*\n\s*[•\-]\s*', ' | ', actividad_raw)
            actividad_detalle = re.sub(r'^[•\-]\s*', '', actividad_detalle).strip()

        # FRENTE
        frente = re.search(
            rf"(?<!\w)(?:Frente|F):\s*([^\n]+?)(?=(?:\s|[-•])+{_FRONT_FRENTE}|[\r\n]|$)",
            bloque, re.IGNORECASE
        )
        if not frente:
            frentes_multi = re.search(
                r'\bFrentes?\s+(\d+(?:\s*[,y]\s*\d+)*)', bloque, re.IGNORECASE
            )
            if frentes_multi and re.search(r'[,y]', frentes_multi.group(1)):
                nums = re.findall(r'\d+', frentes_multi.group(1))
                lista = "-".join(nums)
                frente = type('_', (), {'group': lambda self, n: lista})()
        if not frente:
            frente_rep = re.search(
                r'\bFrentes?\s+\d+(?:\s*(?:,|y)\s*frentes?\s+\d+)+', bloque, re.IGNORECASE
            )
            if frente_rep:
                nums = re.findall(r'\d+', frente_rep.group(0))
                lista = "-".join(nums)
                frente = type('_', (), {'group': lambda self, n: lista})()
        if not frente:
            frente_rango = re.search(
                r'\bF\.?\s*(\d+)\s*[-–—]\s*F?\.?\s*(\d+)\b', bloque, re.IGNORECASE
            )
            if frente_rango:
                _lista = f"{frente_rango.group(1)}-{frente_rango.group(2)}"
                frente = type('_', (), {'group': lambda self, n: _lista})()        
        if not frente:
            frente_inline = re.search(r'\bF(?:rente)?\.?\s*(\d+)\b', linea_principal, re.IGNORECASE)
            if frente_inline:
                num = frente_inline.group(1)
                frente = type('_', (), {'group': lambda self, n: num})()
        if not frente:
            frente_pipe = re.search(r'[|,(]\s*(?:[A-ZÁÉÍÓÚ]{2,6}(?:-[A-ZÁÉÍÓÚ]{2,6})?\s*,\s*)?F(?:rente)?\.?\s*(\d+)\b', bloque, re.IGNORECASE)
            if frente_pipe:
                num = frente_pipe.group(1)
                frente = type('_', (), {'group': lambda self, n: num})()
        if not frente:
            frente_bloque = re.search(r'\bFrente\s+(\d+)\b', bloque, re.IGNORECASE)
            if frente_bloque:
                num = frente_bloque.group(1)
                frente = type('_', (), {'group': lambda self, n: num})()
        if not frente:
            frente_enc = re.search(r'\bFrente\s+(\d+)\b', encabezado, re.IGNORECASE)
            if frente_enc:
                num = frente_enc.group(1)
                frente = type('_', (), {'group': lambda self, n: num})()

        # POLÍGONO
        poligono = re.search(r"Pol[ií]gono:\s*(.*)", bloque, re.IGNORECASE)

        # MUNICIPIO
        municipio_match = re.search(r"Municipio:\s*(.*)", bloque, re.IGNORECASE)
        if not municipio_match:
            municipio_match = re.search(r"Municipio:\s*(.*)", encabezado, re.IGNORECASE)
        municipio = municipio_match.group(1).strip().rstrip('.').strip() if municipio_match else ""
        if municipio_match and municipio.endswith(')'):
            linea_actual = bloque[:municipio_match.start()].rsplit('\n', 1)[-1]
            if linea_actual.count('(') > linea_actual.count(')'):
                municipio = municipio[:-1].strip()

        # MUNICIPIO/ESTADO alterno
        municipio_inline_match = re.search(
            r'Municipio\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ.\'\s]+?)\s*,\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+)\s*\)',
            bloque
        )
        if municipio_inline_match:
            municipio_txt_inline = municipio_inline_match.group(1).strip()
            estado_txt_inline = _sin_acentos(municipio_inline_match.group(2).strip()).upper()
        else:
            municipio_txt_inline = ""
            estado_txt_inline = ""

        # MUNICIPIO plural
        municipios_multi_match = re.search(r'Municipios\s+de\s+([^\n]+)', bloque, re.IGNORECASE)
        municipio_multi_txt = ""
        if municipios_multi_match:
            _partes_mun = [p.strip(' .') for p in
                           re.split(r'\s*,\s*', municipios_multi_match.group(1).strip())
                           if p.strip(' .')]
            if len(_partes_mun) >= 2:
                _lugares_mun = _partes_mun[:-1]          # el último elemento es el Estado
                _sub_mun = re.split(r'\s+y\s+', _lugares_mun[-1], maxsplit=1)
                if len(_sub_mun) == 2:
                    _lugares_mun = _lugares_mun[:-1] + _sub_mun
                municipio_multi_txt = (
                    ", ".join(_lugares_mun[:-1]) + " y " + _lugares_mun[-1]
                    if len(_lugares_mun) > 1 else _lugares_mun[0]
                )

        # ASISTENTES
        asistentes = re.search(
            rf"(?:Asistentes?|Asisten?|Participa(?:n|ntes)?):\s*([^\n]+?)(?=(?:\s|[-•])+{_FRONT_ASIS}|[\r\n]|$)",
            bloque, re.IGNORECASE
        )
        # Fallback: frente pegado a la dependencia en Asistentes ("SEDATU F7", "DEFENSA F3 y F4")
        if not frente and asistentes:
            # multi: "F3 y F4", "F8, F9 y F10" (la F puede o no repetirse por número)
            f_multi = re.search(
                r'\bF\.?\s*(\d+(?:\s*[,y]\s*F?\.?\s*\d+)+)', asistentes.group(1), re.IGNORECASE
            )
            if f_multi:
                nums = re.findall(r'\d+', f_multi.group(1))
                lista = ", ".join(nums[:-1]) + " y " + nums[-1] if len(nums) > 1 else nums[0]
                frente = type('_', (), {'group': lambda self, n: lista})()
            else:
                f_asis = re.search(r'\bF\.?\s*(\d+)\b', asistentes.group(1))
                if f_asis:
                    num = f_asis.group(1)
                    frente = type('_', (), {'group': lambda self, n: num})()
        # Fallback: frente en el encabezado ("Agenda de caminamientos F3 TQSLP", "F2, F7")
        if not frente:
            f_enc = re.search(r'\bF\.?\s*(\d+(?:\s*(?:,|y)\s*F?\.?\s*\d+)*)\b', encabezado, re.IGNORECASE)
            if f_enc:
                _lista_enc = "-".join(re.findall(r'\d+', f_enc.group(1)))
                frente = type('_', (), {'group': lambda self, n: _lista_enc})()

        # UBICACIÓN
        ubicacion = re.search(
            r"\b(?:Ubicaci[oó]n|Punto de reuni[oó]n|Punto de encuentro)\s*:?\s*[\n\r]*\s*(?:\[.*?\]\((https?://[^\)\s]+)\)|(https?://[^\s\]]+)|(.+))",
            bloque,
            re.IGNORECASE
        )
        url = ""
        texto_ubic = ""
        url_directa = False
        if ubicacion:
            url = (ubicacion.group(1) or ubicacion.group(2) or "").strip()
            texto_ubic = (ubicacion.group(3) or "").strip().rstrip('.').strip()
            url_directa = bool(url)
        if not url:
            url_suelta = re.search(r'(?m)^\s*[-•]?\s*(https?://\S+)\s*$', bloque)
            if url_suelta:
                url = url_suelta.group(1).strip()
        if not url:                   # dirección + link en la misma línea: saca el link
            m_embed = re.search(r'https?://[^\s)\]]+', texto_ubic)
            url = m_embed.group(0).strip() if m_embed else texto_ubic
        nota_punto_encuentro = ""
        if texto_ubic and url and not url_directa and url.startswith('http'):
            _ubic_sin_url = re.sub(r'\(?\s*https?://[^\s)\]]+\s*\)?', '', texto_ubic).strip(' -–:,;')
            if _ubic_sin_url:
                nota_punto_encuentro = formatear_punto_encuentro(_ubic_sin_url)
        direccion_extra = ""
        _direccion_match = re.search(
            r"(?im)^\s*Ubicaci[oó]n(?:es)?\s*:\s*(?!https?://)(\S[^\n]*)", bloque
        )
        if _direccion_match:
            _cand = _direccion_match.group(1).strip().rstrip('.').strip()
            if _cand and _cand.upper() != "N/A" and _cand.lower() != texto_ubic.lower():
                direccion_extra = _cand

        estado_geo = ""
        municipio_geo = ""
        if _es_url_maps(url):
            lat, lng = extraer_coordenadas(url)
            if lat is not None:
                estado_geo, municipio_geo = obtener_estado_municipio(lat, lng)

        # UBICACIONES MÚLTIPLES  (a) Punto: url   |   - Punto: url
        ubicaciones_multi = re.findall(
            r'^\s*(?:[a-zA-Z][\)\.]|[-•])\s*(?!\s*(?i:ubicaci[oó]n(?:es)?|link|liga|maps)\s*:)(.+?):\s*(https?://\S+)',   # «- Ubicación: url» no es un punto con nombre
            bloque, re.MULTILINE
        )
        # UBICACIONES POR CÓDIGO: "MQ-NOP-160: https://..." (una fila por nomenclatura)
        ubic_por_codigo = []
        if not ubicaciones_multi:
            ubic_por_codigo = re.findall(
                r'^\s*([A-Z]{2,4}-[A-Z]{2,4}-P?\d{1,4}(?:-\d+)?)\s*:\s*(https?://\S+)',
                bloque, re.MULTILINE
            )

        # BDTs
        bdts = re.search(
            rf"BDTs?:\s*([^\n]+?)(?=(?:\s|[-•])+{_FRONT_BDTS}|[\r\n]|$)",
            bloque, re.IGNORECASE
        )

       # ACTIVIDADES DESARROLLADAS
        bdts_val = bdts.group(1).strip().rstrip('.').strip() if bdts else ""
        if bdts_val.upper() == "N/A":
            bdts_val = ""
        if not bdts_val and asistentes and re.search(r'\bBDT', bloque, re.IGNORECASE):
            m_bdt = re.search(r'\(\s*(agro\s*fore?s?tal(?:es)?|agr[ií]colas?|construcci[oó]n(?:es)?)\s*\)', asistentes.group(1), re.IGNORECASE)
            if m_bdt:
                bdts_val = "Agroforestales" if m_bdt.group(1).lower().startswith("agr") else "Construcción"                                                                                                                                                 

        partes = []
        if nota_punto_encuentro:
            partes.append(nota_punto_encuentro)
        if direccion_extra:
            partes.append(f"Dirección: {direccion_extra}")
        if bdts_val:
            partes.append(bdts_val)
        _TENENCIA_TAG_RE = re.compile(r'\(\s*(?i:uso\s+com[uú]n|propiedad\s+privada)\s*\)')
        if _sin_actividad_explicita and _TENENCIA_TAG_RE.search(linea_principal):
            _base_reunion = re.sub(r'(\S)\(', r'\1 (', linea_principal)
            _base_para_resumen = f"Reunión con el núcleo agrario {_base_reunion}"
        else:
            _base_para_resumen = linea_principal

        linea_resumen = re.sub(
            r'\(\s*titular(?:es)?:?\s+([^)]+?)\s*\)',
            r'(propietario: \1)',
            _base_para_resumen, flags=re.IGNORECASE
        )
        linea_resumen = re.sub(
            r'\((?!\s*(?:Frente|F|propietario)\b)([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})\)'
            r'(?!\s*\(\s*(?i:uso\s+com[uú]n|propiedad\s+privada)\s*\))',
            r'(propietario: \1)',
            linea_resumen
        )
        personal_m = re.search(r'(?im)^[ \t]*Personal\s+(?:de\s+)?SEDATU[ \t]*:[ \t]*(\S[^\n]*)', bloque)
        personal = personal_m.group(1).strip().rstrip('.').strip() if personal_m else ""   # quién de SEDATU la ejecutó
        resumen_base = resumir_actividad(linea_resumen, actividad_detalle, personal)
        noms_lbl = extraer_nomenclaturas(bloque)
        noms_inl = extraer_nomenclaturas_inline(linea_principal)
        noms_inl += [c for l in bloque.splitlines() if not re.sub(r'[\s,;]|\by\b', '', INLINE_NOM_RE.sub('', l))
                     for c in INLINE_NOM_RE.findall(l)]       # códigos solos en su renglón, sin etiqueta
        noms = noms_lbl + [c for c in dict.fromkeys(noms_inl) if c not in noms_lbl]
        if ubic_por_codigo:
            resumen_final = integrar_parcelas(resumen_base, extraer_parcelas_sueltas(bloque))
        else:
            resumen_final = integrar_nomenclaturas(resumen_base, noms)
            _parcelas = extraer_parcelas_sueltas(bloque) + extraer_parcelas_punto(linea_principal)
            resumen_final = integrar_parcelas(resumen_final, _parcelas)
        partes.append(resumen_final)
        actividades_desarrolladas = " | ".join(partes) if partes else ""

        # ADICIONALES (uso común, etc.): líneas sueltas que se ANEXAN a la actividad
        adicionales = [re.sub(r'\.\s*$', '', l).strip()
                       for l in re.findall(r'(?im)^\s*(Adicionales?\b[^\n]*?)\s*$', bloque)]
        if adicionales:
            extra = " | ".join(adicionales)
            actividades_desarrolladas = (
                f"{actividades_desarrolladas} | {extra}" if actividades_desarrolladas else extra
            )
        minutas = re.findall(r'(?im)^\s*(Minuta\b[^\n]*?)\s*$', bloque)
        ejido_match = re.search(r"(?m)^\s*Ejido:?\s*(.*)", bloque, re.IGNORECASE)
        ejido = ejido_match.group(1).strip() if ejido_match else ""

        # NÚCLEO AGRARIO
        nucleo = re.search(r"Núcleo Agrario:\s*(.*)", bloque, re.IGNORECASE)
        if not nucleo:
            lista_ej = re.search(r'\bejidos\s+de\s+(.+?)(?:\.|\n|$)', linea_principal, re.IGNORECASE)
            if lista_ej:
                partes_nuc = [p.strip(' .') for p in
                              re.split(r'\s*,\s*|\s+y\s+|\s+e\s+', lista_ej.group(1))
                              if p.strip(' .')]
                if len(partes_nuc) >= 2:
                    nucleo_txt = ", ".join(partes_nuc[:-1]) + " y " + partes_nuc[-1]
                    nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()
        if not nucleo:
            lista_ej2 = re.search(r'\b(?i:ejidos)\s+([A-ZÁÉÍÓÚÑ].+?)(?:\.|\n|$)', linea_principal)
            if lista_ej2:
                bruto = lista_ej2.group(1).strip()
                antes_m = re.search(r',\s*antes\s+(.+)$', bruto, re.IGNORECASE)
                sufijo = ""
                if antes_m:
                    sufijo = f" ({antes_m.group(1).strip(' .')})"
                    bruto = bruto[:antes_m.start()].strip()
                if re.search(r',|\by\b', bruto):     
                    nucleo_txt = bruto + sufijo
                    nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()
        if not nucleo:
            # Todos los nombres entrecomillados tras "ejido(s)": "ejidos “A” y “B”"
            _ejq = []
            for _seg in re.finditer(
                r'(?i:\bEjidos?\s+(?:de\s+)?)((?:[\u201c\u2018"\'][^\u201c\u201d\u2018\u2019"\']+[\u201d\u2019"\'](?:\s*(?:,|y|e)\s*)?)+)',
                linea_principal
            ):
                _ejq += re.findall(r'[\u201c\u2018"\']([^\u201c\u201d\u2018\u2019"\']+)[\u201d\u2019"\']', _seg.group(1))
            _vist = set(); _ejq = [e.strip() for e in _ejq
                                   if e.strip() and not (e.strip().lower() in _vist or _vist.add(e.strip().lower()))]
            if len(_ejq) >= 2:
                nucleo_txt = ", ".join(_ejq[:-1]) + " y " + _ejq[-1]
                nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()
        if not nucleo:
            ejido_inline = re.search(
                r'(?i:\b(?:Comisariado\s+Ejidal\s+de|Ejidal\s+de|Ejidos?\s+de|Ejidos?))\s+'
                r'["\u2018\u2019\u201c\u201d\']?'
                r'([A-ZÁÉÍÓÚÑ](?:[a-záéíóúñA-ZÁÉÍÓÚÑ\s]|(?<=\w)[-–](?=\w))+?)'
                r'(?:["\u2018\u2019\u201c\u201d\']|\s*\(|\)|,|\.|\n|$|(?=\s+y\s+parcelas?\b))',
                linea_principal
            )
            if ejido_inline:
                nucleo_txt = ejido_inline.group(1).strip()
                nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()
        if not nucleo:
            _parcela_de_matches = re.findall(
                r'(?i:\bparcelas?)\s+\d+[A-Za-z]?(?:\s*[,y]\s*\d+[A-Za-z]?)*\s+de\s+'
                r'([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)',
                linea_principal
            )
            if _parcela_de_matches:
                _vistos_nuc = set()
                _lugares_nuc = [p.strip() for p in _parcela_de_matches
                                 if p.strip() and not (p.strip().lower() in _vistos_nuc
                                                        or _vistos_nuc.add(p.strip().lower()))]
                nucleo_txt = (", ".join(_lugares_nuc[:-1]) + " y " + _lugares_nuc[-1]
                              if len(_lugares_nuc) > 1 else _lugares_nuc[0])
                nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()
        if not nucleo and ejido_match:
            nucleo_txt = ejido_match.group(1).strip()
            if nucleo_txt:
                nucleo = type('_', (), {'group': lambda self, n: nucleo_txt})()

        # PROPIETARIOS
        particular = re.search(r"Propietarios?:\s*(.*)|Particular:\s*(.*)", bloque, re.IGNORECASE)
        if not particular:
            propietario_inline = re.search(
                r'(?i:\b(se[ñn]or[a]?|propietari[ao]))\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+)',
                linea_principal
            )
            if propietario_inline:
                prop_txt = f"{_trato_propietario(propietario_inline.group(1))} {propietario_inline.group(2).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        if not particular:
            titulo_inline = re.search(
                r'\b([Ss][Rr][Aa]|[Ss][Rr])\.?,?\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)',
                linea_principal
            )
            if titulo_inline:
                tit_norm = "Sra." if titulo_inline.group(1).lower().startswith("sra") else "Sr."
                prop_txt = f"{tit_norm} {titulo_inline.group(2).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        if not particular:
            ciudadano_inline = re.search(
                r'\bC\.\s*([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+)',
                linea_principal
            )
            if ciudadano_inline:
                prop_txt = f"C. {ciudadano_inline.group(1).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        if not particular:
            titular_inline = re.search(
                r'\(\s*titular(?:es)?:?\s+'
                r'([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)\s*\)',
                linea_principal, re.IGNORECASE
            )
            if titular_inline:
                nombre = titular_inline.group(1).strip()
                if re.match(r'(?i)(sr|sra|c)\.?\b', nombre):
                    prop_txt = nombre
                else:
                    prop_txt = f"C. {nombre}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        if not particular:
            # "titular Marco Antonio" sin paréntesis
            titular_libre = re.search(
                r'(?i:\btitular(?:es)?)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)',
                linea_principal
            )
            if titular_libre:
                prop_txt = f"C. {titular_libre.group(1).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        if not particular:
            paren_inline = re.search(
                r'\((?!\s*(?:Frente|F)\b)([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})\)'
                r'(?!\s*\(\s*(?i:uso\s+com[uú]n|propiedad\s+privada)\s*\))',
                linea_principal
            )
            if paren_inline:
                prop_txt = paren_inline.group(1).strip()
                nucleo_txt = nucleo.group(1).strip() if nucleo else ""
                ya_es_lugar = {nucleo_txt.lower(), ejido.lower(), municipio.lower()}
                if prop_txt.lower() not in ya_es_lugar:        # guardia: no es lugar ya detectado
                    particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        # Fallback: "familia"
        if not particular:
            familia_inline = re.search(
                r'(?i:\bfamilia)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)',
                linea_principal
            )
            if familia_inline:
                prop_txt = f"Familia {familia_inline.group(1).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()
        # Fallback: nombre de pila solo tras señor(a)/propietari(o/a)
        if not particular:
            _LUGARES_PROP = {"ejido","ejidos","nucleo","núcleo","municipio","palacio",
                             "presidencia","comisariado","poligono","polígono","parcela"}
            persona_simple = re.search(
                r'(?i:\b(se[ñn]or[a]?|propietari[ao]))\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\b',
                linea_principal
            )
            if persona_simple and persona_simple.group(2).lower() not in _LUGARES_PROP:
                prop_txt = f"{_trato_propietario(persona_simple.group(1))} {persona_simple.group(2).strip()}"
                particular = type('_', (), {'group': lambda self, n: prop_txt if n in (1, 2) else ''})()

        tipo_propiedad = detectar_tipo_propiedad(
            bloque, linea_principal, nucleo.group(1).strip() if nucleo else "")

        # Arma el diccionario de una fila con todas las columnas de la hoja.
        def armar_fila(ubic_url, est_geo, mun_g, acts_desarrolladas):
            return {
                "FECHA DE SOLICITUD": fecha_solicitud,
                "SOLICITANTE": "SEDATU",
                "MEDIO DE SOLICITUD": "WhatsApp",
                "TIPO DE SOLICITUD": homologar_tipo(actividad_txt),
                "PROYECTO FERROVIARIO": proyecto_final,
                "UBICACIÓN": ubic_url,
                "TIPO DE PROPIEDAD": tipo_propiedad,
                "FRENTE": (
                    re.sub(r'\b(\d+)\b', r'F\1', frente.group(1).strip())
                    if frente and frente.group(1).strip().upper() != "N/A" else ""
                ),
                "POLÍGONO": (
                    poligono.group(1).strip()
                    if poligono and poligono.group(1).strip().upper() != "N/A" else ""
                ),
                "ESTADO": (est_geo.upper() if est_geo else estado_txt_inline),
                "MUNICIPIO": municipio if municipio else (municipio_multi_txt if municipio_multi_txt else (mun_g if mun_g else municipio_txt_inline)),
                "EJIDO": capitalizar_nombre_propio(ejido),
                "NÚCLEO AGRARIO": capitalizar_nombre_propio(nucleo.group(1).strip() if nucleo else ""),
                "PROPIETARIOS PROPIEDAD PRIVADA": (
                    (particular.group(1) or particular.group(2) or "").strip()
                    if particular else ""
                ),
                "FECHA Y HORA": f"{fecha} {hora_txt}".strip(),   # sin espacio al final si no hay hora
                "DEPENDENCIAS PARTICIPANTES": (
                    asistentes.group(1).strip().rstrip('.') if asistentes else ""
                ),
                "ACTIVIDADES DESARROLLADAS": acts_desarrolladas,
            }

        if ubic_por_codigo:
            hora_por_cod = {mm.group(1): f"{int(mm.group(2)):02d}:{mm.group(3)}"
                            for mm in _NOM_HORA_RE.finditer(bloque)}
            for cod, url_punto in ubic_por_codigo:
                cod = cod.strip()
                url_punto = url_punto.strip()
                est_p, mun_p = "", ""
                if _es_url_maps(url_punto):
                    lat, lng = extraer_coordenadas(url_punto)
                    if lat is not None:
                        est_p, mun_p = obtener_estado_municipio(lat, lng)
                acts_cod = integrar_nomenclaturas(resumen_final, [cod])
                if bdts_val:
                    acts_cod = f"{bdts_val} | {acts_cod}"
                fila = armar_fila(url_punto, est_p, mun_p, acts_cod)
                hp = hora_por_cod.get(cod)
                if hp:
                    fila["FECHA Y HORA"] = f"{fecha} {hp}"
                filas.append(fila)
        elif ubicaciones_multi:
            for nombre_punto, url_punto in ubicaciones_multi:
                nombre_punto = nombre_punto.strip()
                url_punto = url_punto.strip()
                est_p, mun_p = "", ""
                if _es_url_maps(url_punto):
                    lat, lng = extraer_coordenadas(url_punto)
                    if lat is not None:
                        est_p, mun_p = obtener_estado_municipio(lat, lng)
                acts_punto = (
                    f"{nombre_punto} — {actividades_desarrolladas}"
                    if actividades_desarrolladas else nombre_punto
                )
                filas.append(armar_fila(url_punto, est_p, mun_p, acts_punto))
        else:
            filas.append(armar_fila(url, estado_geo, municipio_geo, actividades_desarrolladas))

         # Filas de MINUTA (heredan ubicación/asistentes/frente del bloque, hora propia)
        for minuta_txt in minutas:
            minuta_txt = re.sub(r'\.\s*$', '', minuta_txt.strip())
            mh = re.search(r'\((\d{1,2}):(\d{2})\s*(?:hrs?)?\)', minuta_txt)
            hora_min = f"{int(mh.group(1)):02d}:{mh.group(2)}" if mh else hora_txt
            texto_min = re.sub(r'\s*\(\d{1,2}:\d{2}\s*(?:hrs?)?\)', '', minuta_txt).strip()
            fila_min = armar_fila(url, estado_geo, municipio_geo, texto_min)
            fila_min["TIPO DE SOLICITUD"] = homologar_tipo(texto_min)
            fila_min["FECHA Y HORA"] = f"{fecha} {hora_min}".strip()
            filas.append(fila_min)

    return filas

_INVISIBLES_RE = re.compile(r'[\u200b-\u200d\u2060\ufeff]')

# Separa varias agendas pegadas juntas: por líneas de guiones o por un nuevo encabezado
# ("PROYECTO:" / "Agenda ...") que aparece después de alguna actividad numerada.
def procesar_agendas(texto):
    texto = _INVISIBLES_RE.sub('', texto)
    partes, actual = [], []
    for linea in texto.split('\n'):
        es_sep = re.match(r'^[ \t]*-{5,}[ \t]*$', linea)
        es_enc = re.match(r'^[ \t]*\W*(?:PROYECTO\s*:|Agenda\b)', linea, re.IGNORECASE)
        ya_hay_act = any(re.match(r'^\s*\d+[\.\-]', l) for l in actual)
        if es_sep or (es_enc and ya_hay_act):
            partes.append('\n'.join(actual))
            actual = [] if es_sep else [linea]
        else:
            actual.append(linea)
    partes.append('\n'.join(actual))
    textos = [p.strip() for p in partes if p.strip()]
    # 1) Ensayo sin Haiku: junta lo que hay que resumir (las ligas de Maps quedan en caché).
    _RES_LOCAL.pendientes = []
    try:
        for t in textos:
            procesar_agenda(t)
        pendientes = list(dict.fromkeys(_RES_LOCAL.pendientes))
    finally:
        _RES_LOCAL.pendientes = None
    # 2) Haiku redacta todos en paralelo, 6 a la vez.
    with ThreadPoolExecutor(max_workers=6) as pool:
        listos = dict(zip(pendientes, pool.map(lambda c: resumir_actividad(*c), pendientes)))
    # 3) Pasada real: usa los resúmenes ya listos.
    _RES_LOCAL.listos = listos
    try:
        filas = []
        for t in textos:
            filas.extend(procesar_agenda(t))
    finally:
        _RES_LOCAL.listos = None
    return filas

import threading
_CLIENTE = None
_CACHE = {"t": 0.0, "filas": None}
_CACHE_SEG = 60                      # segundos que se reutiliza la lectura del Sheet
_cache_lock = threading.Lock()

# Conecta a Google Sheets con la cuenta de servicio; la conexión se reutiliza.
def _cliente_sheets():
    global _CLIENTE
    if _CLIENTE is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("credenciales.json", scopes=SCOPES)
        _CLIENTE = gspread.Client(auth=creds)
    return _CLIENTE

# Lee la hoja principal como lista de diccionarios; usa la copia en caché si tiene menos de 60 s.
def _filas_hoja_principal():
    with _cache_lock:
        if _CACHE["filas"] is None or time.time() - _CACHE["t"] > _CACHE_SEG:
            valores = _cliente_sheets().open_by_key(SPREADSHEET_ID).get_worksheet(0).get_all_values()
            headers = [h.replace("\n", " ").strip() for h in valores[0]] if valores else []
            _CACHE["filas"] = [dict(zip(headers, row)) for row in valores[1:]]
            _CACHE["t"] = time.time()
        return [dict(f) for f in _CACHE["filas"]]   # copia: nadie altera la caché

_subir_lock = threading.Lock()       # una subida a la vez: el consecutivo no se repite


# Último número de la columna A (el mayor, aunque la hoja esté ordenada distinto).
def _ultimo_consecutivo(columna):
    return max((int(v) for v in columna[1:] if str(v).strip().isdigit()), default=0)


# Clave para reconocer una actividad repetida: fecha y hora, proyecto, tipo, ubicación y núcleo (sin el resumen de Haiku).
def _clave_dup(f):
    fh = re.sub(r'\s+', ' ', str(f.get("FECHA Y HORA", ""))).strip().lower().replace(" hrs", "")
    return (fh, _norm(f.get("PROYECTO FERROVIARIO", "")), _norm(f.get("TIPO DE SOLICITUD", "")),
            str(f.get("UBICACIÓN", "")).strip(), _norm(f.get("NÚCLEO AGRARIO", "")))


# Revisa las filas antes de subirlas: avisa de las que ya están en el Sheet y de datos faltantes (no quita nada).
def validar_filas(filas):
    try:
        existentes = {_clave_dup(f) for f in _filas_hoja_principal()}
    except Exception as e:
        print(f"[validar] No pude leer el Sheet: {e}", flush=True)
        existentes = set()
    hoy = datetime.now(MX).date()
    repetidas, avisos = [], []
    for f in filas:
        fh = str(f.get("FECHA Y HORA", "")).strip()
        cual = f"{fh or 'sin fecha'} · {str(f.get('TIPO DE SOLICITUD', ''))[:60]}"
        k = _clave_dup(f)
        if k in existentes:
            repetidas.append(cual)
        existentes.add(k)                                 # también detecta repetidas dentro de la misma agenda
        faltas = []
        m_fecha = re.match(r'(\d{1,2}/\d{1,2}/\d{4})', fh)
        if not m_fecha:
            faltas.append("sin fecha")
        else:
            if not re.search(r'\d{1,2}:\d{2}|por confirmar', fh, re.IGNORECASE):
                faltas.append("sin hora")
            try:
                if abs((datetime.strptime(m_fecha.group(1), "%d/%m/%Y").date() - hoy).days) > 30:
                    faltas.append("fecha a más de 30 días de hoy")
            except ValueError:
                faltas.append("fecha inválida")
        if not f.get("ESTADO") and not f.get("MUNICIPIO"):
            faltas.append("sin estado ni municipio")
        if faltas:
            avisos.append(f"{cual} → {', '.join(faltas)}")
    return repetidas, avisos


# Agrega las filas a la hoja principal (con consecutivo) y las replica en la del jefe.
# «revision» (opcional) recibe las actividades que ya estaban en el Sheet y los avisos.
def subir_a_sheets(filas, revision=None):
    with _subir_lock:
        return _subir_a_sheets(filas, {} if revision is None else revision)


def _subir_a_sheets(filas, revision):
    revision["repetidas"], revision["avisos"] = validar_filas(filas)
    client = _cliente_sheets()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.get_worksheet(0)
    headers = worksheet.row_values(1)
    inicio = _ultimo_consecutivo(worksheet.col_values(1)) + 1
    datos = []
    for i, fila in enumerate(filas):
        nueva_fila = [inicio + i]
        for header in headers[1:]:
            nueva_fila.append(fila.get(header, ""))
        datos.append(nueva_fila)

    worksheet.append_rows(datos, value_input_option="USER_ENTERED")
    _CACHE["filas"] = None               # lo recién subido se ve en la siguiente consulta

     # --- Replicar actividades de TSNL/TIG/TQI al sheet del jefe ---
    filas_jefe = [f for f in filas if f.get("PROYECTO FERROVIARIO", "") in PROYECTOS_JEFE]
    if filas_jefe:
        try:
            _subir_a_sheet_jefe(client, filas_jefe)
        except Exception as e:
            print(f"[sheet jefe] No se pudo replicar: {e}")
            avisar_admin("jefe", f"No se replicaron {len(filas_jefe)} actividades al Sheet: {e}")

    return len(filas)


# Agrega las filas a la hoja del jefe, con director del tramo.
def _subir_a_sheet_jefe(client, filas_jefe):
    spreadsheet_jefe = client.open_by_key(SPREADSHEET_ID_JEFE)
    ws_jefe = spreadsheet_jefe.get_worksheet(0)
    headers_jefe = ws_jefe.row_values(1)
    inicio_jefe = _ultimo_consecutivo(ws_jefe.col_values(1)) + 1
    datos_jefe = []
    for i, fila in enumerate(filas_jefe):
        nueva_fila = [inicio_jefe + i]
        for header in headers_jefe[1:]:
            if header.strip().upper() == "DIRECTOR DEL TRAMO":
                valor = DIRECTOR_TRAMO.get(fila.get("PROYECTO FERROVIARIO", ""), "")
            else:
                valor = fila.get(header, "")
            nueva_fila.append(valor)
        datos_jefe.append(nueva_fila)

    ws_jefe.append_rows(datos_jefe, value_input_option="USER_ENTERED")

# Página principal: pegar agenda.
@app.route("/")
def index():
    return render_template("index.html")

# API: procesa el texto enviado desde la web y lo sube a Sheets.
@app.route("/procesar", methods=["POST"])
def procesar():
    try:
        texto = request.json.get("texto", "")
        if not texto.strip():
            return jsonify({"error": "El texto está vacío"}), 400
        filas = procesar_agendas(texto)
        if not filas:
            return jsonify({"error": "No se encontraron actividades en el texto"}), 400
        rev = {}
        total = subir_a_sheets(filas, rev)
        return jsonify({"success": True, "filas": filas, "total": total,
                        "repetidas": rev.get("repetidas", []), "avisos": rev.get("avisos", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Sirve el service worker de la PWA.
@app.route("/sw.js")
def service_worker():
    # Servido desde raíz para que el scope del SW cubra toda la app
    return app.send_static_file("sw.js")


# Recibe texto compartido desde el celular (PWA) y lo sube a Sheets.
@app.route("/share", methods=["GET", "POST"])
def share():
    if request.method == "POST":
        texto = (request.form.get("text") or request.form.get("title") or "").strip()
    else:
        texto = (request.args.get("text") or "").strip()

    if not texto:
        return "No llegó texto para procesar", 400

    try:
        filas = procesar_agendas(texto)
        if not filas:
            return "<h2>No se encontraron actividades en el texto compartido</h2>", 400
        rev = {}
        total = subir_a_sheets(filas, rev)
        resumen_html = "".join(
            f"<li>{f['FECHA Y HORA']} — {f['TIPO DE SOLICITUD'][:80]}</li>" for f in filas
        )
        if rev["repetidas"]:
            resumen_html += f"<p>{len(rev['repetidas'])} ya estaban en el Sheet (se subieron de todos modos).</p>"
        if rev["avisos"]:
            resumen_html += "<p>Revisa:</p><ul>" + "".join(f"<li>{a}</li>" for a in rev["avisos"]) + "</ul>"
        return f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body{{font-family:sans-serif;padding:24px;background:#691B4F;color:white}}
        .card{{background:white;color:#333;border-radius:16px;padding:20px}}</style></head>
        <body><div class="card">
        <h2>{total} actividad(es) subida(s) a Sheets</h2>
        <ul>{resumen_html}</ul>
        </div></body></html>
        """
    except Exception as e:
        return f"<h2>Error: {e}</h2>", 500


# TARA (TELEGRAM AGENDA READER APP) — Webhook para recibir mensajes de Telegram y subirlos a Sheets

TELEGRAM_CHATS_OK = set(
    c.strip() for c in os.environ.get("TELEGRAM_CHATS_OK", "").split(",") if c.strip()
)
_telegram_vistos = set()   # update_id ya procesados (anti-duplicados en frío de Render)
_agendas_recientes = {}
_encabezados = {}   # chat_id -> (hora, encabezado de su última agenda: proyecto y fecha)

import threading

_buffer_lock = threading.Lock()
_mensajes_buffer = {}  # chat_id -> {"texto": str, "timer": Timer}
BUFFER_DELAY = 10  # segundos de espera para juntar fragmentos partidos por Telegram

# Envía un mensaje de Telegram (silencioso si falla).
def telegram_enviar(chat_id, texto):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
            timeout=10,
        )
    except requests.RequestException:
        pass

_ultimo_aviso = {}   # tipo de alerta -> hora del último aviso


# Avisa al administrador por Telegram que algo falló (máximo un aviso por tipo cada 30 min).
def avisar_admin(tipo, texto):
    chat = os.environ.get("TELEGRAM_ADMIN_CHAT", "").strip()
    ahora = time.time()
    if not chat or ahora - _ultimo_aviso.get(tipo, 0) < 1800:
        return
    _ultimo_aviso[tipo] = ahora
    threading.Thread(target=telegram_enviar, args=(chat, f"TRAXA: {texto}"), daemon=True).start()   # no frena la agenda


# Webhook de Telegram: valida, evita duplicados y junta mensajes partidos.
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    # 1) Seguridad: el secreto que Telegram manda en cada llamada.
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""):
        return "", 403

    update = request.get_json(silent=True) or {}

    # 2) Anti-duplicados: si Telegram reintenta el mismo update, lo ignoramos.
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in _telegram_vistos:
            return "", 200
        _telegram_vistos.add(update_id)
        if len(_telegram_vistos) > 1000:        # no crece sin límite
            _telegram_vistos.clear()

    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    texto = msg.get("text", "") or ""
    if not chat_id:
        return "", 200

    # 3) Control de acceso. Sin lista configurada = modo setup: te dice tu id.
    if not TELEGRAM_CHATS_OK:
        telegram_enviar(chat_id, f"[setup] chat_id = {chat_id} — ponlo en TELEGRAM_CHATS_OK")
        return "", 200
    if chat_id not in TELEGRAM_CHATS_OK:
        return "", 200   # chat no autorizado: se ignora en silencio

    # 4) Acumula en buffer por si Telegram partió el mensaje en fragmentos
    with _buffer_lock:
        entry = _mensajes_buffer.get(chat_id)
        if entry:
            entry["timer"].cancel()
            # Telegram parte mensajes SOLO cuando pasan de ~4096 chars. Si el buffer
            # previo es corto, este fragmento es un mensaje NUEVO (ej. el adicional del
            # Frente 4) -> sepáralo con renglón en blanco. Si ya viene largo, es una
            # continuación partida por Telegram, que se come el salto de renglón del corte:
            # se repone según cómo empieza el fragmento.
            if len(entry["texto"]) < 3000 or re.match(r"\s*\d{1,2}[.)]\s", texto):
                sep = "\n\n"   # mensaje nuevo, o el corte cayó justo antes de una actividad numerada
            elif re.match(r"\s*[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ ]{1,30}:", texto):
                sep = "\n"      # el corte cayó antes de un campo («Asistentes:», «Ubicación:»…)
            else:
                sep = " "       # el corte cayó a media línea
            entry["texto"] += sep + texto
        else:
            entry = {"texto": texto}
            _mensajes_buffer[chat_id] = entry
        entry["timer"] = threading.Timer(BUFFER_DELAY, _procesar_buffer, args=(chat_id,))
        entry["timer"].start()

    return "", 200


# Procesa la agenda acumulada de un chat y responde con el resultado.
def _procesar_buffer(chat_id):
    with _buffer_lock:
        entry = _mensajes_buffer.pop(chat_id, None)
    if not entry:
        return
    texto = entry["texto"]

    # Preguntas y comandos. Tara responde y no lo trata como agenda.
    if tara_consultas.atender(chat_id, texto):
        return

    texto = texto.replace('\u200b', '')   
    # Continuación que llegó tarde y sin encabezado: se le pone el de la agenda anterior del chat.
    if re.match(r"\s*\d{1,2}[.)]\s", texto) and not re.search(r"(?i)proyecto|agenda|fecha\s*:", texto[:300]):
        previo = _encabezados.get(chat_id)
        if previo and (datetime.now() - previo[0]).total_seconds() < 900:
            texto = previo[1] + "\n\n" + texto
    inicio = re.search(r"(?m)^\s*\d{1,2}[.)]\s", texto)
    if inicio and inicio.start() > 0:
        _encabezados[chat_id] = (datetime.now(), texto[:inicio.start()].strip())

    if not (re.search(r'(?i)agenda', texto)
            or re.search(r'(?i)\b(?:fecha|proyecto)\s*:', texto)
            or _CLAVES_RE.search(texto[:200])
            or _resolver_abrev_tramo(texto[:200])):
        telegram_enviar(chat_id, "Eso no parece agenda. Reenvíame el mensaje del grupo tal cual.")
        return

    import hashlib
    firma = hashlib.sha256(texto.strip().encode("utf-8")).hexdigest()
    ahora = datetime.now()
    for h in [h for h, t in _agendas_recientes.items() if (ahora - t).total_seconds() > 600]:
        del _agendas_recientes[h]
    if firma in _agendas_recientes:
        telegram_enviar(chat_id, "Esa misma agenda ya la subí hace un momento; no la dupliqué.")
        return
    _agendas_recientes[firma] = ahora

    try:
        filas = procesar_agendas(texto)
        if not filas:
            telegram_enviar(chat_id, "No encontré actividades en ese texto.")
            return
        rev = {}
        total = subir_a_sheets(filas, rev)
        # Actividades por proyecto, en el orden en que llegaron
        por_proyecto = {}
        for f in filas:
            p = f.get("PROYECTO FERROVIARIO", "")
            por_proyecto[p] = por_proyecto.get(p, 0) + 1

        # Filas con link de maps que NO geocodificó (link roto/404 o fuera de capa)
        sin_geo = [
            f for f in filas
            if _es_url_maps(f.get("UBICACIÓN", ""))
            and not f.get("ESTADO", "").strip()
        ]
        if len(por_proyecto) == 1:
            msg = f"Subí {total} actividad(es) a Sheets ({next(iter(por_proyecto))})."
        else:
            detalle_proy = "\n".join(f"• {p}: {n}" for p, n in por_proyecto.items())
            msg = (f"Subí {total} actividades de {len(por_proyecto)} proyectos a Sheets:\n"
                   f"{detalle_proy}")
        if sin_geo:
            claves = {v: k for k, v in mapa_proyectos.items()}
            varios = len(por_proyecto) > 1
            detalle = "\n".join(
                f"• {(p[1] if len(p := f.get('FECHA Y HORA','').split(maxsplit=1)) > 1 else '--')}  "
                + (f"[{claves.get(f.get('PROYECTO FERROVIARIO',''), '?')}] " if varios else "")
                + f"{f.get('TIPO DE SOLICITUD','')[:40]}"
                for f in sin_geo
            )
            msg += (f"\n\n{len(sin_geo)} sin ubicación (link roto o inválido). "
                    f"Llena ESTADO/MUNICIPIO a mano:\n{detalle}")
        if rev["repetidas"]:
            msg += (f"\n\n{len(rev['repetidas'])} ya estaban en el Sheet (las subí de todos modos):\n"
                    + "\n".join(f"• {x}" for x in rev["repetidas"]))
        otros = [re.sub(r' → $', '', re.sub(r',? ?sin estado ni municipio', '', a)) for a in rev["avisos"]]   # la ubicación ya va arriba
        otros = [a for a in otros if " → " in a]
        if otros:
            msg += "\n\nRevisa:\n" + "\n".join(f"• {a}" for a in otros)
        telegram_enviar(chat_id, msg)
    except Exception as e:
        telegram_enviar(chat_id, f"Error: {e}")

# API AGENDA

# Manda el error completo al log y al navegador solo un mensaje genérico.
def _error_api():
    import traceback
    print(traceback.format_exc(), flush=True)
    return jsonify({"error": "No se pudo leer el Sheet; revisa el log del servidor."}), 500

# API del dashboard: carga por proyecto, estado, dependencia y próximas actividades.
@app.route("/api/agenda")
def api_agenda():
    from collections import defaultdict, Counter
    import traceback
    try:
        filas = _filas_hoja_principal()
        if not filas:
            return jsonify({"carga": {}, "edo": {}, "dep": {}, "serie": {}, "agenda": []})

        inv = {v: k for k, v in mapa_proyectos.items()}
        carga, edo, dep, serie = Counter(), Counter(), Counter(), defaultdict(int)
        emp = Counter()

        

        def fecha_norm(fh):
            """Normaliza a dd/mm/yyyy. Devuelve None si no es fecha válida."""
            s = str(fh).split()[0].replace("-", "/") if str(fh).split() else ""
            partes = s.split("/")
            if len(partes) != 3:
                return None
            d, m, a = partes
            if len(a) == 2:
                a = "20" + a
            try:
                if int(m) > 12 and int(d) <= 12:
                    d, m = m, d
                dt = datetime.strptime(f"{int(d):02d}/{int(m):02d}/{a}", "%d/%m/%Y")
                if dt.year < 2025 or dt.year > 2030:
                    return None
                return dt.strftime("%d/%m/%Y")
            except (ValueError, TypeError):
                return None

        def fecha_dt(fh):
            """Igual que fecha_norm pero devuelve un objeto date (o None)."""
            fn = fecha_norm(fh)
            if not fn:
                return None
            try:
                return datetime.strptime(fn, "%d/%m/%Y").date()
            except ValueError:
                return None

        # Ventana de 5 días desde la fecha MÁS RECIENTE del Sheet
        # Fechas a más de 7 días en el futuro se toman como error de captura y no anclan la ventana.
        limite = datetime.now(MX).date() + timedelta(days=7)
        fechas_validas = [d for d in (fecha_dt(f.get("FECHA Y HORA", "")) for f in filas) if d and d <= limite]
        if fechas_validas:
            fecha_tope = max(fechas_validas)
            fecha_piso = fecha_tope - timedelta(days=4)
            # Filtro opcional del dashboard: /api/agenda?desde=AAAA-MM-DD&hasta=AAAA-MM-DD
            try:
                if request.args.get("desde"):
                    fecha_piso = datetime.strptime(request.args["desde"], "%Y-%m-%d").date()
                if request.args.get("hasta"):
                    fecha_tope = datetime.strptime(request.args["hasta"], "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "Fecha inválida en el filtro"}), 400
        else:
            fecha_tope = fecha_piso = None

        for f in filas:
            d = fecha_dt(f.get("FECHA Y HORA", ""))
            if fecha_piso is not None and (d is None or d < fecha_piso or d > fecha_tope):
                continue

            proy = inv.get(f.get("PROYECTO FERROVIARIO", ""), "OTRO")
            carga[proy] += 1
            if f.get("ESTADO"):    edo[f["ESTADO"]] += 1

            celda = _sin_acentos(str(f.get("DEPENDENCIAS PARTICIPANTES", "")).upper())
            for canon, patrones in _DEP_PATRONES.items():
                if any(p.search(celda) for p in patrones):
                    dep[canon] += 1
            for canon, patrones in _EMP_PATRONES.items():
                if any(p.search(celda) for p in patrones):
                    emp[canon] += 1

            fn = fecha_norm(f.get("FECHA Y HORA", ""))
            if fn:
                serie[fn] += 1

        # Extrae HH:MM de 'FECHA Y HORA'.
        def hora_de(fh):
            partes = str(fh).split(maxsplit=1)
            if len(partes) < 2:
                return ""
            m = re.search(r'\d{1,2}:\d{2}', partes[1])
            return m.group(0) if m else ""

        # Lista las 10 actividades más recientes dentro de la ventana de fechas.
        def construir_agenda(filas, piso, tope, fdt, hde, inv):
            items = []
            for f in filas:
                d = fdt(f.get("FECHA Y HORA", ""))
                # solo las de la ventana de 3 días
                if piso is not None and (d is None or d < piso or d > tope):
                    continue
                hora = hde(f.get("FECHA Y HORA", ""))
                # clave de orden: fecha + hora (las que no tienen hora van al final del día)
                clave = (d, hora if hora else "00:00")
                items.append((clave, {
                    "hora":  hora if hora else "--:--",
                    "code":  inv.get(f.get("PROYECTO FERROVIARIO", ""), "—"),
                    "mun":   f.get("MUNICIPIO", ""),
                    "act":   f.get("TIPO DE SOLICITUD", ""),
                }))
            # ordena por (fecha, hora) descendente = más reciente primero
            items.sort(key=lambda x: x[0], reverse=True)
            return [it[1] for it in items[:10]]

        dep_colapsado = Counter()
        for canon, n in dep.items():
            dep_colapsado["OTRAS" if canon not in DEP_NUCLEO else canon] += n

        return jsonify({
            "carga": dict(carga),
            "edo": dict(edo),
            "dep": dict(dep_colapsado),
            "empresas": dict(emp),
            "serie": dict(serie),
            "agenda": construir_agenda(filas, fecha_piso, fecha_tope, fecha_dt, hora_de, inv),
        })
    except Exception as e:
        return _error_api()

# API CONTEO

# API del conteo: actividades por categoría desde abril 2026.
@app.route("/api/conteo")
def api_conteo():
    from collections import Counter
    import traceback
    try:
        filas = _filas_hoja_principal()
        if not filas:
            return jsonify({"conteo": {}, "cat3_desglose": {}, "otras": 0,
                            "sin_clasificar": [], "total": 0})

        PISO = datetime(2026, 1, 1).date()
        HOY = datetime.now(MX).date()
        # Filtro opcional de conteo
        try:
            if request.args.get("desde"):
                PISO = datetime.strptime(request.args["desde"], "%Y-%m-%d").date()
            if request.args.get("hasta"):
                HOY = datetime.strptime(request.args["hasta"], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Fecha inválida en el filtro"}), 400

        # Obtiene la fecha de una fila (acepta dd/mm/aaaa o mm/dd/aaaa).
        def fecha_actividad(f):
            s = str(f.get("FECHA Y HORA", "")).split()
            if not s:
                return None
            partes = s[0].replace("-", "/").split("/")
            if len(partes) != 3:
                return None
            d, m, a = partes
            if len(a) == 2:
                a = "20" + a
            try:
                if int(m) > 12 and int(d) <= 12:
                    d, m = m, d
                return datetime(int(a), int(m), int(d)).date()
            except (ValueError, TypeError):
                return None

        conteo = Counter()
        for n, _ in CATEGORIAS_CONTEO:
            conteo[n] = 0
        conteo[CAT3_NOMBRE] = 0
        cat3_desglose = Counter()
        for n, _ in CAT3_SUB:
            cat3_desglose[n] = 0
        cat3_desglose[CAT3_GENERAL[0]] = 0

        otras = 0
        sin_clasificar = []
        total = 0
        for f in filas:
            d = fecha_actividad(f)
            if d is None or d < PISO or d > HOY:
                continue
            total += 1
            hits = clasificar_actividad(
                f.get("TIPO DE SOLICITUD", ""),
                f.get("ACTIVIDADES DESARROLLADAS", ""),
            )
            if not hits:
                otras += 1
                sin_clasificar.append(f.get("TIPO DE SOLICITUD", ""))
                continue
            for cat in {c for c, _ in hits}:      # la 3 con dos subcategorías cuenta una vez
                conteo[cat] += 1
            for cat, sub in hits:
                if sub:
                    cat3_desglose[sub] += 1

        return jsonify({
            "conteo": dict(conteo),
            "cat3_desglose": dict(cat3_desglose),
            "otras": otras,
            "sin_clasificar": sin_clasificar,
            "total": total,
            "ventana": {"desde": PISO.strftime("%d/%m/%Y"),
                        "hasta": HOY.strftime("%d/%m/%Y")},
        })
    except Exception as e:
        return _error_api()

# API TIPO DE PROPIEDAD

# API del conteo por tipo de propiedad.
@app.route("/api/tipo_propiedad")
def api_tipo_propiedad():
    from collections import Counter
    import traceback
    try:
        filas = _filas_hoja_principal()
        if not filas:
            return jsonify({"conteo": {}, "sin_dato": 0, "total": 0})

        conteo = Counter()
        display = {}
        sin_dato = 0
        total = 0
        for f in filas:
            if not any((v or "").strip() for v in f.values()):
                continue   # renglón totalmente vacío al final del Sheet
            total += 1
            valor = re.sub(r'\s+', ' ', (f.get("TIPO DE PROPIEDAD", "") or "")).strip()
            if not valor or valor.upper() == "N/A":
                sin_dato += 1
                continue
            clave = valor.lower()
            conteo[clave] += 1
            display.setdefault(clave, valor)

        conteo_final = {display[k]: v for k, v in conteo.items()}

        return jsonify({
            "conteo": conteo_final,
            "sin_dato": sin_dato,
            "total": total,
        })
    except Exception as e:
        return _error_api()

# RUTA A DASHBOARD

# Página del dashboard.
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

# RUTA A CONTEO

# Página del conteo por categoría.
@app.route("/conteo")
def conteo_dashboard():
    return render_template("conteo.html")

# RUTA A TIPO DE PROPIEDAD

# Página del conteo por tipo de propiedad.
@app.route("/propiedad")
def propiedad_dashboard():
    return render_template("propiedad.html")

# REPORTE EJECUTIVO (las rutas viven en reporte.py)
import sys
import reporte
reporte.registrar(app, sys.modules[__name__])

# MAPA POR MUNICIPIO (mapa.py) Y TARA QUE RESPONDE PREGUNTAS (tara_consultas.py)
import mapa
mapa.registrar(app, sys.modules[__name__])
import tara_consultas
tara_consultas.registrar(app, sys.modules[__name__])

if __name__ == "__main__":
    app.run(debug=False)