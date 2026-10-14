# Meals & Entertainment Guidance (BlackLine Solutions)

A bookkeeper describes an expense, optionally attaches a receipt or invoice, and gets:

1. The category and deductibility (or nonprofit functional class) **per the firm guides** in `/reference`.
2. A **live web check** of current IRS / primary-source guidance for that expense.
3. A **discrepancy flag** whenever the web check disagrees with the guides. The headline answer always follows the guide; the flag shows what current sources say and what treatment they'd lead to.

If key facts are missing, Claude asks clarifying questions first (usually one, up to three per round).

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit interface |
| `engine.py` | Anthropic API calls (web search + PDF reading + structured answer) |
| `prompts.py` | How Claude should use the guides, plus the firm's interpretive positions |
| `reference/ME_Methodology_General.pdf` | Firm guide, taxable businesses |
| `reference/ME_Methodology_Nonprofit.pdf` | Firm guide, nonprofits |

## Deploy (GitHub + Streamlit Community Cloud)

1. Create a **private** GitHub repo and upload everything in this folder (including the hidden `.streamlit` folder and `.gitignore`).
2. On share.streamlit.io: **Create app** > pick the repo > main file `app.py`.
3. Under **Advanced settings > Secrets**, paste (see `.streamlit/secrets.toml.example`):
   ```
   ANTHROPIC_API_KEY = "sk-ant-..."
   APP_PASSWORD = "your-team-password"
   ```
4. Deploy, then share the link and password with the team.

## Updating the rules

The PDFs **are** the rules. To change them, replace the file in `/reference` (keep the same file name) and commit. The app picks it up on the next load.

Edit `prompts.py` only if one of the firm interpretive positions changes (e.g. "reason, not location").

## Cost and privacy

- Each submission calls the Anthropic API on the firm's key: model usage plus usually one web search (max 2, adjust with `WEB_SEARCH_MAX_USES`), limited to primary sources (irs.gov, the Code, regs, Federal Register). The guides are sent as plain text, receipt photos are downsized, and receipt PDFs are capped at 3 pages. A usage line under each answer shows tokens and searches; set `SHOW_USAGE = "false"` in secrets to hide it.
- Receipts are sent to Anthropic for processing. The app stores nothing; each browser session is independent.
- The password screen keeps the public link from spending the firm's API budget. Remove `APP_PASSWORD` from secrets to turn it off.

## Local testing

```
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then add your key
streamlit run app.py
```
