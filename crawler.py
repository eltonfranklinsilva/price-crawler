import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# Headers que simulam um navegador real
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─────────────────────────────────────────────
# Utilitário: limpa string de preço → float
# Ex: "R$ 89,90" → 89.90
# ─────────────────────────────────────────────
def limpar_preco(texto):
    if not texto:
        return None
    texto = texto.replace("R$", "").replace("\xa0", "").strip()
    # Remove pontos de milhar e troca vírgula decimal por ponto
    texto = re.sub(r"\.(?=\d{3})", "", texto)
    texto = texto.replace(",", ".")
    numeros = re.sub(r"[^\d.]", "", texto)
    try:
        return float(numeros)
    except ValueError:
        return None


# ─────────────────────────────────────────────
# Utilitário: tenta extrair preço do JSON-LD
# (metadados que os sites inserem para o Google)
# ─────────────────────────────────────────────
def preco_via_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(tag.string or "")
            # Pode ser um objeto ou uma lista
            if isinstance(dados, list):
                dados = dados[0]
            offers = dados.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            preco = offers.get("price") or offers.get("lowPrice")
            if preco:
                return float(preco)
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────
# MERCADO LIVRE — API pública oficial
# Busca por EAN (mais preciso) ou nome
# ─────────────────────────────────────────────
def buscar_mercadolivre(nome, ean):
    query = ean if ean else nome
    url = f"https://api.mercadolibre.com/sites/MLB/search?q={query}&limit=5"
    try:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"  [ML] Erro na requisição: {e}")
        return []

    resultados = []
    for item in r.json().get("results", []):
        preco = item.get("price", 0)
        preco_original = item.get("original_price")
        promocao = None

        if preco_original and preco_original > preco:
            desconto = round((1 - preco / preco_original) * 100)
            promocao = f"DE/POR — {desconto}% off"

        # Detecta promoções especiais do ML (ex: "Leve 3, Pague 2")
        for tag in item.get("promotions", []):
            tipo = tag.get("type", "")
            if "bundle" in tipo.lower() or "combo" in tipo.lower():
                promocao = f"Promoção especial: {tag.get('name', tipo)}"

        resultados.append({
            "site": "Mercado Livre",
            "nome": item["title"],
            "preco": preco,
            "preco_original": preco_original,
            "promocao": promocao,
            "link": item["permalink"],
        })
    return resultados


# ─────────────────────────────────────────────
# AMAZON BRASIL
# Estratégia: seletores específicos + fallback JSON-LD
# ─────────────────────────────────────────────
def buscar_amazon(link):
    # Garante que o link tem www
    link = link.replace("https://amazon.com.br", "https://www.amazon.com.br")
    try:
        r = httpx.get(link, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Amazon] Erro: {e}")
        return None

    # Nome do produto
    nome_el = soup.find(id="productTitle")
    nome = nome_el.text.strip() if nome_el else "Produto Amazon"

    # Preço principal — tenta seletores em ordem de prioridade
    preco = None
    seletores_preco = [
        ".a-price.aok-align-center .a-offscreen",  # preço com desconto
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
    ]
    for sel in seletores_preco:
        el = soup.select_one(sel)
        if el:
            preco = limpar_preco(el.get_text())
            if preco:
                break

    # Fallback: JSON-LD
    if not preco:
        preco = preco_via_jsonld(soup)

    if not preco:
        print(f"  [Amazon] Preço não encontrado em {link}")
        return None

    # Preço original (DE/POR)
    preco_original = None
    orig_el = soup.select_one(".a-text-price .a-offscreen")
    if orig_el:
        preco_original = limpar_preco(orig_el.get_text())

    promocao = None
    if preco_original and preco_original > preco:
        desconto = round((1 - preco / preco_original) * 100)
        promocao = f"DE/POR — {desconto}% off"

    # Badge de oferta (ex: "Oferta do Dia")
    badge = soup.select_one("#dealBadgeSupportingText, .a-badge-label")
    if badge and not promocao:
        promocao = badge.get_text(strip=True)

    return {
        "site": "Amazon Brasil",
        "nome": nome,
        "preco": preco,
        "preco_original": preco_original,
        "promocao": promocao,
        "link": link,
    }


# ─────────────────────────────────────────────
# PAGUE MENOS
# Estrutura: preço em .product-price__value
# Promoção em .product-price__discount
# ─────────────────────────────────────────────
def buscar_paguemenos(link):
    try:
        r = httpx.get(link, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Pague Menos] Erro: {e}")
        return None

    # Nome
    nome_el = soup.select_one("h1.product-name, h1.pdp-title, h1")
    nome = nome_el.get_text(strip=True) if nome_el else "Produto Pague Menos"

    # Preço com desconto (preço final que o cliente paga)
    preco = None
    seletores = [
        ".product-price__value--best-price",
        ".product-price__value",
        "[class*='best-price']",
        "[class*='selling-price']",
    ]
    for sel in seletores:
        el = soup.select_one(sel)
        if el:
            preco = limpar_preco(el.get_text())
            if preco:
                break

    # Fallback JSON-LD
    if not preco:
        preco = preco_via_jsonld(soup)

    if not preco:
        print(f"  [Pague Menos] Preço não encontrado em {link}")
        return None

    # Preço original (riscado)
    preco_original = None
    orig_el = soup.select_one(
        ".product-price__value--list-price, "
        "[class*='list-price'], "
        ".product-price__value--old"
    )
    if orig_el:
        preco_original = limpar_preco(orig_el.get_text())

    # Texto de promoção explícito (ex: "30% OFF na 2ª unidade")
    promocao = None
    promo_el = soup.select_one(
        ".product-price__discount, "
        "[class*='discount-badge'], "
        "[class*='promo-tag'], "
        ".badge--discount"
    )
    if promo_el:
        promocao = promo_el.get_text(strip=True)
    elif preco_original and preco_original > preco:
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


