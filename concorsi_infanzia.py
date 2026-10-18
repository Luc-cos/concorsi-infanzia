#!/usr/bin/env python3
"""
Ricerca concorsi per diplomati nell'ambito infanzia/materna/nido
a Napoli e provincia: mostra solo i bandi NON SCADUTI.

Uso:
    python3 concorsi_infanzia.py

Cosa fa:
  - scarica le pagine delle fonti elencate in FONTI
  - tiene solo i link con parole chiave pertinenti (esclude graduatorie, esiti, ecc.)
  - cerca la scadenza nel testo del link e, se serve, aprendo la pagina del bando
  - scarta i bandi con scadenza passata
  - segnala i bandi NUOVI rispetto all'ultima esecuzione
  - legge anche inPA (API del portale portale.inpa.gov.it)
  - crea la pagina concorsi_trovati.html e la apre nel browser

Su GitHub (GitHub Actions) gira in automatico: vedi le istruzioni di pubblicazione.
Controllo dei campi dell'API inPA:  python3 concorsi_infanzia.py --test-inpa

Dipendenze:  pip3 install requests beautifulsoup4
"""

import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Mancano le librerie. Esegui:  pip3 install requests beautifulsoup4")

# ---------------------------------------------------------------- CONFIGURAZIONE

# Un link viene tenuto se il testo contiene ALMENO una di queste parole
PAROLE_CHIAVE = [
    "infanzia", "materna", "nido", "educatore", "educatrice",
    "diploma magistrale", "scienze della formazione primaria",
    "assistente all'infanzia", "operatore dei servizi educativi",
    "educatori", "educatrici", "nidi", "asilo", "asili",
]

# Parole usate per interrogare il portale inPA (la sua ricerca trova anche i plurali)
RICERCHE_INPA = ["infanzia", "nido", "educatore", "educatrice", "materna", "asilo"]

# Link scartati se contengono una di queste parole (non sono bandi aperti)
ESCLUDI = [
    "graduatoria", "graduatorie", "esito", "esiti", "risultati", "calendario",
    "convocazione", "convocazioni", "diario", "ammessi", "commissione",
    "sorteggio", "scaduto", "scaduti", "chiuso", "chiusi",
    "nomine", "nomina", "rettifica", "rettifiche",   # nomine e rettifiche non sono concorsi
    "mobilità", "2021",                                     # mobilità: riservata a chi è già dipendente PA
]

# Per le fonti NAZIONALI (filtra_luogo=True) il testo deve citare anche
# Napoli o un comune della provincia. Aggiungi/togli comuni a piacere.
LUOGHI = [
    "napoli", "casoria", 
    "afragola", "giugliano", "acerra", "arzano",
    "marano", "qualiano", "quarto"
]

# True  = i bandi di cui non si riesce a leggere la scadenza compaiono in una
#         sezione separata "Scadenza da verificare"
# False = vengono scartati
MOSTRA_SCADENZA_SCONOSCIUTA = True

# Massimo di pagine di risultati (da 50 bandi) lette da inPA per ogni parola chiave
MAX_PAGINE_INPA = 3

# Massimo di pagine di bando che lo script apre per cercare la scadenza
MAX_PAGINE_BANDO = 80

# nome, url, filtra_luogo
# ATTENZIONE: verifica che gli indirizzi siano ancora validi nel browser.
# Per aggiungere un comune: copia una riga e metti l'URL della sua pagina
# "Bandi di concorso" / "Amministrazione trasparente".
FONTI = [
    ("Gazzetta Ufficiale - Concorsi",
     "https://www.gazzettaufficiale.it/30giorni/concorsi", True),
    ("MIM - Ministero dell'Istruzione e del Merito",
     "https://www.mim.gov.it/", False),
    ("USR Campania - Notizie",
     "https://www.mim.gov.it/web/miur-usr-campania/notizie?p_p_id=1_WAR_miurmulticategoriesnavigator100SNAPSHOT&p_p_lifecycle=1&p_p_state=normal&p_p_mode=view&p_r_p_categoryIds=7905906", False),
    ("UAT Napoli (Ufficio Scolastico Territoriale)",
     "https://www.uat-napoli.it/", False),
    ("Comune di Napoli",
     "https://www.comune.napoli.it/", False),
    # Siti che raccolgono i bandi (utili finché non aggiungiamo inPA)
    ("Concorsando - Concorsi Napoli e provincia",
     "https://www.concorsando.it/blog/concorso-comune-di-napoli", False),
    ("Concorsando - Comune di Napoli",
     "https://www.concorsando.it/blog/concorsi-pubblici-online/concorsi-suddivisi-per-regione/campania/napoli/comune-di-napoli/", False),
    # ("Comune di ...", "https://www.comune.xxx.na.it/...", False),
]

