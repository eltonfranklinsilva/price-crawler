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
    s = str(v).replace("R$","").replace("\xa0","").replace(" ","").strip()
    s = re.sub(r"\.(?=\d{3}[,.])", "", s)
    s = s.replace(",",".")
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


# ═══════════════════════════════════════════════════════════════
#  PAGUE MENOS
#
#  A API VTEX retorna [] para esses produtos (slug não funciona).
#  Solução: extrai do bloco [class*='pdp-custom'] que contém
#  no texto: nome + preço original + "X% OFF" + preço final.
#
#  Exemplos do bloco:
#  → sem promoção: "...R$ 76,49Quantidade..."
#  → com DE/POR:   "...R$ 259,9914% OFFR$ 223,90Quantidade..."
#
#  O badge "-50% na 2ª unidade" só aparece após JS — não está
#  no HTML estático. Quando presente, registra como "Promoção
#  ativa (detalhes no site)" para não deixar o campo vazio.
# ═══════════════════════════════════════════════════════════════
def buscar_paguemenos(link):
    try:
        r    = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        html = r.text
    except Exception as e:
        print(f"  [Pague Menos] Erro: {e}")
        return None

    # ── Nome via JSON-LD ─────────────────────────────────────
    nome  = None
    preco = None
    preco_original = None
    promocao = None

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
            if isinstance(d, list):
                d = d[0]
            if d.get("@type") not in ("Product", "IndividualProduct"):
                continue
            nome = d.get("name", "")
            o = d.get("offers", {})
            if isinstance(o, list):
                o = o[0]
            p = o.get("price") or o.get("lowPrice")
            if p:
                preco = limpar_preco(p)
                break
        except Exception:
            continue

    # ── Bloco pdp-custom: extrai preço original + desconto + preço final ──
    bloco = soup.select_one("[class*='pdp-custom']")
    if bloco:
        txt = bloco.get_text(strip=True)

        # Extrai todos os preços R$ XX,XX do bloco
        precos_encontrados = re.findall(r'R\$\s*([\d\.]+,\d{2})', txt)
        precos_vals = [limpar_preco(p) for p in precos_encontrados if limpar_preco(p)]

        # Extrai percentual de desconto "14% OFF"
        m_pct = re.search(r'(\d+)\s*%\s*OFF', txt, re.IGNORECASE)

        if m_pct and len(precos_vals) >= 2:
            # Tem DE/POR explícito: primeiro preço = original, último = final
            preco_original = precos_vals[0]
            preco          = precos_vals[-1]
            pct            = int(m_pct.group(1))
            promocao       = f"DE/POR — {pct}% off"
        elif precos_vals:
            # Sem desconto explícito no bloco — usa o preço do JSON-LD
            if not preco:
                preco = precos_vals[0]

    if not preco:
        print(f"  [Pague Menos] Preço não encontrado")
        return None

    # ── Detecta promoção via Teasers no __STATE__ ────────────
    # Os teasers reais ficam no __STATE__ mas como referências,
    # os dados completos estão num bloco separado
    if not promocao:
        # Tenta encontrar o nome real do teaser no __STATE__
        for s in soup.find_all("script"):
            txt_s = s.string or ""
            if "commertialOffer" not in txt_s:
                continue
            # Procura teasers com nome dentro do __STATE__
            m_t = re.search(
                r'"Teaser:\d+"[^{]*\{[^}]*"name"\s*:\s*"([^"]+)"',
                txt_s
            )
            if not m_t:
                # Tenta formato alternativo
                m_t = re.search(
                    r'"teaserName"\s*:\s*"([^"]+)"',
                    txt_s
                )
            if m_t:
                raw = m_t.group(1)
                if raw and not re.match(r'^\$', raw):
                    promocao = detectar_promocao(raw) or raw[:80]
            break

    # ── Fallback: verifica productClusters para promoções ────
    # Ex: "Nutrição Infantil até 50% na 2un"
    if not promocao:
        m_cluster = re.search(
            r'"productClusters\.\d+"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
            html
        )
        if m_cluster:
            raw = m_cluster.group(1)
            # Filtra apenas clusters que parecem promoções (contêm % ou desconto)
            if any(p in raw.lower() for p in ['%', 'off', 'desconto', 'leve', 'pague', 'brinde']):
                promocao = detectar_promocao(raw) or raw[:80]

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
#  PANVEL
#
#  Tipo A — DE/POR: tag="PROMOTION" + promotionId presente
#    → preço = total das parcelas (Nx de R$ Y)
#    → pricePerUnit ignorado
#
#  Tipo B — Leve Mais: sem tag, sem promotionId, pricePerUnit presente
#    → preço = pricePerUnit (preço por unidade na promoção)
#
#  Tipo C — Sem promoção: sem tag, sem promotionId, sem pricePerUnit
#    → preço = total das parcelas
#    → promocao = None
#
#  CORREÇÃO: pricePerUnit de packs sem promoção ativa (ex: pack 2un
#  a R$ 139,99 → pricePerUnit=70) deve ser ignorado. Só usa
#  pricePerUnit quando o produto tem sinalização de promoção ativa.
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
        print(f"  [Panvel] ID não encontrado na URL")
        return None
    pid = m.group(1)

    chave = f'"G.json.api/v2/catalog/{pid}?type=SSR"'
    idx   = html.find(chave)
    if idx == -1:
        print(f"  [Panvel] Bloco JSON não encontrado")
        return None

    trecho = html[max(0, idx - 3000):idx]

    # ── Tipo de promoção ─────────────────────────────────────
    is_de_por    = '"PROMOTION"' in trecho and bool(re.search(r'"promotionId"\s*:\s*\d+', trecho))
    tem_price_pu = bool(re.search(r'"pricePerUnit"\s*:\s*[\d.]+', trecho))

    # ── pricePerUnit ─────────────────────────────────────────
    preco_unit = None
    m_pu = re.search(r'"pricePerUnit"\s*:\s*([\d.]+)', trecho)
    if m_pu:
        preco_unit = limpar_preco(m_pu.group(1))

    # ── Preço total via parcelas ─────────────────────────────
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

    # ── Define preço e promoção por tipo ─────────────────────
    if is_de_por:
        # Tipo A: DE/POR — preço total das parcelas, ignora pricePerUnit
        preco    = preco_parcelas
        promocao = "DE/POR"

    elif tem_price_pu and preco_unit:
        # Tipo B: Leve Mais — usa pricePerUnit
        # Só entra aqui se não é DE/POR mas tem pricePerUnit sinalizado
        # (produto com promoção de bundle ativa)
        preco    = preco_unit
        promocao = "Leve mais, pague menos"

    else:
        # Tipo C: sem promoção ativa — preço total das parcelas
        preco    = preco_parcelas
        promocao = None

    # Fallback: qualquer preço disponível
    if not preco:
        preco = preco_unit or preco_parcelas
    if not preco:
        print(f"  [Panvel] Nenhum preço encontrado")
        return None

    # ── Nome do produto ──────────────────────────────────────
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
                        .replace(" | Panvel Farmácias","")\
                        .replace(" | Panvel","").strip()
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
#  AMAZON
#
#  CORREÇÃO: badge só é usado se contiver % ou palavras de desconto.
#  Textos sazonais como "Semana do Consumidor" são ignorados.
# ═══════════════════════════════════════════════════════════════
def buscar_amazon(link):
    link = link.replace("https://amazon.com.br", "https://www.amazon.com.br")

    if not SCRAPERAPI_KEY or SCRAPERAPI_KEY == "SUA_CHAVE_AQUI":
        print(f"  [Amazon] ScraperAPI não configurado — pulando")
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
        print(f"  [Amazon] Preço não encontrado")
        return None

    # Preço original DE/POR
    preco_original = None
    el = soup.select_one(".a-text-price .a-offscreen")
    if el:
        v = limpar_preco(el.get_text())
        if v and v > preco:
            preco_original = v

    promocao = None

    # DE/POR calculado — prioridade máxima
    if preco_original:
        d = round((1 - preco / preco_original) * 100)
        if 1 <= d <= 99:
            promocao = f"DE/POR — {d}% off"

    # Badge — SOMENTE se contiver indicação real de desconto
    # Ignora textos sazonais como "Semana do Consumidor", "Black Friday", etc.
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
        nome_site = link.split("/")[2].replace("www.", "")
        return _vtex_html_generico(link, nome_site)


def _vtex_html_generico(link, nome_site):
    """Fallback genérico para sites não mapeados."""
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
        print(f"  [{nome_site}] Preço não encontrado")
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
                dominio = link.split("/")[2].replace("www.", "")
                print(f"  [--] {dominio} — nao retornou preco")

    corte     = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    historico = [h for h in historico if h["data"] >= corte]
    historico.extend(novos)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*55}")
    print(f"Concluido! {len(novos)} precos coletados e salvos.")


if __name__ == "__main__":
    main()
