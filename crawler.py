import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
#  ScraperAPI resolve bloqueios de IP na Amazon, Panvel etc.
#  Cadastro gratuito em: https://www.scraperapi.com
#  Plano Free: 5.000 requisições/mês (suficiente para uso diário)
# ═══════════════════════════════════════════════════════════════
SCRAPERAPI_KEY = "SUA_CHAVE_AQUI"   # ← cole sua chave aqui depois de cadastrar

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def limpar_preco(texto):
    if not texto:
        return None
    texto = str(texto).replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
    texto = re.sub(r"\.(?=\d{3}[,\.])", "", texto)
    texto = texto.replace(",", ".")
    numeros = re.sub(r"[^\d.]", "", texto)
    try:
        v = float(numeros)
        return v if v > 0 else None
    except ValueError:
        return None


def preco_via_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(tag.string or "")
            if isinstance(dados, list):
                dados = dados[0]
            offers = dados.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            preco = offers.get("price") or offers.get("lowPrice")
            nome  = dados.get("name", "")
            if preco:
                return float(preco), nome
        except Exception:
            continue
    return None, None


def get_url(url, render_js=False):
    if SCRAPERAPI_KEY and SCRAPERAPI_KEY != "SUA_CHAVE_AQUI":
        params = f"api_key={SCRAPERAPI_KEY}&url={url}"
        if render_js:
            params += "&render=true"
        return f"https://api.scraperapi.com/?{params}"
    return url


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

def detectar_promocao_no_texto(texto):
    texto_lower = texto.lower()
    for padrao, formatar in PADROES_PROMO:
        m = re.search(padrao, texto_lower)
        if m:
            try:
                return formatar(m)
            except Exception:
                continue
    return None


def buscar_mercadolivre(nome, ean):
    query = ean if ean else nome
    url = f"https://api.mercadolibre.com/sites/MLB/search?q={query}&limit=5"
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
            promocao = f"DE/POR — {desconto}% off"

        for tag in item.get("promotions", []):
            label = tag.get("name", "")
            if label:
                promocao = label

        if not promocao:
            promocao = detectar_promocao_no_texto(item.get("title", ""))

        resultados.append({
            "site": "Mercado Livre",
            "nome": item["title"],
            "preco": preco,
            "preco_original": preco_original,
            "promocao": promocao,
            "link": item["permalink"],
        })
    return resultados


def buscar_amazon(link):
    link = link.replace("https://amazon.com.br", "https://www.amazon.com.br")
    try:
        r = httpx.get(get_url(link), headers=HEADERS, timeout=30, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Amazon] Erro: {e}")
        return None

    if "captcha" in r.text.lower() or "robot" in r.text.lower():
        print(f"  [Amazon] Bloqueado por CAPTCHA — configure o ScraperAPI")
        return None

    nome_el = soup.find(id="productTitle")
    nome = nome_el.text.strip() if nome_el else "Produto Amazon"

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
            preco = limpar_preco(el.get_text())
            if preco:
                break

    if not preco:
        preco, _ = preco_via_jsonld(soup)

    if not preco:
        print(f"  [Amazon] Preço não encontrado")
        return None

    preco_original = None
    for sel in [".a-text-price .a-offscreen", "#priceblock_listprice"]:
        el = soup.select_one(sel)
        if el:
            preco_original = limpar_preco(el.get_text())
            if preco_original:
                break

    promocao = None
    if preco_original and preco_original > preco:
        desconto = round((1 - preco / preco_original) * 100)
        promocao = f"DE/POR — {desconto}% off"

    badge = soup.select_one("#dealBadgeSupportingText, .a-badge-label, #couponBadgeRegularVpc")
    if badge and not promocao:
        txt = badge.get_text(strip=True)
        if txt:
            promocao = txt

    if not promocao:
        promocao = detectar_promocao_no_texto(soup.get_text())

    return {
        "site": "Amazon Brasil",
        "nome": nome,
        "preco": preco,
        "preco_original": preco_original,
        "promocao": promocao,
        "link": link,
    }


def buscar_paguemenos(link):
    try:
        r = httpx.get(get_url(link), headers=HEADERS, timeout=30, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Pague Menos] Erro: {e}")
        return None

    nome_el = soup.select_one("h1.product-name, h1.vtex-store-components-3-x-productNameContainer, h1")
    nome = nome_el.get_text(strip=True) if nome_el else "Produto Pague Menos"

    preco, nome_jsonld = preco_via_jsonld(soup)
    if nome_jsonld and nome == "Produto Pague Menos":
        nome = nome_jsonld

    if not preco:
        for sel in [
            ".vtex-product-price-1-x-sellingPrice",
            ".vtex-product-price-1-x-sellingPriceValue",
            ".product-price__value--best-price",
            ".product-price__value",
            "[class*='sellingPrice']",
            "[class*='best-price']",
        ]:
            el = soup.select_one(sel)
            if el:
                preco = limpar_preco(el.get_text())
                if preco:
                    break

    if not preco:
        print(f"  [Pague Menos] Preço não encontrado em {link}")
        return None

    preco_original = None
    for sel in [
        ".vtex-product-price-1-x-listPrice",
        ".vtex-product-price-1-x-listPriceValue",
        ".product-price__value--list-price",
        "[class*='listPrice']",
    ]:
        el = soup.select_one(sel)
        if el:
            preco_original = limpar_preco(el.get_text())
            if preco_original:
                break

    promocao = None
    texto_pagina = soup.get_text(" ", strip=True)

    for sel in [
        "[class*='discount']", "[class*='promo']", "[class*='badge']",
        "[class*='tag-promo']", ".teaserContent", "[class*='teaser']",
    ]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt and len(txt) < 80:
                if any(p in txt.lower() for p in ["%", "off", "compre", "leve", "pague", "brinde", "desconto", "grátis"]):
                    promocao = txt
                    break

    if not promocao:
        promocao = detectar_promocao_no_texto(texto_pagina)

    if not promocao and preco_original and preco_original > preco:
        desconto = round((1 - preco / preco_original) * 100)
        promocao = f"DE/POR — {desconto}% off"

    return {
        "site": "Pague Menos",
        "nome": nome,
        "preco": preco,
        "preco_original": preco_original,
        "promocao": promocao,
        "link": link,
    }