# Fonti gestite con codice dedicato (non pagine HTML): compaiono comunque in fondo alla pagina
NOME_INPA = "inPA - Portale del reclutamento"
FONTI_SPECIALI = [(NOME_INPA, "https://www.inpa.gov.it/", False)]

INPA_API = "https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better"

CARTELLA = Path(__file__).parent
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))  # True quando gira sui server di GitHub
if IN_ACTIONS:
    # su GitHub: la pagina va in docs/ (la pubblica GitHub Pages) e l'archivio in dati/
    FILE_HTML = CARTELLA / "docs" / "index.html"
    FILE_VISTI = CARTELLA / "dati" / "visti.json"
else:
    FILE_HTML = CARTELLA / "concorsi_trovati.html"
    FILE_VISTI = CARTELLA / ".concorsi_visti.json"
# Intestazioni da normale browser (Safari su Mac): alcuni siti bloccano i programmi con errore 403
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
SESSIONE = requests.Session()  # mantiene i cookie tra una richiesta e l'altra

# ---------------------------------------------------------------- SCADENZE

MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
    "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
    "novembre": 11, "dicembre": 12,
}
RE_NUM = re.compile(r"\b(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})\b")
RE_TXT = re.compile(
    r"\b(\d{1,2})\s*°?\s*(" + "|".join(MESI) + r")\s+(\d{4})\b", re.IGNORECASE
)
# Parole che di solito precedono la data di scadenza
RE_CHIAVE = re.compile(
    r"scadenz\w*|entro il|entro e non oltre|entro le ore|termine\w*|fino al|"
    r"presentazione delle domande",
    re.IGNORECASE,
)

_cache_pagine = {}
_pagine_aperte = 0
STATO_FONTI = {}  # esito del controllo di ogni fonte, mostrato in fondo alla pagina


def contiene_parola_chiave(testo):
    """True se una parola chiave compare come inizio di parola (evita 'indeterminato', ecc.)."""
    t = testo.lower()
    return any(re.search(r"\b" + re.escape(p), t) for p in PAROLE_CHIAVE)


def prima_data(testo):
    """Restituisce la prima data (date) trovata nel testo, o None."""
    trovate = []
    for m in RE_NUM.finditer(testo):
        try:
            trovate.append((m.start(), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            pass
    for m in RE_TXT.finditer(testo):
        try:
            trovate.append((m.start(), date(int(m.group(3)), MESI[m.group(2).lower()], int(m.group(1)))))
        except ValueError:
            pass
    return min(trovate)[1] if trovate else None


def scadenza_da_testo(testo):
    """Cerca una data subito dopo parole come 'scadenza' o 'entro il'."""
    for m in RE_CHIAVE.finditer(testo):
        d = prima_data(testo[m.start(): m.start() + 160])
        if d:
            return d
    return None


def testo_pagina(url):
    """Scarica una pagina di bando (solo HTML) e ne restituisce il testo."""
    global _pagine_aperte
    if url in _cache_pagine:
        return _cache_pagine[url]
    testo = ""
    if (not url.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".zip", ".odt"))
            and _pagine_aperte < MAX_PAGINE_BANDO):
        _pagine_aperte += 1
        try:
            r = SESSIONE.get(url, headers=HEADERS, timeout=20)
            if "html" in r.headers.get("Content-Type", "").lower():
                testo = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:80000]
        except requests.RequestException:
            pass
    _cache_pagine[url] = testo
    return testo


def trova_scadenza(bando):
    """Prima nel testo del link, poi nella pagina del bando."""
    if bando.get("scadenza_api"):
        return bando["scadenza_api"]
    d = scadenza_da_testo(bando["titolo"] + " " + bando["contesto"])
    if d is None and not bando.get("solo_testo"):
        d = scadenza_da_testo(testo_pagina(bando["url"]))
    return d


# ---------------------------------------------------------------- RICERCA


