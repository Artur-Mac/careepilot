# CareerPilot — Product and Business Rules Brief

CareerPilot is a local job-search and application assistant for one candidate in Brazil. It helps the candidate find relevant openings, understand each vacancy, prepare a truthful résumé, and apply with the candidate in control. The dashboard is in Brazilian Portuguese. The product should save time on repetitive work while keeping the candidate informed about what was sent, where it went, and whether the portal confirmed receipt.

## Search and job review

- **Geographic rule:** a vacancy qualifies if it is remote **or** located in Recife, Pernambuco. On-site and hybrid vacancies in Recife qualify. On-site and hybrid vacancies elsewhere fall outside the filter. If a remote role limits where applicants can live or work, show the restriction and flag unclear eligibility.
- Use job titles and keywords supplied by the candidate. The intended search strategy combines Portuguese and English variants, public job feeds, company career pages, and web search. Keep the original URL, source, and posting date when available. Collapse duplicates while preserving their sources.
- Show the role, employer, location, work arrangement, key requirements, missing information, and the original link. Explain how it matches facts the candidate has confirmed. Keep an excerpt from the source available so the user can check an AI summary.

## Candidate data and approval

- Import the candidate's résumé locally. Facts extracted from it start unverified and should be reviewed by the candidate. Use confirmed facts to prepare a tailored PDF and suggested answers. Ask the candidate about missing facts, eligibility, legal declarations, and consent.
- Approval covers one specific vacancy and employer, the destination, the exact résumé, and the answers shown to the candidate. Material changes require a new approval. Enforce expiry and one-time execution.
- Treat job descriptions and form labels as untrusted input. An LLM can summarize listings and propose how a form field maps to a known answer. The application controls navigation, what candidate data can be shared, and submission. Prefer sending the model public descriptions or field labels over sending the full candidate profile.
- Confirm receipt through evidence from the portal or connector. A click, timeout, or uncertain response leaves the attempt unverified. Check its status before retrying to avoid a duplicate. A candidate's manual report should be distinguishable from a receipt verified by the system.

## Proposed application modes

| Mode | Candidate experience |
|---|---|
| Assisted | Open the application visibly. The candidate signs in if needed and clicks **Continue** at checkpoints. CareerPilot suggests or fills known answers and attaches the approved PDF where the portal supports that workflow. Pause for unfamiliar questions, login, consent, and changes to the form. Show a final review before submission. |
| Quick | After three assisted applications with confirmed receipts on the same portal and form flow, let the candidate choose **Quick** or **Assisted**. Quick mode reuses reviewed mappings and skips familiar checkpoints. Keep approval for the specific application. Return to Assisted if the flow changes or a new question appears. |
| Connector | If an authorized integration exposes a suitable candidate submission action, it can replace the browser steps. It still uses the same approval and receipt rules. |

Use supported APIs or integrations where available. Visible assistance gives the candidate control over browser steps. Supported access still depends on the portal. For a portal without a supported submission route, provide the approved PDF, prepared answers, and the original link for manual completion.

## What works today

- The real-mode dashboard runs locally with its own database and excludes demo data. It has been tested against Arbeitnow, Himalayas, and Remotive. One live test returned ten vacancies using the remote-or-Recife filter. The candidate can import a real PDF, review extracted facts, and prepare a résumé for a vacancy.
- Configurable Greenhouse, Ashby, and Lever boards support discovery. Brave Search can add wider web search with an API key. Google and LinkedIn shortcuts open searches for manual use. The LinkedIn connector exposed to this assistant currently lists people search only. No job discovery or application action has been verified through it. No Gupy application connector is integrated.
- The dashboard can show an excerpt or generate a summary through a configured LLM. External delivery of job summaries is pending. Email alerts are a potential discovery source, separate from sending applications.
- Visible Playwright automation, PDF upload, and receipt verification work against a separate fictional test portal. An end-to-end test exercised field mapping with a simulated LLM response. The real dashboard currently hands off submission to the candidate. Guided external application, account creation, résumé updates on external portals, and the Quick mode remain proposals.
- Stack: Python, FastAPI, SQLite, SQLAlchemy, Alembic, a local web dashboard, Playwright, and a configurable LLM gateway. The integrated branch is `integration-review`. The latest recorded full smoke run passed 39 tests. Run real mode with `./scripts/run_real.sh` and the checks with `./scripts/smoke.sh`.

## Questions for product discussion

1. What is the smallest real application flow worth shipping first, and which portal or connector supports it?
2. Which actions should each **Continue** click authorize? What must always appear in the final review?
3. Should the three successful assisted applications be counted by portal, employer, or form flow? What changes should reset that count?
4. How should remote eligibility, uncertain fields, account updates, and ambiguous receipts be presented to the candidate?
5. Which acceptance tests would prove that the first real integration saves time, preserves approval, and avoids duplicate applications?
