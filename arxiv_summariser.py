import requests
import xml.etree.ElementTree as ET
import datetime
from openai import OpenAI
from openai_apikey import API_KEY

def fetch_arxiv_quant_ph():
    """
    Fetch quant-ph papers from arXiv that were published in the last 24 hours.
    """
    # Define time window: now and 24 hours ago (in UTC)
    now = datetime.datetime.now(datetime.timezone.utc)
    yesterday = now - datetime.timedelta(days=1)
    
    # Query parameters: search for category quant-ph, sorted by submission date (descending)
    url = ("http://export.arxiv.org/api/query?"
           "search_query=cat:quant-ph&sortBy=submittedDate&sortOrder=descending&max_results=100")
    response = requests.get(url)
    if response.status_code != 200:
        print("Error fetching from arXiv API")
        return []
    
    # Parse the returned XML
    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        title = entry.find("atom:title", ns).text.strip()
        summary = entry.find("atom:summary", ns).text.strip()
        published_str = entry.find("atom:published", ns).text.strip()
        pub_date = datetime.datetime.strptime(published_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        
        # Select papers published within the last 24 hours
        if pub_date > yesterday:
            papers.append({
                "title": title,
                "summary": summary,
                "published": published_str
            })
    return papers

def query_chatgpt(papers):
    """
    Uses the ChatGPT API to select papers that match Luca Innocenti's interests.
    """
    # Define Luca Innocenti's research interests
    interests = (
        "Luca Innocenti is interested in topics such as quantum machine learning "
        "(with an emphasis on quantum reservoir computing and quantum extreme learning machines), "
        "quantum information theory, quantum metrology, experimental quantum optics, "
        "and foundational topics including quantum information scrambling and quantum Darwinism."
    )
    
    # Create a formatted list of papers
    paper_list_text = "\n\n".join([
        f"Title: {p['title']}\nPublished: {p['published']}\nAbstract: {p['summary']}"
        for p in papers
    ])
    
    # Build the prompt for ChatGPT
    prompt = (
        f"You are an expert in quantum physics. Given the following list of quant-ph papers "
        f"published in the last day and Luca Innocenti's research interests:\n\n"
        f"{interests}\n\n"
        f"List of papers:\n{paper_list_text}\n\n"
        "Please select the papers that are most likely to be interesting to Luca Innocenti. "
        "For each selected paper, provide a brief reason explaining why it was chosen. "
        "Respond in a structured format."
    )
    
    # Initialize the OpenAI client
    client = OpenAI(api_key=API_KEY)
    
    # Call the ChatGPT API
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that selects relevant research papers."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content

def main():
    # Fetch the recent quant-ph papers
    papers = fetch_arxiv_quant_ph()
    if not papers:
        print("No new quant-ph papers found from the last day.")
        return
    
    print("Fetched the following papers published in the last day:")
    for p in papers:
        print(f"- {p['title']} (Published: {p['published']})")
    
    # Query ChatGPT to select the most relevant papers based on Luca Innocenti's interests
    selected = query_chatgpt(papers)
    print("\nSelected Papers and Reasons:")
    print(selected)

if __name__ == "__main__":
    main()