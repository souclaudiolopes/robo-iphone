"""Robô de ofertas de iPhone 15/16/17 Pro e Pro Max -> Telegram.

Regras (ajuste em CONFIG):
- Só aparelhos novos (nacional ou versão EUA/eSIM).
- Pro Max: alerta sempre que o preço for <= PRECO_MAX.
- Pro: alerta só se o preço for <= PRECO_MAX e a oferta tiver cupom.
- Cupons avulsos para iPhone (ex.: "R$ 300 OFF em iPhone") também são avisados.
"""
import html
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CONFIG = {
    "PRECO_MAX": 7000,
    "PRECO_MIN": 2500,  # abaixo disso é acessório, golpe ou anúncio errado
    "ALERTAR_CUPONS_AVULSOS": True,
}

VISTOS_ARQ = Path(__file__).with_name("vistos.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}

MODELO_RE = re.compile(r"iphone\s*(1[5-7])\s*(pro\s*max|promax|pro)\b")
# números 11-19 soltos = modelos ("iphone 17 16 15", "17 pro ou 16 pro" => acessório/comparação);
# ignora specs como "12 gb", "48+12+12mp", "a19", "12 meses"
NUM_MODELO_RE = re.compile(r"\b(1[1-9])\b(?!\s*(?:gb|tb|mp|w|mah|%|\+|meses|vezes|parcelas))")
EXCLUIR = [
    "seminovo", "semi novo", "usado", "recondicionado", "renewed", "vitrine",
    "open box", "cpo", "excelente estado", "bom estado", "grade a", "grade b",
    "capa", "capinha", "case", "pelicula", "carregador", "cabo", "fone",
    "suporte", "lente", "protetor", "compativel", "para iphone", "bateria",
    "tela para", "display para",
]


def norm(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt.lower()).strip()


def preco_br(txt):
    """'R$ 6.615' / '7.469,90' / 6615.0 -> float. None se não achar."""
    if isinstance(txt, (int, float)):
        return float(txt)
    m = re.search(r"\d[\d.]*(?:,\d{1,2})?", txt or "")
    if not m:
        return None
    return float(m.group().replace(".", "").replace(",", "."))


def identificar(titulo):
    """Retorna (geracao, 'Pro'|'Pro Max') ou None se não for alvo."""
    t = norm(titulo).replace("propro", "pro")  # erro comum de título ("ProPro Max")
    if any(re.search(rf"\b{re.escape(p)}\b", t) for p in EXCLUIR):
        return None
    m = MODELO_RE.search(t)
    if not m:
        return None
    # títulos de acessório listam vários modelos ("iphone 17 16 15 pro max")
    if len(set(NUM_MODELO_RE.findall(t))) > 1:
        return None
    variante = "Pro Max" if "max" in m.group(2) else "Pro"
    return int(m.group(1)), variante


def eh_cupom_avulso_iphone(titulo):
    """Cupom/desconto avulso que pode servir para os modelos-alvo."""
    t = norm(titulo)
    if "iphone" not in t or re.search(r"\b(air|1[5-7]e)\b", t):
        return False
    depois = t[t.index("iphone"):]  # ignora "10% OFF" etc. antes do modelo
    nums = {int(n) for n in re.findall(r"\b(1\d)\b", depois)}
    if not nums:
        return True  # cupom genérico para iPhones
    return bool(nums & {15, 16, 17}) and "pro" in t


def decidir(oferta):
    """Recebe dict com titulo, preco, cupom, cupom_avulso. Retorna motivo ou None."""
    if oferta.get("cupom_avulso"):
        if CONFIG["ALERTAR_CUPONS_AVULSOS"] and eh_cupom_avulso_iphone(oferta["titulo"]):
            return "cupom"
        return None
    alvo = identificar(oferta["titulo"])
    preco = oferta.get("preco")
    if not alvo or preco is None:
        return None
    if not (CONFIG["PRECO_MIN"] <= preco <= CONFIG["PRECO_MAX"]):
        return None
    _, variante = alvo
    if variante == "Pro Max":
        return "promax"
    return "pro_cupom" if oferta.get("cupom") else None


# ---------------------------------------------------------------- fontes

def get(url, **kw):
    r = requests.get(url, headers={**HEADERS, **kw.pop("headers", {})}, timeout=20, **kw)
    r.raise_for_status()
    return r


def pelando_parse_busca(html_txt):
    soup = BeautifulSoup(html_txt, "html.parser")
    ofertas = []
    for a in soup.select("h3 a[data-deal-id]"):
        url = a.get("href", "")
        if "/d/" not in url or a.get("data-inactive") == "true":
            continue  # /t/ = discussão; inactive = expirada
        card = a.find_parent("li") or a.find_parent("div")
        stamp = card.select_one('[class*="deal-card-stamp"]') if card else None
        stamp_txt = stamp.get_text("", strip=True) if stamp else ""
        loja = card.select_one('[class*="deal-card-store"] a, [class*="deal-card-store"] strong')
        ofertas.append({
            "fonte": "Pelando",
            "id": a["data-deal-id"],
            "titulo": a.get_text(strip=True),
            "preco": None if "OFF" in stamp_txt.upper() else preco_br(stamp_txt),
            "cupom_avulso": "OFF" in stamp_txt.upper(),
            "desconto": stamp_txt if "OFF" in stamp_txt.upper() else None,
            "loja": loja.get_text(strip=True) if loja else "",
            "url": url,
            "cupom": None,
        })
    return ofertas


def pelando_parse_cupom(html_txt):
    soup = BeautifulSoup(html_txt, "html.parser")
    pill = soup.select_one(".pill--coupon[data-code]")
    return pill["data-code"].strip() if pill and pill["data-code"].strip() else None


def fonte_pelando():
    vistos = {}
    for termo in ["iphone-15-pro", "iphone-16-pro", "iphone-17-pro", "cupom-iphone"]:
        for o in pelando_parse_busca(get(f"https://www.pelando.com.br/busca/{termo}").text):
            vistos[o["id"]] = o
    ofertas = list(vistos.values())
    # só abre a página da oferta (pra ler o cupom) quando pode fazer diferença
    for o in ofertas:
        if o["cupom_avulso"] or (identificar(o["titulo"]) and o["preco"]
                                 and CONFIG["PRECO_MIN"] <= o["preco"] <= CONFIG["PRECO_MAX"]):
            try:
                o["cupom"] = pelando_parse_cupom(get(o["url"]).text)
            except requests.RequestException:
                pass
    return ofertas


def promobit_converter(o):
    preco = o.get("offer_price")
    titulo = o.get("offer_title", "")
    avulso = (preco is not None and preco < 1) and bool(o.get("offer_coupon"))
    return {
        "fonte": "Promobit",
        "id": str(o.get("offer_id")),
        "titulo": titulo,
        "preco": None if avulso else preco,
        "cupom_avulso": avulso,
        "desconto": None,
        "loja": (o.get("store_name") or "").strip(),
        "url": f"https://www.promobit.com.br/oferta/{o.get('offer_slug')}/",
        "cupom": (o.get("offer_coupon") or "").strip() or None,
    }


def fonte_promobit():
    api = "https://api.promobit.com.br"
    h = {"Origin": "https://www.promobit.com.br", "Referer": "https://www.promobit.com.br/"}
    brutas = []
    # feed geral das mais recentes (3 páginas ≈ últimas horas)
    after = None
    for _ in range(3):
        j = get(f"{api}/offers", params={"after": after} if after else None, headers=h).json()
        brutas += j.get("offers", [])
        after = j.get("after")
        if not after:
            break
    # busca por modelo (só ofertas ativas)
    for termo in ["iphone 15 pro", "iphone 16 pro", "iphone 17 pro"]:
        for pagina in (1, 2):
            j = get(f"{api}/search", params={"q": termo, "page": pagina}, headers=h).json()
            brutas += j.get("active_offers", [])
    unicas = {str(o.get("offer_id")): o for o in brutas}
    return [promobit_converter(o) for o in unicas.values()]


def amazon_parse(html_txt):
    soup = BeautifulSoup(html_txt, "html.parser")
    ofertas = []
    for d in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = d.get("data-asin")
        h2 = d.select_one("h2")
        preco = d.select_one(".a-price:not(.a-text-price) .a-offscreen")
        if not (asin and h2 and preco):
            continue
        texto = d.get_text(" ", strip=True)
        m = re.search(r"(economize[^.]{0,40}?cupom|cupom[^.]{0,40})", texto, re.I)
        ofertas.append({
            "fonte": "Amazon",
            "id": asin,
            "titulo": h2.get_text(" ", strip=True),
            "preco": preco_br(preco.get_text()),
            "cupom_avulso": False,
            "desconto": None,
            "loja": "Amazon",
            "url": f"https://www.amazon.com.br/dp/{asin}",
            "cupom": m.group(1) if m else None,
        })
    return ofertas


def fonte_amazon():
    ofertas = {}
    for termo in ["iphone 15 pro", "iphone 16 pro", "iphone 17 pro"]:
        r = get("https://www.amazon.com.br/s", params={"k": termo})
        if "captcha" in r.text.lower()[:20000] and "s-search-result" not in r.text:
            raise RuntimeError("bloqueado (captcha)")
        for o in amazon_parse(r.text):
            ofertas[o["id"]] = o
    return list(ofertas.values())


FONTES = {"Pelando": fonte_pelando, "Promobit": fonte_promobit, "Amazon": fonte_amazon}


# ---------------------------------------------------------------- telegram

def formatar(o, motivo):
    e = html.escape
    if motivo == "cupom":
        cab = "🎟️ <b>Cupom para iPhone</b>"
    elif motivo == "promax":
        cab = "🔥 <b>Pro Max até R$ {:,.0f}</b>".format(CONFIG["PRECO_MAX"]).replace(",", ".")
    else:
        cab = "🔥 <b>Pro com cupom até R$ {:,.0f}</b>".format(CONFIG["PRECO_MAX"]).replace(",", ".")
    linhas = [cab, "", e(o["titulo"])]
    if o.get("preco"):
        linhas.append("💰 R$ {:,.2f}".format(o["preco"]).replace(",", "X").replace(".", ",").replace("X", "."))
    if o.get("desconto"):
        linhas.append(f"💸 {e(o['desconto'])}")
    if o.get("cupom"):
        linhas.append(f"🎟️ Cupom: <code>{e(o['cupom'])}</code>")
    if re.search(r"\be[\s-]?sim\b", norm(o["titulo"])):
        linhas.append("🇺🇸 Versão eSIM")
    linhas.append(f"🏪 {e(o.get('loja') or '?')} · via {o['fonte']}")
    linhas.append(o["url"])
    return "\n".join(linhas)


def enviar(texto):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
        "chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": False})
    if not r.ok:
        raise RuntimeError(f"Telegram respondeu {r.status_code}: {r.text[:200]}")


