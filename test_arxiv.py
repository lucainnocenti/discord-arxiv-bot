import arxiv

def test_arxiv():
    client = arxiv.Client()

    # Create a simple search to fetch up to 10 results from quant-ph
    search = arxiv.Search(
        query="cat:quant-ph",
        max_results=10,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    print("Fetching up to 10 results from arXiv's quant-ph...")

    # Synchronous iteration
    results = client.results(search)
    for result in results:
        print(f"Title: {result.title}")
        print(f"Published: {result.published}")
        print("----")

if __name__ == '__main__':
    test_arxiv()
