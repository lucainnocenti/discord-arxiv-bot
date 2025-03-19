# import arxiv

# def test_arxiv():
#     client = arxiv.Client()

#     # Create a simple search to fetch up to 10 results from quant-ph
#     search = arxiv.Search(
#         query="cat:quant-ph",
#         max_results=10,
#         sort_by=arxiv.SortCriterion.SubmittedDate,
#         sort_order=arxiv.SortOrder.Descending,
#     )

#     print("Fetching up to 10 results from arXiv's quant-ph...")

#     # Synchronous iteration
#     results = client.results(search)
#     for result in results:
#         print(f"Title: {result.title}")
#         print(f"Published: {result.published}")
#         print("----")

# if __name__ == '__main__':
#     test_arxiv()

import arxiv
from datetime import datetime

def main():
    # List of authors to search for
    authors = [
        "Mauro Paternostro",
        "Francesco Ciccarello"
    ]

    # Define the earliest date we want in YYYYMMDD format
    # e.g., 2023-01-01 -> "20230101"
    start_dt = datetime(2023, 1, 1)
    date_str = start_dt.strftime("%Y%m%d")

    # Build the query:
    # 1. category: quant-ph
    # 2. any of our authors (join with OR)
    # 3. submitted after the chosen date
    authors_query = " OR ".join(f'au:"{a}"' for a in authors)
    query_str = (
        f'cat:quant-ph AND ({authors_query}) '
        f'AND submittedDate:[{date_str} TO 20250319]'
    )

    print("Querying arXiv with:\n", query_str, "\n")

    # Make the search request
    search = arxiv.Search(
        query=query_str,
        max_results=50,  # or more if you wish
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    # Retrieve and print results
    results = list(search.results())
    print(f"Found {len(results)} results.\n")

    for paper in results:
        print("Title:    ", paper.title)
        print("Authors:  ", ", ".join(str(a) for a in paper.authors))
        print("Date:     ", paper.published.strftime('%Y-%m-%d'))
        print("URL:      ", paper.entry_id)
        print("-" * 60)

if __name__ == "__main__":
    main()
