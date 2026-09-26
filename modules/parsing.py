from urllib.parse import urljoin

from bs4 import BeautifulSoup


def get_search_items(html: str, url: str = "https://www.canal-u.tv") -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('.search-results') or soup.select_one('main') or soup
    children = {}
    for child in root.select('.wrapper-content'):
        link = child.select_one('h3 a[href]')
        if link is None or not link['href'].strip():
            continue
        card = child.find_parent('article')
        bundle = card.select_one('.wrapper-bundle') if card else None
        production = card.select_one('.field--name-field-type-production') if card else None
        type_ = (bundle or production)
        label = type_.get_text(' ', strip=True).lower() if type_ else ''
        children[urljoin(url, link['href'])] = label
    return children


BASE = "https://www.canal-u.tv"


def _dans_carte(element):
    """Ignore les médias et champs appartenant à une page listée."""
    return element.find_parent("article", class_="node--view-mode-teaser") is not None


def _texte(root, selecteur):
    for element in root.select(selecteur):
        if not _dans_carte(element):
            return element.get_text(" ", strip=True)
    return ""


def _infos(root, url):
    langues = []
    for element in root.select(".wrapper-langues .wrapper-langue"):
        if not _dans_carte(element):
            langue = element.get_text(" ", strip=True)
            if langue and langue not in langues:
                langues.append(langue)

    audios = []
    for source in root.select("source[src][type]"):
        if _dans_carte(source):
            continue
        type_audio = source["type"].split(";", 1)[0].strip().lower()
        if type_audio in {"audio/mp3", "audio/mpeg"}:
            if not source["src"].strip():
                continue
            lien = urljoin(url, source["src"])
            if lien not in audios:
                audios.append(lien)

    return {
        "url": url,
        "titre": _texte(root, "h1"),
        "desc": _texte(root, ".field--name-field-description"),
        "audios": audios,
        "lieu": _texte(root, ".field--name-field-lieu .field__item"),
        "langues": langues,
        "cdt": _texte(root, ".field-condition-utilisation .field__item"),
        "citation": _texte(root, ".field-citation-ressource .field__item"),
        "doi": _texte(root, ".id-doi-datacite .field__items"),
    }


def _racine(html):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("article.node--view-mode-full")
    if root is None or soup.select_one("h1") is None:
        raise ValueError("Page Canal-U non reconnue ou incomplète ; elle reste à reprendre")
    return root


def parse_page(html: str, url: str = BASE):
    """Retourne (audio_trouve, infos)."""
    infos = _infos(_racine(html), url)
    return bool(infos["audios"]), infos


def parse_collection(html: str, url: str = BASE):
    """Retourne (contenu_trouve, infos_avec_pages).

    Fonctionne si la collection contient un audio intégré, des pages listées,
    ou les deux. Les audios des cartes ne sont pas attribués à la collection.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("article.node--view-mode-full")
    if root is None or soup.select_one("h1") is None:
        raise ValueError("Page Canal-U non reconnue ou incomplète ; elle reste à reprendre")
    infos = _infos(root, url)
    pages = {}
    deja_vus = set()

    selecteur = (
        "article.node--view-mode-teaser.node--type-page-media, "
        "article.node--view-mode-teaser.node--type-collection, "
        "article.node--view-mode-teaser.node--type-dossier"
    )
    cartes = root.select(selecteur)
    for section in soup.select("#videos"):
        cartes.extend(section.select(selecteur))

    for carte in cartes:
        lien = carte.select_one(".wrapper-content h3 a[href]")
        if lien is None or not lien["href"].strip():
            continue
        adresse = urljoin(url, lien["href"])
        if adresse == url or adresse in deja_vus:
            continue
        deja_vus.add(adresse)
        type_ = carte.select_one(".field--name-field-type-production")
        pages[adresse] = type_.get_text(" ", strip=True) if type_ else ""

    infos["pages"] = pages
    return bool(infos["audios"] or pages), infos


def parse_folder(html: str, url: str = BASE):
    """Même traitement pour les dossiers Canal-U."""
    return parse_collection(html, url)
