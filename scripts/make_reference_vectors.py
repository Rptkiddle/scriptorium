"""make reference vectors: embed a fixed set of texts with the configured
model and write them to tests/data/reference_vectors.json.

The test suite (tests/test_reference_vectors.py) checks that the local
embedder still reproduces these vectors, which catches a library update
that changes them; a remote embedder (e.g. the one used for bulk indexing
on a GPU server) is checked against the same file before it writes. Run
this only after an intentional change of model, revision or dimension,
together with a rebuild of the index.

Usage:
    uv run python scripts/make_reference_vectors.py
"""

import json
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.index import Embedder

OUT = Path(__file__).resolve().parents[1] / "tests" / "data" / "reference_vectors.json"

# Written for this file: short passages in the register of the library's
# papers (communication science, computing, methods), plus queries.
DOCUMENTS = [
    "News recommender systems select which articles a reader sees, and the selection shapes the agenda each reader encounters.",
    "We measure exposure diversity as the spread of topics across the sequence of articles a user reads in a week.",
    "Agent-based models represent individuals as rule-following agents whose interactions produce population-level patterns.",
    "Large language models can be prompted to act as survey respondents, but their answers vary with the wording of the persona.",
    "Toxic comments were coded by two annotators; disagreements were resolved by discussion before the final label was assigned.",
    "The filter bubble hypothesis holds that personalisation narrows the range of viewpoints a person meets online.",
    "Echo chambers describe networks in which beliefs are reinforced by repeated exposure to like-minded sources.",
    "Opinion dynamics models such as bounded confidence let agents update only when others' views are close enough to their own.",
    "Platform governance concerns the rules, enforcement practices and accountability mechanisms that shape what may be posted.",
    "The study combines web tracking data with a panel survey to link actual news use to self-reported attitudes.",
    "Polarisation can be affective, concerning feelings toward the other side, or ideological, concerning distances between positions.",
    "Generative agents keep a memory stream of observations and retrieve relevant memories when planning their next action.",
    "Content moderation at scale relies on automated classifiers whose errors fall unevenly across languages and communities.",
    "A sequence of news items can be described by the semantic distance between consecutive articles.",
    "Misinformation corrections reduce belief in the false claim, although the effect decays over the following weeks.",
    "We fit a multilevel model with articles nested in outlets and random intercepts for each outlet.",
    "Transformer encoders map each token to a contextual vector; pooling these vectors yields a sentence embedding.",
    "Retrieval-augmented generation grounds a model's answer in passages retrieved from a document collection.",
    "Serendipity in recommendation refers to items that are both unexpected and useful to the person who receives them.",
    "Simulated social media platforms let researchers test interventions that would be unethical or impossible in the field.",
    "Computational communication science uses large digital trace data sets together with methods from computer science.",
    "The incivility classifier reached an F1 score of 0.81 on held-out comments from the same platform.",
    "Selective exposure is the tendency to choose information that agrees with one's existing beliefs.",
    "Complex systems show emergent behaviour that cannot be read off the properties of their components alone.",
    "Hybrid communication systems include human participants and automated agents that post, reply and recommend.",
    "Interviews with editors show how audience metrics enter decisions about which stories receive prominence.",
    "The algorithm ranks candidate posts by predicted engagement, then removes items that violate the content policy.",
    "We release the code and the anonymised data so that the analysis can be reproduced from the raw files.",
    "Topic models summarise a corpus as mixtures of word distributions, each read by the analyst as a theme.",
    "A preregistered experiment randomly assigned participants to a personalised or a non-personalised news feed.",
    "Social bots amplify particular hashtags, which can make fringe positions appear more widely held than they are.",
    "The European Digital Services Act requires very large platforms to assess systemic risks, including risks to civic discourse.",
    "Multi-agent simulations with language models raise questions about what such agents can tell us about real populations.",
    "Network centrality measures identify accounts that bridge otherwise separate clusters of users.",
    "Readers spent more time on articles placed at the top of the page, regardless of the article's topic.",
    "Dense retrieval compares a query vector with document vectors and returns the documents with the highest similarity.",
    "Measurement validity requires that a computational indicator captures the concept it is meant to represent.",
    "The decline of trust in news varies across countries and is strongest among people who distrust institutions in general.",
    "An embedding model trained with contrastive learning places related texts close together in vector space.",
    "We observe that recommendation diversity increased after the platform introduced an editorial curation layer.",
]
QUERIES = [
    "how do news recommenders affect diversity of exposure",
    "agent-based models of polarisation",
    "can language models simulate survey respondents",
    "measuring toxicity in online comments",
    "evidence on filter bubbles and echo chambers",
    "regulation of large online platforms in Europe",
    "serendipity in recommender systems",
    "retrieval-augmented generation over documents",
    "social bots and amplification",
    "validity of computational measures",
]


def main() -> None:
    config = load_config()
    embedder = Embedder(config.embedding_model, config.embedding_dim)
    documents = embedder.encode_documents(DOCUMENTS)
    queries = [embedder.encode_query(q) for q in QUERIES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "model": config.embedding_model,
        "revision": embedder.revision,
        "dim": config.embedding_dim,
        "dtype": str(next(embedder.model.parameters()).dtype),
        "documents": [{"text": t, "vector": [round(x, 6) for x in v]}
                      for t, v in zip(DOCUMENTS, documents, strict=True)],
        "queries": [{"text": t, "vector": [round(x, 6) for x in v]}
                    for t, v in zip(QUERIES, queries, strict=True)],
    }, indent=0))
    print(f"wrote {OUT} ({len(DOCUMENTS)} documents, {len(QUERIES)} queries;"
          f" revision {embedder.revision})")


if __name__ == "__main__":
    main()
