"""Regenerate the data-driven sections of README.md from the current dataset.

Run after every dataset refresh:
    python scripts/update-readme-stats.py [--data data/press-corner.parquet]

Rewrites everything between <!-- STATS:<NAME> --> ... <!-- /STATS:<NAME> -->
marker pairs in README.md, so the README never drifts from the data.
"""

import argparse
import re
from pathlib import Path

import pandas as pd

# Human-readable labels for type codes, incl. legacy RAPID series the UI no longer lists
TYPE_LABELS = {
    "IP": "Press release",
    "SPEECH": "Speech",
    "MEX": "Daily news (Midday Express)",
    "MEMO": "Memo / background note",
    "STATEMENT": "Statement",
    "QANDA": "Questions and answers",
    "READ": "Read-out",
    "INF": "Infringement decisions",
    "FS": "Factsheet",
    "AC": "News article",
    "BIO": "Spokesperson's briefing (legacy)",
    "P": "Early press note (legacy)",
    "PRES": "Council of the EU press release (legacy)",
    "CJE": "Court of Justice press release (legacy)",
    "PESC": "CFSP declaration (legacy)",
    "CES": "European Economic and Social Committee (legacy)",
    "COR": "Committee of the Regions (legacy)",
    "ECA": "Court of Auditors (legacy)",
    "EO": "European Ombudsman (legacy)",
    "BEI": "European Investment Bank (legacy)",
    "OLAF": "European Anti-Fraud Office (legacy)",
    "EDPS": "European Data Protection Supervisor (legacy)",
    "EPSO": "European Personnel Selection Office (legacy)",
    "STAT": "Eurostat release (legacy)",
    "DOC": "European Council conclusions digest (legacy)",
    "AGENDA": "Weekly agenda (legacy)",
    "CLDR": "Calendar (legacy)",
    "WM": "Week in the media (legacy)",
    "DN": "Daily news bulletin (legacy)",
    "COUNTRY": "Country information (legacy)",
    "ETW": "Enterprise Europe Network (legacy)",
    "TRANS": "Transcript (legacy)",
}


# Compute all README stat blocks from the dataset and return them keyed by marker name.
def compute_blocks(df: pd.DataFrame) -> dict[str, str]:
    date_col = "date" if "date" in df.columns else "publication_date"
    type_col = "doc_type" if "doc_type" in df.columns else "document_type"
    d = pd.to_datetime(df[date_col])

    overview = (
        f"**{len(df):,} documents** · {d.min().date()} to {d.max().date()} · "
        f"{df[type_col].nunique()} document types · "
        f"{df['language'].str.upper().value_counts().idxmax()} language edition"
    )

    # Per-type coverage table, largest series first
    g = df.groupby(type_col).agg(n=(type_col, "size"), first=(date_col, "min"), last=(date_col, "max"))
    g = g.sort_values("n", ascending=False)
    lines = ["| Code | What it is | Documents | Coverage |", "|---|---|---:|---|"]
    for code, row in g.iterrows():
        label = TYPE_LABELS.get(code, "—")
        lines.append(f"| `{code}` | {label} | {row.n:,} | {row['first'][:4]}–{row['last'][:4]} |")
    types_table = "\n".join(lines)

    # Metadata availability by era (share of non-empty values)
    df = df.assign(_decade=(d.dt.year // 10) * 10)
    fields = [
        ("full_text", "Full text"),
        ("subtitle", "Subtitle"),
        ("summary", "Summary"),
        ("policy_areas", "Policy areas"),
        ("spokespersons" if "spokespersons" in df.columns else "authors", "Spokespersons"),
    ]
    if "commissioners" in df.columns:
        fields.append(("commissioners", "Commissioners"))
    if "place" in df.columns:
        fields.append(("place", "Place"))
    decades = sorted(x for x in df["_decade"].unique() if x >= 1980)
    header = "| Field | " + " | ".join(f"{dec}s" for dec in decades) + " |"
    sep = "|---|" + "---|" * len(decades)
    era_lines = [header, sep]
    for col, label in fields:
        if col not in df.columns:
            continue
        filled = df.groupby("_decade")[col].apply(
            lambda s: (s.fillna("").astype(str).str.len() > 0).mean()
        )
        cells = " | ".join(f"{filled.get(dec, 0):.0%}" for dec in decades)
        era_lines.append(f"| {label} | {cells} |")
    era_table = "\n".join(era_lines)

    return {"OVERVIEW": overview, "TYPES": types_table, "ERA": era_table}


# Replace the content of each marked block in the README with freshly computed stats.
def rewrite_readme(readme_path: Path, blocks: dict[str, str]) -> None:
    text = readme_path.read_text()
    for name, content in blocks.items():
        pattern = re.compile(
            rf"(<!-- STATS:{name} -->\n).*?(\n<!-- /STATS:{name} -->)", re.DOTALL
        )
        if not pattern.search(text):
            print(f"warning: marker STATS:{name} not found in README")
            continue
        text = pattern.sub(rf"\g<1>{content}\g<2>", text)
    readme_path.write_text(text)
    print(f"Updated {readme_path} ({', '.join(blocks)})")


parser = argparse.ArgumentParser()
parser.add_argument("--data", default="data/press-corner.parquet")
parser.add_argument("--readme", default="README.md")
args = parser.parse_args()

df = pd.read_parquet(args.data)
rewrite_readme(Path(args.readme), compute_blocks(df))
