import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════
SCRAPERAPI_KEY = "SUA_CHAVE_AQUI"  # usado só para Amazon

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

HEADERS_JSON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


# ───────────────────────────────────────────────────────────────
# Limpa string de preço → float
# "R$ 1.299,90" → 1299.90
# ───────────────────────────────────────────────────────────────
def limpar_preco(texto):
    if texto is None:
        return None
    texto = str(texto).strip()
    # Remove símbolo e espaços
    texto = texto.replace("R$", "").replace("\xa0", "").replace(" ", "")
    # Remove ponto de milhar antes de vírgula decimal (1.299,90 → 1299,90)
    texto = re.sub(r"\.(?=\d{3},)", "", texto)
    # Troca vírgula decimal por ponto
    texto = texto.replace(",", ".")
    # Mantém só dígitos e ponto
    texto = re.sub(r"[^\d.]", "", texto)
    try:
        v = float(texto)
        return v if v > 0 else None
    except ValueError:
        return None


# ───────────────────────────────────────────────────────────────
# Detecta promoções por padrão de texto
# ───────────────────────────────────────────────────────────────
PADROES_PROMO = [
    (r"(?:compre|leve)\s*(\d+)\s*(?:e\s*)?(?:pague|leve)\s*(\d+)",
     lambda m: f"Leve {m.group(1)} pague {m.group(2)}"),
    (r"(\d+)\s*%\s*(?:de\s*)?(?:desconto|off)\s*(?:na\s*)?(\d+)[ªº°]?\s*unidade",
     lambda m: f"{m.group(1)}% off na {m.group(2)}ª unidade"),
    (r"compre\s*(\d+)\s*(?:e\s*)?pague\s*R?\$?\s*([\d\.,]+)\s*(?:em cada|por unid|cada)",
     lambda m: f"Compre {m.group(1)} pague R${m.group(2)} cada"),
    (r"(\d+)\s*%\s*(?:de\s*)?(?:desconto|off)",
     lambda m: f"{m.group(1)}% off"),
    (r"(?:ganhe|com)\s+brinde",
     lambda m: "Acompanha brinde"),
]

def detectar_promocao(texto):
    if not texto:
        return None
    t = texto.lower()
    for padrao, formatar in PADROES_PROMO:
        m = re.search(padrao, t)
        if m:
            try:
                return formatar(m)
            except Exception:
                continue
    return None


