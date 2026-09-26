import requests
from bs4 import BeautifulSoup

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}


def crawl(url: str) -> str:
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def get_results_count() -> int:
    url = "https://www.canal-u.tv/recherche?search_api_fulltext=&op=Submit&page=0"
    html = crawl(url)
    soup = BeautifulSoup(html, "html.parser")

    counter = soup.select_one("#global-search-results-counter")
    if counter is None:
        raise ValueError("Cannot find any results")

    return int(int(counter.get_text(strip=True)) / 12)
