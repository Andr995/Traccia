# Traccia

Traccia is a small, source-first company research app. It searches SEC registrant filings for passages that mention Israel and nearby relationship terms, then shows the original SEC filing beside every candidate passage. The verdict is deliberately cautious: keyword proximity is a lead for analyst review, not proof of a corporate relationship.

## Free-by-design choices

- No paid LLM or search API is required.
- The app uses public SEC registrant and filing data; search and analysis stay bounded to a small set of documents.
- Retrieval combines BM25 candidate selection with a CPU FAISS rerank of at most 200 passages using a small ONNX embedding model (`all-MiniLM-L6-v2` through FastEmbed).
- spaCy's small English NER model loads only when evidence needs entity extraction. The embedding model and NER pipeline objects are released after each analysis; native runtimes can retain memory arenas, so this is a best-effort reduction, not a hard cap.
- Reports are saved in the analyst's browser only. The server does not keep company dossiers or source text.
- Render Blueprint uses its Free web plan. This is a no-cost preview configuration, not a promise of always-on availability: Render can spin down an idle Free service, and its filesystem is ephemeral.

There is no zero-cost hosting architecture that can guarantee continuous uptime, persistent server-side storage, no provider limits, and complete global source coverage. Traccia chooses a small scope and makes those limits visible.

## Run locally

Use Python 3.12. Set a real SEC user agent that identifies the operator and includes a contact address. SEC requests are disabled until that value is provided.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m spacy download en_core_web_sm
$env:SEC_USER_AGENT = "Traccia/1.0 Your Organization contact@example.org"
$env:FASTEMBED_CACHE_PATH = ".model-cache"
python scripts/download_models.py
python app.py
```

Open `http://127.0.0.1:5000`. The SEC company lookup only includes issuers in SEC's public company ticker file. Private companies and many non-US issuers will not appear; an empty search is not evidence that an entity has no relationship.

For a public deployment, also set a strong `APP_ACCESS_KEY`. The access key is required in the browser for API requests and is not saved to local storage. SEC's user-agent value is a separate deployment setting.

## Render deployment

1. Push this folder to a Git repository and create a Render Blueprint from `render.yaml`.
2. Set `SEC_USER_AGENT` to the deployer's genuine app name and contact email.
3. Set a private random `APP_ACCESS_KEY` before exposing the app publicly.
4. Deploy. The build installs the small spaCy and FastEmbed model assets so the first analysis does not need to fetch model weights.

Render Free currently provides 512 MB RAM but can spin down when idle and does not provide persistent local storage. The app therefore limits each analysis to one active job and keeps case history in the browser. Monitor peak memory on the deployed service. If it repeatedly approaches 400–430 MB, reports memory restarts, or concurrency needs to increase, move to a larger paid compute tier; the current Render web Starter tier is also 512 MB, so it does not provide additional RAM.

## Analysis contract

- Search method: BM25 selects up to 200 candidate passages, then FAISS reranks them with local ONNX embeddings; falls back to BM25 and labels the retrieval mode if FAISS/model assets cannot load.
- Language: initial retrieval and relationship patterns focus on English SEC filing text.
- Candidate terms: Israel, Israeli, Tel Aviv, Jerusalem and selected relationship terms such as subsidiary, office, supplier, contract, or joint venture.
- Verdicts distinguish a candidate relation, an Israel mention without a relation cue, no evidence within retrieved filings, and incomplete analysis.
- The displayed 0–100 score is an uncalibrated evidence-relevance heuristic. It is not a probability that a relationship exists.
- Every excerpt links to its SEC filing. Review the full document, nearby context, date, negation, and entity identity before making a compliance decision.

## Current scope and next steps

This first implementation is an SEC-focused vertical slice, not a global corporate registry. To broaden it, add verified connectors for official company registries, annual-report archives, and procurement databases; preserve each provider's usage conditions; add multilingual NER/embedding support; and validate the verdict rules against analyst-labelled cases. Do not convert “no evidence found” into “no relationship.”