def buscar_panvel(link):
    try:
        r = httpx.get(
            get_url(link, render_js=True),
            headers=HEADERS, timeout=45, follow_redirects=True
        )
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Panvel] Erro: {e}")
        return None

    nome_el = soup.select_one("h1.vtex-store-components-3-x-productNameContainer, h1.product__name, h1")
    nome = nome_el.get_text(strip=True) if nome_el else "Produto Panvel"

    preco, nome_jsonld = preco_via_jsonld(soup)
    if nome_jsonld and nome == "Produto Panvel":
        nome = nome_jsonld

    if not preco:
        for sel in [
            ".vtex-product-price-1-x-sellingPrice",
            ".vtex-product-price-1-x-sellingPriceValue",
            ".product__best-price",
            ".product__price--best",
            "[class*='sellingPrice']",
            "[class*='bestPrice']",
            "[class*='best-price']",
        ]:
            el = soup.select_one(sel)
            if el:
                preco = limpar_preco(el.get_text())
                if preco:
                    break

    if not preco:
        print(f"  [Panvel] Preço não encontrado em {link}")
        return None

    preco_original = None
    for sel in [
        ".vtex-product-price-1-x-listPrice",
        ".product__list-price",
        ".product__price--from",
        "[class*='listPrice']",
    ]:
        el = soup.select_one(sel)
        if el:
            preco_original = limpar_preco(el.get_text())
            if preco_original:
                break

    promocao = None
    texto_pagina = soup.get_text(" ", strip=True)

    for sel in [
        ".teaserContent", "[class*='teaser']", "[class*='discount']",
        "[class*='promo']", "[class*='badge']", ".product__discount",
    ]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt and len(txt) < 80:
                if any(p in txt.lower() for p in ["%", "off", "compre", "leve", "pague", "brinde", "desconto", "grátis"]):
                    promocao = txt
                    break

    if not promocao:
        promocao = detectar_promocao_no_texto(texto_pagina)

    if not promocao and preco_original and preco_original > preco:
        desconto = round((1 - preco / preco_original) * 100)
        promocao = f"DE/POR — {desconto}% off"

    return {
        "site": "Panvel",
        "nome": nome,
        "preco": preco,
        "preco_original": preco_original,
        "promocao": promocao,
        "link": link,
    }


def buscar_generico(link):
    try:
        r = httpx.get(get_url(link), headers=HEADERS, timeout=30, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Genérico] Erro em {link}: {e}")
        return None

    titulo = soup.find("title")
    nome = titulo.get_text(strip=True)[:80] if titulo else link

    preco, nome_jsonld = preco_via_jsonld(soup)
    if nome_jsonld:
        nome = nome_jsonld

    if not preco:
        texto = soup.get_text()
        precos_encontrados = re.findall(r"R\$\s*[\d\.]+,\d{2}", texto)
        if precos_encontrados:
            preco = limpar_preco(precos_encontrados[0])

    if not preco:
        print(f"  [Genérico] Preço não encontrado em {link}")
        return None

    promocao = detectar_promocao_no_texto(soup.get_text())

    return {
        "site": link.split("/")[2].replace("www.", ""),
        "nome": nome,
        "preco": preco,
        "preco_original": None,
        "promocao": promocao,
        "link": link,
    }


def buscar_por_link(link):
    dominio = link.lower()
    if "amazon" in dominio:
        return buscar_amazon(link)
    elif "paguemenos" in dominio:
        return buscar_paguemenos(link)
    elif "panvel" in dominio:
        return buscar_panvel(link)
    else:
        return buscar_generico(link)


def main():
    if not SCRAPERAPI_KEY or SCRAPERAPI_KEY == "SUA_CHAVE_AQUI":
        print("AVISO: ScraperAPI nao configurado — Amazon e Panvel podem falhar.")
        print("Cadastre-se em scraperapi.com e coloque sua chave em SCRAPERAPI_KEY\n")

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

        resultados_ml = buscar_mercadolivre(produto["nome"], produto.get("ean", ""))
        for r in resultados_ml:
            novos.append({**r, "produto_buscado": produto["nome"], "data": hoje})
        print(f"  [ML] {len(resultados_ml)} resultado(s)")

        for link in produto.get("links", []):
            time.sleep(2)
            resultado = buscar_por_link(link)
            if resultado:
                novos.append({**resultado, "produto_buscado": produto["nome"], "data": hoje})
                promo_str = f" | {resultado['promocao']}" if resultado['promocao'] else ""
                print(f"  [OK] {resultado['site']} — R$ {resultado['preco']:.2f}{promo_str}")
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