# ═══════════════════════════════════════════════════════════════
#  VTEX API — funciona para Pague Menos, Panvel e qualquer
#  loja na plataforma VTEX sem precisar de JavaScript
#
#  Como funciona: extrai o slug do produto da URL e chama
#  diretamente a API interna do VTEX que retorna JSON com
#  preço, estoque e promoções.
# ═══════════════════════════════════════════════════════════════
def buscar_vtex(link, nome_site):
    """
    Estratégia 1: API catalog_system (retorna preço + promoções)
    Estratégia 2: API sp/product (fallback)
    Estratégia 3: JSON-LD no HTML (último recurso)
    """

    # Extrai o domínio base (ex: www.paguemenos.com.br)
    partes = link.split("/")
    base   = "/".join(partes[:3])  # https://www.paguemenos.com.br

    # Extrai o slug do produto da URL
    # URLs VTEX geralmente terminam em /p ou /p-XXXXXX
    slug = None
    for parte in reversed(partes):
        if parte and parte not in ("p",) and not parte.startswith("p-"):
            slug = parte
            break
        elif parte.startswith("p-"):
            # slug está na parte anterior
            idx = partes.index(parte)
            if idx > 0:
                slug = partes[idx - 1]
            break

    # ── Estratégia 1: API VTEX catalog_system ──
    if slug:
        api_url = f"{base}/api/catalog_system/pub/products/search/{slug}"
        try:
            r = httpx.get(api_url, headers=HEADERS_JSON, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                produtos = r.json()
                if produtos and isinstance(produtos, list):
                    prod = produtos[0]
                    resultado = _extrair_vtex_produto(prod, link, nome_site)
                    if resultado:
                        print(f"  [{nome_site}] API VTEX funcionou")
                        return resultado
        except Exception as e:
            print(f"  [{nome_site}] API catalog_system falhou: {e}")

    # ── Estratégia 2: API VTEX por EAN/slug alternativo ──
    if slug:
        api_url2 = f"{base}/api/catalog_system/pub/products/search?fq=skuId:{slug}"
        try:
            r = httpx.get(api_url2, headers=HEADERS_JSON, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                produtos = r.json()
                if produtos and isinstance(produtos, list):
                    prod = produtos[0]
                    resultado = _extrair_vtex_produto(prod, link, nome_site)
                    if resultado:
                        print(f"  [{nome_site}] API VTEX (alt) funcionou")
                        return resultado
        except Exception as e:
            print(f"  [{nome_site}] API alt falhou: {e}")

    # ── Estratégia 3: HTML + JSON-LD (fallback) ──
    print(f"  [{nome_site}] Tentando HTML + JSON-LD...")
    try:
        r = httpx.get(link, headers=HEADERS_BROWSER, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        # JSON-LD é injetado no servidor — não precisa de JS
        preco = None
        nome  = None
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                dados = json.loads(tag.string or "")
                if isinstance(dados, list):
                    dados = dados[0]
                if dados.get("@type") not in ("Product", "IndividualProduct"):
                    continue
                nome = dados.get("name", "")
                offers = dados.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                p = offers.get("price") or offers.get("lowPrice")
                if p:
                    preco = float(p)
                    break
            except Exception:
                continue

        if not preco:
            print(f"  [{nome_site}] JSON-LD não encontrado, tentando seletores...")
            # Tenta seletores VTEX genéricos
            for sel in [
                ".vtex-product-price-1-x-sellingPrice .vtex-product-price-1-x-currencyContainer",
                ".vtex-product-price-1-x-sellingPrice",
                "[class*='sellingPrice']",
                "[class*='best-price']",
                "[class*='bestPrice']",
            ]:
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text(strip=True)
                    # Só aceita se contém padrão de moeda
                    if re.search(r"\d+[,\.]\d{2}", txt):
                        preco = limpar_preco(txt)
                        if preco and 1 < preco < 100000:
                            break
                        preco = None

        if not preco:
            print(f"  [{nome_site}] Preço não encontrado")
            return None

        if not nome:
            nome_el = soup.select_one("h1")
            nome = nome_el.get_text(strip=True) if nome_el else "Produto"

        # Promoção no texto da página
        promocao = detectar_promocao(soup.get_text(" "))

        return {
            "site": nome_site,
            "nome": nome[:120],
            "preco": preco,
            "preco_original": None,
            "promocao": promocao,
            "link": link,
        }

    except Exception as e:
        print(f"  [{nome_site}] HTML falhou: {e}")
        return None


def _extrair_vtex_produto(prod, link, nome_site):
    """Extrai preço e promoção do JSON retornado pela API VTEX"""
    nome = prod.get("productName", "Produto")

    # Pega o primeiro SKU disponível
    items = prod.get("items", [])
    if not items:
        return None

    sku    = items[0]
    offers = sku.get("sellers", [{}])[0].get("commertialOffer", {})

    preco          = offers.get("Price")
    preco_original = offers.get("ListPrice")

    if not preco or preco == 0:
        return None

    # Sanidade: ignora preços obviamente errados (CEP, código, etc.)
    if preco > 99999 or preco < 0.5:
        return None

    promocao = None

    # DE/POR
    if preco_original and preco_original > preco:
        desconto = round((1 - preco / preco_original) * 100)
        if 1 <= desconto <= 99:
            promocao = f"DE/POR — {desconto}% off"

    # Teasers = promoções especiais (ex: "Leve 3 pague 2", brinde, etc.)
    teasers = offers.get("Teasers", [])
    for t in teasers:
        nome_teaser = t.get("name", "") or t.get("<Name>k__BackingField", "")
        if nome_teaser:
            promo_detectada = detectar_promocao(nome_teaser)
            promocao = promo_detectada or nome_teaser[:80]
            break

    # Se ainda não tem promoção, tenta detectar no nome do produto
    if not promocao:
        promocao = detectar_promocao(nome)

    return {
        "site": nome_site,
        "nome": nome[:120],
        "preco": float(preco),
        "preco_original": float(preco_original) if preco_original else None,
        "promocao": promocao,
        "link": link,
    }


# ═══════════════════════════════════════════════════════════════
#  MERCADO LIVRE — API oficial (gratuita, sem chave)
# ═══════════════════════════════════════════════════════════════
def buscar_mercadolivre(nome, ean):
    query = ean if ean else nome
    url   = f"https://api.mercadolibre.com/sites/MLB/search?q={query}&limit=5"
    try:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"  [ML] Erro: {e}")
        return []

    resultados = []
    for item in r.json().get("results", []):
        preco          = item.get("price", 0)
        preco_original = item.get("original_price")
        promocao       = None

        if preco_original and preco_original > preco:
            desconto = round((1 - preco / preco_original) * 100)
            if 1 <= desconto <= 99:
                promocao = f"DE/POR — {desconto}% off"

        for tag in item.get("promotions", []):
            label = tag.get("name", "")
            if label:
                promocao = label

        if not promocao:
            promocao = detectar_promocao(item.get("title", ""))

        resultados.append({
            "site": "Mercado Livre",
            "nome": item["title"],
            "preco": preco,
            "preco_original": preco_original,
            "promocao": promocao,
            "link": item["permalink"],
        })
    return resultados


# ═══════════════════════════════════════════════════════════════
#  AMAZON — usa ScraperAPI para contornar bloqueio no Actions
# ═══════════════════════════════════════════════════════════════
def buscar_amazon(link):
    link = link.replace("https://amazon.com.br", "https://www.amazon.com.br")

    # Monta URL via ScraperAPI se configurado
    fetch_url = link
    if SCRAPERAPI_KEY and SCRAPERAPI_KEY != "SUA_CHAVE_AQUI":
        fetch_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={link}&country_code=br"

    try:
        r    = httpx.get(fetch_url, headers=HEADERS_BROWSER, timeout=30, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Amazon] Erro: {e}")
        return None

    if "captcha" in r.text.lower() or "robot check" in r.text.lower():
        print(f"  [Amazon] Bloqueado por CAPTCHA")
        return None

    nome_el = soup.find(id="productTitle")
    nome    = nome_el.text.strip() if nome_el else "Produto Amazon"

    preco = None
    for sel in [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".a-price.aok-align-center .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            v = limpar_preco(el.get_text())
            if v and 1 < v < 100000:
                preco = v
                break

    # Fallback JSON-LD
    if not preco:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(tag.string or "")
                if isinstance(d, list):
                    d = d[0]
                o = d.get("offers", {})
                if isinstance(o, list):
                    o = o[0]
                p = o.get("price")
                if p:
                    v = float(p)
                    if 1 < v < 100000:
                        preco = v
                        break
            except Exception:
                continue

    if not preco:
        print(f"  [Amazon] Preço não encontrado")
        return None

    preco_original = None
    for sel in [".a-text-price .a-offscreen", "#priceblock_listprice"]:
        el = soup.select_one(sel)
        if el:
            v = limpar_preco(el.get_text())
            if v and v > preco:
                preco_original = v
                break

    promocao = None
    if preco_original:
        desconto = round((1 - preco / preco_original) * 100)
        if 1 <= desconto <= 99:
            promocao = f"DE/POR — {desconto}% off"

    if not promocao:
        badge = soup.select_one("#dealBadgeSupportingText, .a-badge-label")
        if badge:
            txt = badge.get_text(strip=True)
            if txt:
                promocao = txt

    if not promocao:
        promocao = detectar_promocao(soup.get_text())

    return {
        "site": "Amazon Brasil",
        "nome": nome,
        "preco": preco,
        "preco_original": preco_original,
        "promocao": promocao,
        "link": link,
    }


# ═══════════════════════════════════════════════════════════════
#  ROTEADOR
# ═══════════════════════════════════════════════════════════════
def buscar_por_link(link):
    d = link.lower()
    if "amazon"     in d:
        return buscar_amazon(link)
    elif "paguemenos" in d:
        return buscar_vtex(link, "Pague Menos")
    elif "panvel"   in d:
        return buscar_vtex(link, "Panvel")
    else:
        return buscar_vtex(link, link.split("/")[2].replace("www.", ""))


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

        # Mercado Livre
        resultados_ml = buscar_mercadolivre(produto["nome"], produto.get("ean", ""))
        for r in resultados_ml:
            novos.append({**r, "produto_buscado": produto["nome"], "data": hoje})
        print(f"  [ML] {len(resultados_ml)} resultado(s)")

        # Links específicos
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

    # Mantém 90 dias de histórico
    corte     = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    historico = [h for h in historico if h["data"] >= corte]
    historico.extend(novos)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*55}")
    print(f"Concluido! {len(novos)} precos coletados e salvos.")


if __name__ == "__main__":
    main()
