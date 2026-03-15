import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════
SCRAPERAPI_KEY = "3a4f98804a2b98772342d286824afcd2"

HEADERS_JSON = {
    "User-Agent": "price-crawler/1.0",
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def limpar_preco(v):
    if v is None:
        return None
    s = str(v).replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
    s = re.sub(r"\.(?=\d{3}[,.])", "", s)
    s = s.replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        f = float(s)
        return f if 0.5 < f < 100_000 else None
    except ValueError:
        return None


PADROES_PROMO = [
    (r"(?:compre|leve)\s*(\d+)\s*(?:e\s*)?(?:pague|leve)\s*(\d+)",
     lambda m: f"Leve {m.group(1)} pague {m.group(2)}"),
    (r"(\d+)\s*%\s*(?:de\s*)?(?:desconto|off)\s*(?:na\s*)?(\d+)[ªº°]?\s*unidade",
     lambda m: f"{m.group(1)}% off na {m.group(2)}ª unidade"),
    (r"compre\s*(\d+)\s*(?:e\s*)?pague\s*R?\$?\s*([\d.,]+)\s*(?:em cada|por unid|cada)",
     lambda m: f"Compre {m.group(1)} pague R${m.group(2)} cada"),
    (r"(\d+)\s*%\s*(?:de\s*)?(?:desconto|off)",
     lambda m: f"{m.group(1)}% off"),
    (r"(?:ganhe|com)\s+brinde",
     lambda m: "Acompanha brinde"),
]


def detectar_promocao(texto):
    if not texto:
        return None
    t = str(texto).lower()
    for padrao, fmt in PADROES_PROMO:
        m = re.search(padrao, t)
        if m:
            try:
                return fmt(m)
            except Exception:
                continue
    return None


def _price_unit_e_divisao(preco_unit, preco_total, tolerancia=0.02):
    """
    Retorna True se pricePerUnit for apenas a divisao matematica
    do preco total por N itens (N entre 2 e 6).
    Ex: total=140, pricePerUnit=70 -> 140/2=70 -> e divisao, nao promocao.
    Ex: total=77,  pricePerUnit=54.9 -> nao bate em nenhuma divisao -> e promocao real.
    """
    if not preco_unit or not preco_total:
        return False
    for n in [2, 3, 4, 5, 6]:
        divisao = preco_total / n
        if abs(preco_unit - divisao) / max(preco_unit, 0.01) < tolerancia:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  PANVEL
# ═══════════════════════════════════════════════════════════════
def buscar_panvel(link):
    try:
        r = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        html = r.text
    except Exception as e:
        print(f"  [Panvel] Erro: {e}")
        return None

    m = re.search(r'p-(\d+)$', link.rstrip('/'))
    if not m:
        print(f"  [Panvel] ID nao encontrado na URL")
        return None
    pid = m.group(1)

    chave = f'"G.json.api/v2/catalog/{pid}?type=SSR"'
    idx   = html.find(chave)
    if idx == -1:
        print(f"  [Panvel] Bloco JSON nao encontrado")
        return None

    trecho = html[max(0, idx - 3000):idx]

    is_de_por = '"PROMOTION"' in trecho and bool(
        re.search(r'"promotionId"\s*:\s*\d+', trecho)
    )

    preco_unit = None
    m_pu = re.search(r'"pricePerUnit"\s*:\s*([\d.]+)', trecho)
    if m_pu:
        preco_unit = limpar_preco(m_pu.group(1))

    preco_parcelas = None
    m_inst = re.search(
        r'"installments"\s*:\s*"ou\s*(\d+)x\s*de\s*R\$[\xa0\s]*([\d,.]+)"',
        trecho
    )
    if m_inst:
        qtd  = int(m_inst.group(1))
        parc = limpar_preco(m_inst.group(2))
        if parc:
            preco_parcelas = round(parc * qtd, 2)

    if is_de_por:
        preco    = preco_parcelas
        promocao = "DE/POR"
    elif preco_unit and not _price_unit_e_divisao(preco_unit, preco_parcelas):
        preco    = preco_unit
        promocao = "Leve mais, pague menos"
    else:
        preco    = preco_parcelas
        promocao = None

    if not preco:
        preco = preco_unit or preco_parcelas
    if not preco:
        print(f"  [Panvel] Nenhum preco encontrado")
        return None

    nome = None
    m_nome = re.search(
        rf'"G\.json\.api/v2/catalog/{pid}\?type=SSR"\s*:\s*\{{"body"\s*:\s*\{{[^{{}}]{{0,50}}"name"\s*:\s*"([^"]+)"',
        html
    )
    if m_nome:
        nome = m_nome.group(1)
    else:
        soup  = BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        if title:
            nome = title.get_text(strip=True)\
                        .replace(" | Panvel Farmacias", "")\
                        .replace(" | Panvel", "").strip()
    nome = (nome or "Produto Panvel")[:120]

    return {
        "site":           "Panvel",
        "nome":           nome,
        "preco":          preco,
        "preco_original": None,
        "promocao":       promocao,
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  PAGUE MENOS
# ═══════════════════════════════════════════════════════════════
def buscar_paguemenos(link):
    try:
        r    = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        html = r.text
    except Exception as e:
        print(f"  [Pague Menos] Erro: {e}")
        return None

    nome           = None
    preco          = None
    preco_original = None
    promocao       = None

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
            if isinstance(d, list):
                d = d[0]
            if d.get("@type") not in ("Product", "IndividualProduct"):
                continue
            nome = d.get("name", "")
            o    = d.get("offers", {})
            if isinstance(o, list):
                o = o[0]
            p = o.get("price") or o.get("lowPrice")
            if p:
                preco = limpar_preco(p)
                break
        except Exception:
            continue

    bloco = soup.select_one("[class*='pdp-custom']")
    if bloco:
        txt = bloco.get_text(strip=True)
        precos_vals = []
        for p in re.findall(r'R\$\s*([\d\.]+,\d{2})', txt):
            v = limpar_preco(p)
            if v:
                precos_vals.append(v)

        tem_off = bool(re.search(r'%\s*OFF', txt, re.IGNORECASE))

        if tem_off and len(precos_vals) >= 2:
            # Calcula o desconto a partir dos dois preços
            # Evita parsear "9914%" quando o % fica colado ao preço anterior
            orig  = precos_vals[0]
            final = precos_vals[-1]
            pct   = round((1 - final / orig) * 100)
            if 1 <= pct <= 99:
                preco_original = orig
                preco          = final
                promocao       = f"DE/POR — {pct}% off"
        elif precos_vals and not preco:
            preco = precos_vals[0]

    if not preco:
        print(f"  [Pague Menos] Preco nao encontrado")
        return None

    if not promocao:
        for s in soup.find_all("script"):
            txt_s = s.string or ""
            if "commertialOffer" not in txt_s:
                continue
            m_t = re.search(
                r'"Teaser:[^"]+"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                txt_s
            )
            if m_t:
                raw = m_t.group(1).strip()
                if raw and not raw.startswith("$"):
                    promocao = detectar_promocao(raw) or raw[:80]
                    break
            m_t2 = re.search(r'"teaserName"\s*:\s*"([^"]+)"', txt_s)
            if m_t2:
                raw = m_t2.group(1).strip()
                if raw and not raw.startswith("$"):
                    promocao = detectar_promocao(raw) or raw[:80]
                    break

    if not promocao:
        clusters = re.findall(
            r'"productClusters\.\d+"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
            html
        )
        for c in clusters:
            c_lower = c.lower()
            if (any(p in c_lower for p in ['%', 'off', 'desconto', 'leve', 'pague', 'brinde', 'unidade', 'un'])
                    and any(ch.isdigit() for ch in c_lower)):
                promo_detectada = detectar_promocao(c)
                if promo_detectada:
                    promocao = promo_detectada
                    break
                elif re.search(r'\d+\s*%', c_lower):
                    promocao = c[:80]
                    break

    if not nome:
        h1 = soup.select_one("h1")
        nome = h1.get_text(strip=True) if h1 else "Produto Pague Menos"

    return {
        "site":           "Pague Menos",
        "nome":           (nome or "Produto Pague Menos")[:120],
        "preco":          preco,
        "preco_original": preco_original,
        "promocao":       promocao,
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  AMAZON
# ═══════════════════════════════════════════════════════════════
def buscar_amazon(link):
    link = link.replace("https://amazon.com.br", "https://www.amazon.com.br")

    if not SCRAPERAPI_KEY or SCRAPERAPI_KEY == "SUA_CHAVE_AQUI":
        print(f"  [Amazon] ScraperAPI nao configurado — pulando")
        return None

    fetch_url = (
        f"https://api.scraperapi.com/"
        f"?api_key={SCRAPERAPI_KEY}&url={link}&country_code=br"
    )
    try:
        r    = httpx.get(fetch_url, headers=HEADERS_HTML, timeout=35, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Amazon] Erro: {e}")
        return None

    if "captcha" in r.text.lower():
        print(f"  [Amazon] CAPTCHA mesmo com ScraperAPI")
        return None

    nome_el = soup.find(id="productTitle")
    nome    = nome_el.text.strip() if nome_el else "Produto Amazon"

    preco = None
    for sel in [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".a-price.aok-align-center .a-offscreen",
        ".a-price .a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            preco = limpar_preco(el.get_text())
            if preco:
                break

    if not preco:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(tag.string or "")
                if isinstance(d, list):
                    d = d[0]
                o = d.get("offers", {})
                if isinstance(o, list):
                    o = o[0]
                preco = limpar_preco(o.get("price"))
                if preco:
                    break
            except Exception:
                continue

    if not preco:
        print(f"  [Amazon] Preco nao encontrado")
        return None

    preco_original = None
    el = soup.select_one(".a-text-price .a-offscreen")
    if el:
        v = limpar_preco(el.get_text())
        if v and v > preco:
            preco_original = v

    promocao = None
    if preco_original:
        d = round((1 - preco / preco_original) * 100)
        if 1 <= d <= 99:
            promocao = f"DE/POR — {d}% off"

    if not promocao:
        badge = soup.select_one("#dealBadgeSupportingText, .a-badge-label")
        if badge:
            txt = badge.get_text(strip=True)
            palavras_desconto = ["%", "off", "desconto", "economize", "cupom"]
            if txt and len(txt) < 60 and any(p in txt.lower() for p in palavras_desconto):
                promocao = txt

    return {
        "site":           "Amazon Brasil",
        "nome":           nome,
        "preco":          preco,
        "preco_original": preco_original,
        "promocao":       promocao,
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  ROTEADOR
# ═══════════════════════════════════════════════════════════════
def buscar_por_link(link):
    d = link.lower()
    if "amazon"       in d:
        return buscar_amazon(link)
    elif "paguemenos" in d:
        return buscar_paguemenos(link)
    elif "panvel"     in d:
        return buscar_panvel(link)
    else:
        return _html_generico(link, link.split("/")[2].replace("www.", ""))


def _html_generico(link, nome_site):
    try:
        r    = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [{nome_site}] Erro: {e}")
        return None
    preco = None
    nome  = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
            if isinstance(d, list):
                d = d[0]
            if d.get("@type") not in ("Product", "IndividualProduct"):
                continue
            nome = d.get("name", "")
            o    = d.get("offers", {})
            if isinstance(o, list):
                o = o[0]
            p = o.get("price") or o.get("lowPrice")
            if p:
                preco = limpar_preco(p)
                if preco:
                    break
        except Exception:
            continue
    if not preco:
        return None
    if not nome:
        h1 = soup.select_one("h1")
        nome = h1.get_text(strip=True) if h1 else "Produto"
    return {
        "site":           nome_site,
        "nome":           nome[:120],
        "preco":          preco,
        "preco_original": None,
        "promocao":       None,
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    with open("products.json", encoding="utf-8") as f:
        produtos = json.load(f)

    try:
        with open("prices.json", encoding="utf-8") as f:
            historico = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        historico = []

    hoje  = datetime.date.today().isoformat()
    novos = []

    for produto in produtos:
        print(f"\n{'─'*55}")
        print(f"Buscando: {produto['nome']}")

        for link in produto.get("links", []):
            time.sleep(2)
            resultado = buscar_por_link(link)
            if resultado:
                novos.append({**resultado, "produto_buscado": produto["nome"], "data": hoje})
                promo = f" | {resultado['promocao']}" if resultado['promocao'] else ""
                print(f"  [OK] {resultado['site']} — R$ {resultado['preco']:.2f}{promo}")
            else:
                print(f"  [--] {link.split('/')[2].replace('www.','')} — nao retornou preco")

    corte     = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    historico = [h for h in historico if h["data"] >= corte]
    historico.extend(novos)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*55}")
    print(f"Concluido! {len(novos)} precos coletados e salvos.")


if __name__ == "__main__":
    main()
