import re
from urllib.parse import urljoin, urlsplit, urldefrag

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


class IncompletePage(ValueError):
    """Document absent, erreur ou chargement incomplet : reprise autorisée."""


class UnrecognizedPage(ValueError):
    """Document chargé mais structure à examiner : pas de reprise automatique."""


def _dans_carte(element):
    """Ignore les médias et champs appartenant à une page listée."""
    return element.find_parent(class_="node--view-mode-teaser") is not None


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


def _document(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one('h1')
    status_title = soup.select_one('title')
    if (soup.select_one('img[src*="bot_challenge.png"]') or
            any(re.fullmatch(r'(?:403 forbidden|access denied|service unavailable|challenge\.*|just a moment\.*)',
                             tag.get_text(' ', strip=True), re.I)
                for tag in (title, status_title) if tag)):
        raise IncompletePage('Page de refus ou de challenge, contenu non chargé')
    # Une collection peut être un div ou un terme de taxonomie, pas un article.
    root = soup.select_one('.node--view-mode-full') or soup.select_one('.taxonomy-term')
    main = soup.select_one('main, [role="main"]')
    if title is None:
        raise IncompletePage('Titre principal absent ; document incomplet ou inattendu')
    if root is None:
        root = main
    if root is None:
        raise IncompletePage('Contenu principal absent ; document incomplet ou inattendu')
    # Ne pas valider un téléchargement HTML tronqué comme une page vide.
    if soup.find('html') and not re.search(r'</html\s*>', html, re.I):
        raise IncompletePage('Document HTML tronqué')
    return soup, root, title.get_text(' ', strip=True)


def parse_page(html: str, url: str = BASE):
    """Retourne (audio_trouve, infos)."""
    soup, root, titre = _document(html)
    infos = _infos(root, url)
    infos['titre'] = titre
    return bool(infos["audios"]), infos


def parse_collection(html: str, url: str = BASE):
    """Retourne (contenu_trouve, infos_avec_pages).

    Fonctionne si la collection contient un audio intégré, des pages listées,
    ou les deux. Les audios des cartes ne sont pas attribués à la collection.
    """
    soup, root, titre = _document(html)
    infos = _infos(root, url)
    infos['titre'] = titre
    pages = {}
    deja_vus = set()

    selecteur = (
        ".node--view-mode-teaser.node--type-page-media, "
        ".node--view-mode-teaser.node--type-collection, "
        ".node--view-mode-teaser.node--type-dossier"
    )
    is_main = root.name == 'main' or root.get('role') == 'main'
    cartes = [] if is_main else root.select(selecteur)
    sections = soup.select('#videos, #collections, #dossiers')
    # Autre gabarit : repérer la section par son titre, sans prendre les suggestions.
    for heading in root.select('h2'):
        if heading.get_text(' ', strip=True).lower() in {'vidéos', 'videos', 'collections', 'dossiers', 'podcasts'}:
            for sibling in heading.next_siblings:
                if getattr(sibling, 'name', None) == 'h2':
                    break
                if getattr(sibling, 'select', None):
                    sections.append(sibling)
    for section in sections:
        cartes.extend(section.select('.wrapper-content'))
        if 'wrapper-content' in section.get('class', []):
            cartes.append(section)

    def internal(href):
        adresse = urldefrag(urljoin(url, href))[0]
        parts, origin = urlsplit(adresse), urlsplit(url)
        return adresse if parts.scheme in {'http', 'https'} and parts.netloc == origin.netloc else None

    for carte in cartes:
        lien = carte.select_one("h3 a[href], h2 a[href]")
        if lien is None or not lien["href"].strip():
            continue
        adresse = internal(lien['href'])
        if not adresse or adresse == url or adresse in deja_vus:
            continue
        deja_vus.add(adresse)
        card_root = carte.find_parent(class_='node--view-mode-teaser') or carte
        type_ = card_root.select_one(".field--name-field-type-production, .wrapper-bundle")
        pages[adresse] = type_.get_text(" ", strip=True) if type_ else ""

    # Ne suivre que la page suivante de CETTE collection.
    for section in [root, *sections]:
        for link in section.select('.pager__item--next a[href], a[rel~="next"]'):
            adresse = internal(link['href'])
            if adresse and adresse != url and urlsplit(adresse).path == urlsplit(url).path:
                pages[adresse] = 'pagination'

    if not infos['audios'] and not pages:
        if cartes or root.select_one(
                selecteur + ', .wrapper-content h3 a, iframe, [data-drupal-views-infinite-scroll-content-wrapper]'):
            raise UnrecognizedPage('Page chargée, mais cartes ou lecteur non extractibles : à examiner')

    infos["pages"] = pages
    infos['status'] = 'exploitable' if infos['audios'] or pages else 'sans_contenu'
    return bool(infos["audios"] or pages), infos


def parse_folder(html: str, url: str = BASE):
    """Même traitement pour les dossiers Canal-U."""
    return parse_collection(html, url)