def scarica(nome, url, filtra_luogo):
    """Scarica una pagina e restituisce i bandi pertinenti."""
    try:
        r = SESSIONE.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 403:  # sito che blocca i programmi: riprovo partendo dalla home
            radice = "{0.scheme}://{0.netloc}/".format(urlparse(url))
            SESSIONE.get(radice, headers=HEADERS, timeout=25)
            time.sleep(2)
            r = SESSIONE.get(url, headers={**HEADERS, "Referer": radice}, timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] {nome}: errore di download ({e})")
        STATO_FONTI[nome] = {"errore": True}
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    trovati = []
    tutti_i_link = soup.find_all("a", href=True)
    print(f"  letti {len(tutti_i_link)} link")
    for a in tutti_i_link:
        testo = a.get_text(" ", strip=True)
        if len(testo) < 8:
            continue
        t = testo.lower()
        if not contiene_parola_chiave(t):
            continue
        if any(re.search(rf"\b{e}\b", t) for e in ESCLUDI):
            continue
        if filtra_luogo and not any(l in t for l in LUOGHI):
            continue
        link = urljoin(url, a["href"])
        if link.startswith(("javascript", "mailto")):
            continue
        contesto = a.parent.get_text(" ", strip=True)[:500] if a.parent else ""
        trovati.append({"fonte": nome, "titolo": testo, "url": link, "contesto": contesto})
    STATO_FONTI[nome] = {"errore": False, "link": len(tutti_i_link), "trovati": len(trovati)}
    return trovati


def data_da_valore(v):
    """Converte un valore dell'API (timestamp, 'AAAA-MM-GG...', 'gg/mm/aaaa') in date."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return prima_data(v)
    return None


def scadenza_da_api(item):
    """Cerca nei campi del bando uno che contenga la scadenza. Restituisce (campo, data)."""
    chiavi = sorted(item, key=lambda k: "scadenz" not in k.lower())
    for k in chiavi:
        kl = k.lower()
        if "inizio" in kl or "pubblicaz" in kl:
            continue
        if any(x in kl for x in ("scadenz", "chiusur", "fine", "termine", "deadline", "closing")):
            d = data_da_valore(item[k])
            if d:
                return k, d
    return None, None


def scarica_inpa():
    """inPA: interroga l'API del portale (portale.inpa.gov.it) per ogni parola chiave."""
    trovati, id_visti, campo_usato, chiavi_viste = [], set(), None, []
    try:
        for parola in RICERCHE_INPA:
            for pagina in range(MAX_PAGINE_INPA):
                r = requests.post(
                    INPA_API, params={"page": pagina, "size": 50}, json={"text": parola},
                    headers={**HEADERS, "Accept": "application/json"}, timeout=40,
                )
                r.raise_for_status()
                dati = r.json()
                bandi = dati.get("content", [])
                for item in bandi:
                    if item["id"] in id_visti:
                        continue
                    id_visti.add(item["id"])
                    if not chiavi_viste:
                        chiavi_viste = sorted(item)
                    if item.get("calculatedStatus") not in (None, "OPEN"):
                        continue  # solo bandi aperti
                    titolo = BeautifulSoup(item.get("titolo", ""), "html.parser").get_text(" ", strip=True)
                    descr = BeautifulSoup(item.get("descrizione") or "", "html.parser").get_text(" ", strip=True)
                    sedi = " ".join(item.get("sedi") or [])
                    if not contiene_parola_chiave(titolo):
                        continue  # la ricerca di inPA è larga: il titolo deve parlare di infanzia/nido
                    if any(re.search(rf"\b{e}\b", titolo.lower()) for e in ESCLUDI):
                        continue
                    if not any(l in (sedi + " " + titolo).lower() for l in LUOGHI):
                        continue
                    campo, scad = scadenza_da_api(item)
                    campo_usato = campo_usato or campo
                    trovati.append({
                        "fonte": NOME_INPA, "titolo": titolo,
                        "url": "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=" + item["id"],
                        "contesto": descr[:3000], "scadenza_api": scad, "solo_testo": True,
                    })
                if not bandi or pagina + 1 >= (dati.get("totalPages") or 1):
                    break
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"  [!] {NOME_INPA}: errore ({e})")
        STATO_FONTI[NOME_INPA] = {"errore": True}
        return []
    print(f"  letti {len(id_visti)} bandi, {len(trovati)} aperti a Napoli e provincia")
    if trovati and not campo_usato:
        print("  [i] campo scadenza non riconosciuto, uso il testo. Campi disponibili:")
        print("      " + ", ".join(chiavi_viste))
    STATO_FONTI[NOME_INPA] = {"errore": False, "link": len(id_visti), "trovati": len(trovati)}
    return trovati