# ---------------------------------------------------------------- main

def chave(o):
    preco = round(o["preco"]) if o.get("preco") else None
    return f"{o['fonte']}:{o['id']}:{preco}:{o.get('cupom')}"


def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or "--teste" in sys.argv
    vistos = json.loads(VISTOS_ARQ.read_text()) if VISTOS_ARQ.exists() else []
    vistos_set = set(vistos)
    status, enviados = [], 0

    try:
        for nome, fn in FONTES.items():
            try:
                ofertas = fn()
                status.append(f"✅ {nome}: {len(ofertas)} ofertas lidas")
            except Exception as ex:  # uma fonte fora do ar não derruba as outras
                status.append(f"⚠️ {nome}: falhou ({str(ex)[:80]})")
                continue
            for o in ofertas:
                motivo = decidir(o)
                if not motivo or chave(o) in vistos_set:
                    continue
                enviar(formatar(o, motivo))
                vistos.append(chave(o))
                vistos_set.add(chave(o))
                enviados += 1
    finally:  # salva o que já foi enviado mesmo se algo quebrar no meio
        VISTOS_ARQ.write_text(json.dumps(vistos[-2000:], ensure_ascii=False, indent=0))

    print("\n".join(status), f"\nAlertas enviados: {enviados}")
    if manual:
        enviar("🤖 <b>Robô de iPhone rodando</b>\n\n" + "\n".join(html.escape(s) for s in status)
               + f"\n\nAlertas novos nesta rodada: {enviados}")


if __name__ == "__main__":
    main()