# ─────────────────────────────────────────────
# PANVEL
# Estrutura: preço em .product__price ou JSON-LD
# Promoção em .product__discount-badge
# ─────────────────────────────────────────────
def buscar_panvel(link):
    try:
        r = httpx.get(link, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Panvel] Erro: {e}")
        return None

    # Nome
    nome_el = soup.select_one("h1.product__name, h1.product-name, h1")
    nome = nome_el.get_text(strip=True) if nome_el else "Produto Panvel"

    # Preço — a Panvel frequentemente usa JSON-LD como fonte primária
    preco = preco_via_jsonld(soup)

    # Fallback: seletores HTML
    if not preco:
        seletores = [
            ".product__best-price",
            ".product__price--best",
            "[class*='best-price']",
            ".product__price",
            "[class*='selling-price']",
        ]
        for sel in seletores:
            el = soup.select_one(sel)
            if el:
                preco = limpar_preco(el.get_text())
                if preco:
                    break

    if not preco:
        print(f"  [Panvel] Preço não encontrado em {link}")
        return None

    # Preço original
    preco_original = None
    orig_el = soup.select_one(
        ".product__list-price, "
        "[class*='list-price'], "
        ".product__price--from"
    )
    if orig_el:
        preco_original = limpar_preco(orig_el.get_text())

    # Promoção
    promocao = None
    promo_el = soup.select_one(
        ".product__discount, "
        "[class*='discount-tag'], "
        "[class*='promo-label'], "
        ".badge-promo"
    )
    if promo_el:
        promocao = promo_el.get_text(strip=True)
    elif preco_original and preco_original > preco:
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


# ─────────────────────────────────────────────
# GENÉRICO — fallback para qualquer outro site
# Usa JSON-LD primeiro, depois regex no HTML
# ─────────────────────────────────────────────
def buscar_generico(link):
    try:
        r = httpx.get(link, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [Genérico] Erro em {link}: {e}")
        return None

    titulo = soup.find("title")
    nome = titulo.get_text(strip=True)[:80] if titulo else link

    # Tenta JSON-LD primeiro
    preco = preco_via_jsonld(soup)

    # Fallback: regex no texto da página
    if not preco:
        texto = soup.get_text()
        precos_encontrados = re.findall(r"R\$\s*[\d\.]+,\d{2}", texto)
        if precos_encontrados:
            preco = limpar_preco(precos_encontrados[0])

    if not preco:
        print(f"  [Genérico] Preço não encontrado em {link}")
        return None

    return {
        "site": link.split("/")[2].replace("www.", ""),
        "nome": nome,
        "preco": preco,
        "preco_original": None,
        "promocao": None,
        "link": link,
    }


# ─────────────────────────────────────────────
# Roteador: decide qual função usar pelo domínio
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# MAIN — orquestra tudo
# ─────────────────────────────────────────────
def main():
    with open("products.json", encoding="utf-8") as f:
        produtos = json.load(f)

    try:
        with open("prices.json", encoding="utf-8") as f:
            historico = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        historico = []

    hoje = datetime.date.today().isoformat()
    novos = []

    for produto in produtos:
        print(f"\n{'─'*50}")
        print(f"Buscando: {produto['nome']}")

        # 1. Mercado Livre via API
        resultados_ml = buscar_mercadolivre(produto["nome"], produto.get("ean", ""))
        for r in resultados_ml:
            novos.append({**r, "produto_buscado": produto["nome"], "data": hoje})
        print(f"  [ML] {len(resultados_ml)} resultado(s) encontrado(s)")

        # 2. Links específicos por site
        for link in produto.get("links", []):
            time.sleep(2)  # pausa educada para não ser bloqueado
            resultado = buscar_por_link(link)
            if resultado:
                novos.append({**resultado, "produto_buscado": produto["nome"], "data": hoje})
                print(f"  [OK] {resultado['site']} — R$ {resultado['preco']:.2f}"
                      + (f" | {resultado['promocao']}" if resultado['promocao'] else ""))

    # Remove entradas com mais de 90 dias e adiciona as novas
    corte = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    historico = [h for h in historico if h["data"] >= corte]
    historico.extend(novos)

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*50}")
    print(f"Concluído! {len(novos)} preços coletados e salvos em prices.json")


if __name__ == "__main__":
    main()
