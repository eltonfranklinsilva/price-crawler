import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════
# Amazon bloqueia IPs de nuvem com CAPTCHA — desabilitada por padrão.
# Se quiser tentar, coloque sua chave do scraperapi.com abaixo.
SCRAPERAPI_KEY = "3a4f98804a2b98772342d286824afcd2"

HEADERS = {
    "User-Agent": "price-crawler/1.0 (monitoramento de precos)",
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


# ───────────────────────────────────────────────────────────────
# Converte "R$ 1.299,90" → 1299.90
# ───────────────────────────────────────────────────────────────
def limpar_preco(v):
    if v is None:
        return None
    s = str(v).replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
    s = re.sub(r"\.(?=\d{3}[,.])", "", s)   # remove ponto de milhar
    s = s.replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        f = float(s)
        return f if 0.5 < f < 100_000 else None   # sanidade
    except ValueError:
        return None


# ───────────────────────────────────────────────────────────────
# Detecta promoções especiais no texto
# ───────────────────────────────────────────────────────────────
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
#  MERCADO LIVRE
#  CORREÇÃO: busca por nome (não EAN) — EAN causa 403
#  A API pública exige User-Agent de aplicação, não de browser
# ═══════════════════════════════════════════════════════════════
def buscar_mercadolivre(nome, ean):
    # Busca por nome — mais resultados e sem o 403 que EAN causava
    r = None
    for query in [nome, ean] if ean else [nome]:
        try:
            r = httpx.get(
                "https://api.mercadolibre.com/sites/MLB/search",
                params={"q": query, "limit": 5},
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code == 200:
                break
            print(f"  [ML] status {r.status_code} para query '{query}', tentando próxima...")
        except Exception as e:
            print(f"  [ML] Erro: {e}")
            return []

    if not r or r.status_code != 200:
        print(f"  [ML] Falhou após todas as tentativas")
        return []

    resultados = []
    for item in r.json().get("results", []):
        preco          = item.get("price", 0)
        preco_original = item.get("original_price")
        promocao       = None

        if preco_original and preco_original > preco:
            d = round((1 - preco / preco_original) * 100)
            if 1 <= d <= 99:
                promocao = f"DE/POR — {d}% off"

        for tag in item.get("promotions", []):
            label = tag.get("name", "")
            if label:
                promocao = label

        if not promocao:
            promocao = detectar_promocao(item.get("title", ""))

        resultados.append({
            "site":           "Mercado Livre",
            "nome":           item["title"],
            "preco":          preco,
            "preco_original": preco_original,
            "promocao":       promocao,
            "link":           item["permalink"],
        })
    return resultados


# ═══════════════════════════════════════════════════════════════
#  VTEX API — Pague Menos e Panvel
#
#  CORREÇÃO Pague Menos: "70% off" vinha do texto da página
#  (era a tag de desconto do site, não o desconto real do produto).
#  Agora usa APENAS os Teasers da API VTEX ou DE/POR calculado.
#
#  CORREÇÃO Panvel: extraia o ID numérico do final da URL (p-485500)
#  e chama /api/catalog_system/pub/products/search?fq=productId:485500
# ═══════════════════════════════════════════════════════════════
def buscar_vtex(link, nome_site):
    partes = link.rstrip("/").split("/")
    base   = "/".join(partes[:3])
    ultimo = partes[-1]   # ex: "p-485500" ou "aptanutri-3-premium-800g"

    # Extrai ID numérico se a URL termina em p-XXXXX (padrão Panvel)
    product_id = None
    m = re.search(r"p-(\d+)$", ultimo)
    if m:
        product_id = m.group(1)

    # Slug de texto (padrão Pague Menos: termina em /p)
    slug = None
    if ultimo == "p" and len(partes) >= 2:
        slug = partes[-2]   # a parte antes do /p
    elif ultimo != "p" and not product_id:
        slug = ultimo

    # ── Tentativa 1: por productId (Panvel) ──
    if product_id:
        resultado = _vtex_api(
            base,
            f"/api/catalog_system/pub/products/search?fq=productId:{product_id}",
            link, nome_site, "productId"
        )
        if resultado:
            return resultado

    # ── Tentativa 2: por slug de texto (Pague Menos) ──
    if slug:
        resultado = _vtex_api(
            base,
            f"/api/catalog_system/pub/products/search/{slug}",
            link, nome_site, "slug"
        )
        if resultado:
            return resultado

    # ── Tentativa 3: HTML + JSON-LD (fallback) ──
    print(f"  [{nome_site}] API falhou, tentando HTML...")
    return _vtex_html(link, nome_site)


def _vtex_api(base, path, link, nome_site, modo):
    """Chama a API interna do VTEX e extrai preço + promoções dos Teasers."""
    try:
        r = httpx.get(
            base + path,
            headers=HEADERS,
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code != 200 or not r.text.strip():
            return None
        produtos = r.json()
        if not isinstance(produtos, list) or not produtos:
            return None
    except Exception as e:
        print(f"  [{nome_site}] API ({modo}) erro: {e}")
        return None

    prod  = produtos[0]
    nome  = prod.get("productName", "Produto")
    items = prod.get("items", [])
    if not items:
        return None

    offer = items[0].get("sellers", [{}])[0].get("commertialOffer", {})
    preco          = limpar_preco(offer.get("Price"))
    preco_original = limpar_preco(offer.get("ListPrice"))

    if not preco:
        return None

    # Ignora preco_original igual ao preco (sem desconto real)
    if preco_original and abs(preco_original - preco) < 0.01:
        preco_original = None

    # Promoção via Teasers (promoções configuradas no painel VTEX)
    # Ex: "Leve 2 pague 1", "30% off na 2ª unidade", brinde, etc.
    promocao = None
    for teaser in offer.get("Teasers", []):
        # Teasers podem vir em formatos diferentes dependendo da versão VTEX
        nome_t = (
            teaser.get("name") or
            teaser.get("<Name>k__BackingField") or
            teaser.get("Name") or ""
        )
        if nome_t:
            promo_detectada = detectar_promocao(nome_t)
            promocao = promo_detectada or nome_t[:80]
            break

    # Fallback DE/POR calculado (só aceita se desconto entre 1% e 99%)
    if not promocao and preco_original and preco_original > preco:
        d = round((1 - preco / preco_original) * 100)
        if 1 <= d <= 99:
            promocao = f"DE/POR — {d}% off"

    print(f"  [{nome_site}] API VTEX ({modo}) OK")
    return {
        "site":           nome_site,
        "nome":           nome[:120],
        "preco":          preco,
        "preco_original": preco_original,
        "promocao":       promocao,
        "link":           link,
    }


def _vtex_html(link, nome_site):
    """Fallback: lê o HTML e extrai preço via JSON-LD injetado no servidor."""
    try:
        r    = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [{nome_site}] HTML erro: {e}")
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
        print(f"  [{nome_site}] JSON-LD não encontrado")
        return None

    if not nome:
        h1 = soup.select_one("h1")
        nome = h1.get_text(strip=True) if h1 else "Produto"

    return {
        "site":           nome_site,
        "nome":           nome[:120],
        "preco":          preco,
        "preco_original": None,
        "promocao":       detectar_promocao(soup.get_text(" ")),
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  AMAZON
#  Bloqueada por CAPTCHA no GitHub Actions sem ScraperAPI.
#  Mantida no código — funciona se ScraperAPI estiver configurado.
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
            promocao = badge.get_text(strip=True) or None

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
    if "amazon"      in d:
        return buscar_amazon(link)
    elif "paguemenos" in d:
        return buscar_vtex(link, "Pague Menos")
    elif "panvel"    in d:
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
