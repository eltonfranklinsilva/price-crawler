import json
import re
import time
import datetime
import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
#  ScraperAPI gratuito em scraperapi.com (5.000 req/mês)
#  Necessário para Amazon. Panvel e Pague Menos funcionam sem ele.
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


# ───────────────────────────────────────────────────────────────
# Converte "R$ 1.299,90" → 1299.90
# ───────────────────────────────────────────────────────────────
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
#  PAGUE MENOS — plataforma VTEX
#  URL termina em /slug-do-produto/p
#  API: /api/catalog_system/pub/products/search/slug
# ═══════════════════════════════════════════════════════════════
def buscar_paguemenos(link):
    partes = link.rstrip("/").split("/")
    base   = f"https://{partes[2]}"

    # Slug é a parte antes do /p final
    slug = partes[-2] if partes[-1] == "p" else partes[-1]

    # Tenta API VTEX pelo slug
    api_url = f"{base}/api/catalog_system/pub/products/search/{slug}"
    resultado = _vtex_api(api_url, link, "Pague Menos")
    if resultado:
        return resultado

    # Fallback: HTML + JSON-LD
    print(f"  [Pague Menos] API falhou, tentando HTML...")
    return _vtex_html(link, "Pague Menos")


# ═══════════════════════════════════════════════════════════════
#  PANVEL — plataforma VTEX
#  URL: /panvel/nome-do-produto/p-485500
#  O número após p- é o productId
#  APIs tentadas em sequência até uma funcionar
# ═══════════════════════════════════════════════════════════════
def buscar_panvel(link):
    partes = link.rstrip("/").split("/")
    base   = f"https://{partes[2]}"   # https://www.panvel.com
    ultimo = partes[-1]               # p-485500

    # Extrai o productId numérico
    m = re.search(r"p-(\d+)$", ultimo)
    product_id = m.group(1) if m else None

    # Extrai o slug de texto (parte antes do p-XXXXX)
    slug = partes[-2] if product_id else ultimo

    apis = []
    if product_id:
        # Variação 1: API padrão VTEX com productId
        apis.append(f"{base}/api/catalog_system/pub/products/search?fq=productId:{product_id}")
        # Variação 2: alguns sites Panvel respondem com o nome da conta no path
        apis.append(f"{base}/panvel/api/catalog_system/pub/products/search?fq=productId:{product_id}")
        # Variação 3: busca por skuId
        apis.append(f"{base}/api/catalog_system/pub/products/search?fq=skuId:{product_id}")
    if slug:
        # Variação 4: busca pelo slug de texto
        apis.append(f"{base}/api/catalog_system/pub/products/search/{slug}")

    for api_url in apis:
        resultado = _vtex_api(api_url, link, "Panvel")
        if resultado:
            return resultado

    # Fallback HTML
    print(f"  [Panvel] Todas as APIs falharam, tentando HTML...")
    return _vtex_html(link, "Panvel")


# ───────────────────────────────────────────────────────────────
# Chama a API VTEX e extrai preço + promoções
# ───────────────────────────────────────────────────────────────
def _vtex_api(api_url, link, nome_site):
    try:
        r = httpx.get(
            api_url,
            headers=HEADERS_JSON,
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
    except Exception:
        return None

    prod  = data[0]
    nome  = prod.get("productName", "Produto")
    items = prod.get("items", [])
    if not items:
        return None

    offer          = items[0].get("sellers", [{}])[0].get("commertialOffer", {})
    preco          = limpar_preco(offer.get("Price"))
    preco_original = limpar_preco(offer.get("ListPrice"))

    if not preco:
        return None

    if preco_original and abs(preco_original - preco) < 0.01:
        preco_original = None

    # Promoções via Teasers VTEX — única fonte confiável
    promocao = None
    for teaser in offer.get("Teasers", []):
        nome_t = (
            teaser.get("name") or
            teaser.get("Name") or
            teaser.get("<n>k__BackingField") or ""
        )
        if nome_t and not re.match(r"^\d+$", nome_t.strip()):
            promocao = detectar_promocao(nome_t) or nome_t[:80]
            break

    if not promocao and preco_original and preco_original > preco:
        d = round((1 - preco / preco_original) * 100)
        if 1 <= d <= 99:
            promocao = f"DE/POR — {d}% off"

    return {
        "site":           nome_site,
        "nome":           nome[:120],
        "preco":          preco,
        "preco_original": preco_original,
        "promocao":       promocao,
        "link":           link,
    }


# ───────────────────────────────────────────────────────────────
# Fallback HTML — lê JSON-LD e __STATE__ sem executar JavaScript
# NÃO usa texto livre da página (evita promoções falsas)
# ───────────────────────────────────────────────────────────────
def _vtex_html(link, nome_site):
    try:
        r    = httpx.get(link, headers=HEADERS_HTML, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [{nome_site}] HTML erro: {e}")
        return None

    preco = None
    nome  = None

    # Tenta JSON-LD primeiro
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

    # Tenta __STATE__ (dados VTEX embutidos no HTML)
    if not preco:
        for s in soup.find_all("script"):
            txt = s.string or ""
            if "__STATE__" not in txt:
                continue
            m = re.search(r'"sellingPrice"\s*:\s*([\d.]+)', txt)
            if m:
                preco = limpar_preco(m.group(1))
            if not nome:
                mn = re.search(r'"productName"\s*:\s*"([^"]+)"', txt)
                if mn:
                    nome = mn.group(1)
            if preco:
                break

    if not preco:
        print(f"  [{nome_site}] Nenhum preço encontrado no HTML")
        return None

    if not nome:
        h1 = soup.select_one("h1")
        nome = h1.get_text(strip=True) if h1 else "Produto"

    # Promoção via Teasers no __STATE__
    promocao = None
    for s in soup.find_all("script"):
        txt = s.string or ""
        if "Teasers" not in txt:
            continue
        m = re.search(r'"Teasers"\s*:\s*\[(.*?)\]', txt, re.DOTALL)
        if m and m.group(1).strip():
            nome_t = re.search(r'"name"\s*:\s*"([^"]+)"', m.group(1))
            if nome_t:
                raw = nome_t.group(1)
                promocao = detectar_promocao(raw) or raw[:80]
        break

    return {
        "site":           nome_site,
        "nome":           nome[:120],
        "preco":          preco,
        "preco_original": None,
        "promocao":       promocao,
        "link":           link,
    }


# ═══════════════════════════════════════════════════════════════
#  AMAZON
#  Requer ScraperAPI para funcionar no GitHub Actions
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
            txt = badge.get_text(strip=True)
            if txt and len(txt) < 60:
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
        # Genérico: tenta VTEX e se não funcionar usa HTML
        nome_site = link.split("/")[2].replace("www.", "")
        return _vtex_html(link, nome_site)


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