def test_inpa():
    """Mostra come l'API del portale inPA descrive un bando (per controllare i campi)."""
    r = requests.post(INPA_API, params={"page": 0, "size": 3}, json={"text": "infanzia"},
                      headers={**HEADERS, "Accept": "application/json"}, timeout=40)
    print("Stato HTTP:", r.status_code)
    dati = r.json()
    print("Campi della pagina:", {k: (v if not isinstance(v, list) else f"[{len(v)} elementi]") for k, v in dati.items()})
    for item in dati.get("content", [])[:1]:
        print("\nCampi del primo bando:")
        for k, v in item.items():
            print(f"  {k}: {str(v)[:90]!r}")
        print("\nScadenza riconosciuta:", scadenza_da_api(item))


def carica_visti():
    if FILE_VISTI.exists():
        return set(json.loads(FILE_VISTI.read_text()))
    return set()


def ora_italia():
    """Data e ora in Italia (i server di GitHub usano l'ora UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        return datetime.now()


def notifica(titolo, messaggio):
    """Notifica: su iPhone/Android con ntfy (se c'è NTFY_TOPIC), altrimenti nativa su Mac."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        headers = {"Title": titolo}
        if os.environ.get("PAGINA_URL"):
            headers["Click"] = os.environ["PAGINA_URL"]
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=messaggio.encode("utf-8"),
                          headers=headers, timeout=20)
        except requests.RequestException as e:
            print(f"  [!] notifica ntfy non inviata ({e})")
    elif sys.platform == "darwin":
        messaggio = messaggio.replace('"', "'")
        titolo = titolo.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{messaggio}" with title "{titolo}"'],
            check=False,
        )


# ---------------------------------------------------------------- PAGINA HTML

CSS = """
:root { --bg:#f5f5f7; --card:#fff; --testo:#1d1d1f; --sec:#6e6e73;
        --link:#0a58ca; --bordo:#d2d2d7; --nuovo:#1a7f37; --urgente:#c62828; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1c1c1e; --card:#2c2c2e; --testo:#f5f5f7; --sec:#a1a1a6;
          --link:#6cb2ff; --bordo:#3a3a3c; --nuovo:#4ac26b; --urgente:#ff6b6b; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--testo);
       font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
       line-height:1.45; }
main { max-width:820px; margin:0 auto; }
h1 { font-size:1.6rem; margin:0 0 4px; }
.sotto { color:var(--sec); margin:0 0 24px; font-size:.95rem; }
h2 { font-size:1.05rem; margin:28px 0 10px; color:var(--sec);
     text-transform:uppercase; letter-spacing:.04em; }
.card { background:var(--card); border:1px solid var(--bordo); border-radius:12px;
        padding:12px 16px; margin:0 0 10px; }
.card a { color:var(--link); text-decoration:none; font-weight:600; }
.card a:hover { text-decoration:underline; }
.meta { color:var(--sec); font-size:.85rem; margin-top:4px; }
.urgente { color:var(--urgente); font-weight:600; }
.nuovo { display:inline-block; background:var(--nuovo); color:#fff; font-size:.7rem;
         font-weight:700; padding:2px 7px; border-radius:99px; margin-right:8px;
         vertical-align:middle; }
.vuoto { color:var(--sec); padding:24px 0; }
.nota { color:var(--sec); font-size:.85rem; margin-top:32px; }
ul.fonti { list-style:none; margin:0; padding:0; }
ul.fonti li { background:var(--card); border:1px solid var(--bordo); border-radius:12px;
              padding:10px 16px; margin:0 0 8px; }
ul.fonti a { color:var(--link); text-decoration:none; font-weight:600; word-break:break-word; }
ul.fonti a:hover { text-decoration:underline; }
.errore { color:var(--urgente); }
"""


def card(b, nuovi_url, oggi):
    badge = '<span class="nuovo">NUOVO</span>' if b["url"] in nuovi_url else ""
    d = b["scadenza"]
    if d:
        giorni = (d - oggi).days
        quando = "scade oggi" if giorni == 0 else f"tra {giorni} giorni"
        classe = ' class="urgente"' if giorni <= 7 else ""
        scad = f'Scadenza: <span{classe}>{d:%d/%m/%Y} ({quando})</span>'
    else:
        scad = "Scadenza non rilevata: controlla il bando"
    return (
        '<div class="card">' + badge +
        f'<a href="{html.escape(b["url"], quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">{html.escape(b["titolo"])}</a>'
        f'<div class="meta">{html.escape(b["fonte"])} &middot; {scad}</div></div>'
    )


