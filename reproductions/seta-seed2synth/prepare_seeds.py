"""Fetch public Q&A seeds into SETA's documented Unix.SE input schema.

Run remotely. These are independently sampled public source inputs, not a copy of
the gated SETA split. Attribution and API responses are retained with the inputs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/work/seta")
API = "https://api.stackexchange.com/2.3/"


def get(endpoint: str, **parameters) -> dict:
    query = urllib.parse.urlencode({"site": "unix", "filter": "withbody", **parameters})
    with urllib.request.urlopen(API + endpoint + "?" + query, timeout=60) as response:
        data = json.load(response)
    if data.get("backoff"):
        raise RuntimeError(f"Stack Exchange API requests backoff: {data['backoff']}s")
    return data


def main() -> None:
    destination = ROOT / "seed_data" / "unix_linux_se"
    if (destination / "metadata.csv").exists():
        raise RuntimeError("Seed set already frozen; use existing inputs")
    destination.mkdir(parents=True, exist_ok=True)
    response = get("questions", tagged="bash", sort="votes", order="desc", pagesize=30)
    questions = [q for q in response["items"] if q.get("accepted_answer_id")][:5]
    rows, manifest = [], []
    for question in questions:
        answers = get(f"answers/{question['accepted_answer_id']}")
        answer = answers["items"][0]
        task_id = f"unix-se-{question['question_id']}"
        folder = destination / task_id
        folder.mkdir()
        seed = {"title": question["title"], "question_text": question["body"],
                "answer_text": answer["body"], "tags": question["tags"],
                "source": "unix_linux_se", "url": question["link"],
                "question_id": question["question_id"], "answer_id": answer["answer_id"],
                "attribution": {"question": question.get("owner"), "answer": answer.get("owner")},
                "content_license": {"question": question.get("content_license"),
                                    "answer": answer.get("content_license")}}
        body = json.dumps(seed, indent=2) + "\n"
        (folder / "main.json").write_text(body)
        rows.append({"task_id": task_id, "source": "unix_linux_se", "title": seed["title"],
                     "category": "bash", "tags": json.dumps(seed["tags"]),
                     "score": question["score"], "url": seed["url"], "filtered": "False",
                     "filter_reason": ""})
        manifest.append({"task_id": task_id, "url": seed["url"],
                         "sha256": hashlib.sha256(body.encode()).hexdigest()})
    with (destination / "metadata.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (ROOT / "seed-manifest.json").write_text(json.dumps({"selection": "first five accepted-answer questions from Unix.SE bash sorted votes descending; API response frozen", "inputs": manifest}, indent=2))
    (ROOT / "questions-response.json").write_text(json.dumps(response, indent=2))
    (ROOT / "smoke.csv").write_text("source,task_id\nunix_linux_se," + rows[0]["task_id"] + "\n")
    (ROOT / "five.csv").write_text("source,task_id\n" + "".join(f"unix_linux_se,{r['task_id']}\n" for r in rows))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
