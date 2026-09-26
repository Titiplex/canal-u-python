from modules import crawling
from modules import db_manager as dbm
from modules import parsing
from modules.visual import loadingbar as lb


def main() -> None:
    count: int = crawling.get_results_count()

    base_url = "https://www.canal-u.tv/recherche?search_api_fulltext=&op=Submit&page="

    db = dbm.SQLManager()

    print("Starting search pages crawling...")

    first_bar: lb.LoadingBar = lb.LoadingBar(count)
    first_bar.print()

    for i in range(count + 1):
        current_url = base_url + str(i)
        if not db.is_visited(current_url):
            try:
                res = parsing.get_search_items(crawling.crawl(current_url))  # TODO add error catch
                db.save_to_queue(res)
                db.add_visited(current_url)
                db.commit()
            except:
                pass
        first_bar.increment()
        first_bar.print()

    # saving queue_js with page urls to lookup

    print("Finished searching pages crawling")
    print("Starting individual pages crawling...")

    url_count: int = db.get_queue_size()
    queue_data = db.get_queue()

    second_bar: lb.LoadingBar = lb.LoadingBar(url_count)
    second_bar.print()

    # get data of individual pages
    for url, type_ in queue_data:
        html = crawling.crawl(url)
        if type_ == "dossier":
            extracted_links = parsing.parse_collection(html)
            if extracted_links[0]:
                db.save_to_queue(extracted_links)
            # TODO deal with errors
        else:
            metadata = parsing.parse_page(html)
            if metadata[0]:
                db.create_audio(metadata["url"], metadata["audios"][0], metadata["title"], metadata["desc"],
                                metadata["langues"][0], metadata["citation"], metadata["cdt"])
        db.commit()
        db.remove_from_queue(url)
        second_bar.increment()
        second_bar.print()

    db.close()


if __name__ == "__main__":
    main()