def genera_html(aperti, da_verificare, nuovi_url, ora, scaduti):
    oggi = ora.date()
    corpo = []
    if aperti:
        corpo.append(f"<h2>Bandi aperti ({len(aperti)})</h2>")
        corpo += [card(b, nuovi_url, oggi) for b in aperti]
    if da_verificare:
        corpo.append(f"<h2>Scadenza da verificare ({len(da_verificare)})</h2>")
        corpo += [card(b, nuovi_url, oggi) for b in da_verificare]
    if not aperti and not da_verificare:
        corpo.append('<p class="vuoto">Nessun bando valido trovato in questa ricerca.</p>')

    righe = []
    for nome, url, _ in FONTI + FONTI_SPECIALI:
        st = STATO_FONTI.get(nome)
        if st is None:
            stato = "non controllata"
        elif st["errore"]:
            stato = '<span class="errore">non raggiungibile</span>'
        else:
            stato = f'{st["link"]} link letti, {st["trovati"]} pertinenti'
        righe.append(
            f'<li><a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(nome)}</a>'
            f'<div class="meta">{stato}</div></li>'
        )
    fonti_html = "\n<h2>Fonti consultate</h2>\n<ul class=\"fonti\">" + "\n".join(righe) + "</ul>"

    totale = len(aperti) + len(da_verificare)
    pagina = (
        '<!DOCTYPE html>\n<html lang="it">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Concorsi infanzia - Napoli e provincia</title>\n'
        "<style>" + CSS + "</style>\n</head>\n<body>\n<main>\n"
        "<h1>Concorsi infanzia - Napoli e provincia</h1>\n"
        f'<p class="sotto">Aggiornato il {ora:%d/%m/%Y alle %H:%M} &middot; '
        f"{totale} bandi non scaduti, {len(nuovi_url)} nuovi "
        f"({scaduti} scaduti esclusi)</p>\n"
        + "\n".join(corpo) + fonti_html +
        '\n<p class="nota">La scadenza è letta automaticamente dal testo e può '
        "contenere errori: verifica sempre requisiti e data sul bando ufficiale.</p>\n"
        "</main>\n</body>\n</html>\n"
    )
    FILE_HTML.write_text(pagina, encoding="utf-8")


# ---------------------------------------------------------------- MAIN


def main():
    if "--test-inpa" in sys.argv:
        test_inpa()
        return
    ora = ora_italia()
    oggi = ora.date()
    FILE_HTML.parent.mkdir(parents=True, exist_ok=True)
    FILE_VISTI.parent.mkdir(parents=True, exist_ok=True)
    print(f"Ricerca concorsi - {ora:%d/%m/%Y %H:%M}\n")
    visti = carica_visti()

    candidati = {}
    for nome, url, filtra in FONTI:
        print(f"- {nome}")
        for b in scarica(nome, url, filtra):
            candidati.setdefault(b["url"], b)  # niente doppioni

    print(f"- {NOME_INPA}")
    for b in scarica_inpa():
        candidati.setdefault(b["url"], b)

    print(f"\nControllo le scadenze di {len(candidati)} bandi...")
    aperti, da_verificare, scaduti = [], [], 0
    for b in candidati.values():
        b["scadenza"] = trova_scadenza(b)
        if b["scadenza"] is None:
            if MOSTRA_SCADENZA_SCONOSCIUTA:
                da_verificare.append(b)
        elif b["scadenza"] < oggi:
            scaduti += 1
        else:
            aperti.append(b)

    aperti.sort(key=lambda b: b["scadenza"])  # prima quelli in scadenza
    mostrati = aperti + da_verificare
    nuovi_url = {b["url"] for b in mostrati if b["url"] not in visti}
    da_verificare.sort(key=lambda b: b["url"] not in nuovi_url)  # nuovi per primi

    genera_html(aperti, da_verificare, nuovi_url, ora, scaduti)
    FILE_VISTI.write_text(json.dumps(sorted(visti | set(candidati))))

    print(f"Aperti: {len(aperti)} | da verificare: {len(da_verificare)} | "
          f"scaduti esclusi: {scaduti} | nuovi: {len(nuovi_url)}")
    if nuovi_url:
        notifica("Concorsi infanzia", f"{len(nuovi_url)} nuovi bandi non scaduti")
    print(f"Pagina creata: {FILE_HTML}")
    if sys.platform == "darwin" and not IN_ACTIONS:
        subprocess.run(["open", str(FILE_HTML)], check=False)  # apre nel browser


if __name__ == "__main__":
    main()
