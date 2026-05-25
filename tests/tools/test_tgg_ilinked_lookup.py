import json

from tools.tgg_ilinked_lookup import (
    extract_job_no,
    query_ilinked,
)


def _write_corpus(tmp_path):
    corpus = tmp_path / "full-import-test"
    tree = corpus / "tree"
    tree.mkdir(parents=True)
    rows = [
        [
            "PG/JOB/2605/0334",
            "Toilet door defective, cannot close.",
            "Job",
            "BLK 223A SUMANG LANE, #12-4947 MATILDA EDGE, SINGAPORE 821223",
            "New",
        ],
        [
            "PG/JOB/2605/0335",
            "Kitchen tap leaking.",
            "Job",
            "BLK 223A SUMANG LANE, #08-153 MATILDA EDGE, SINGAPORE 821223",
            "New",
        ],
        [
            "AM/JOB/2605/0112",
            "Ceiling leak in master bedroom.",
            "Job",
            "BLK 210 ANG MO KIO AVE 3, #04-118, SINGAPORE 560210",
            "In Progress",
        ],
    ]
    (tree / "leaf-0001-page-first.json").write_text(
        json.dumps(
            {
                "leaf": {"text": "Job (3)"},
                "pageArg": "first",
                "grid": {
                    "ok": True,
                    "headers": [
                        "",
                        "Task Number",
                        "Description",
                        "Task Type",
                        "Location",
                        "Created Date",
                        "Created By",
                        "Sub Status",
                        "Status",
                    ],
                    "rows": [
                        {
                            "cells": [
                                {"text": ""},
                                {"text": task_no},
                                {"text": description},
                                {"text": task_type},
                                {"text": location},
                                {"text": "2026-05-25"},
                                {"text": "Sky"},
                                {"text": status},
                                {"text": status},
                            ]
                        }
                        for task_no, description, task_type, location, status in rows
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return corpus


def test_extract_job_no_from_message():
    assert (
        extract_job_no("please check am/job/2605/0112 before sending the report")
        == "AM/JOB/2605/0112"
    )


def test_exact_match_returns_canonical_entry(tmp_path):
    corpus = _write_corpus(tmp_path)

    result = query_ilinked({"jobNo": "pg/job/2605/0334"}, str(corpus))

    assert result["confidence"] == "exact"
    assert result["matches"][0]["score"] == 1.0
    assert result["matches"][0]["entry"]["location"].startswith("BLK 223A")


def test_address_unit_fuzzy_match_ranks_correct_unit(tmp_path):
    corpus = _write_corpus(tmp_path)

    result = query_ilinked(
        {"message": "worker says blk 223A #12-4947 toilet door done", "limit": 2},
        str(corpus),
    )

    assert result["confidence"] == "high_similarity"
    assert result["matches"][0]["entry"]["taskNo"] == "PG/JOB/2605/0334"
    assert "block_unit_pair_match" in result["matches"][0]["reasons"]


def test_no_match_when_job_missing_and_address_dissimilar(tmp_path):
    corpus = _write_corpus(tmp_path)

    result = query_ilinked(
        {"jobNo": "BS/JOB/2605/9999", "message": "blk 999 #01-01"},
        str(corpus),
    )

    assert result["confidence"] == "no_match"
    assert result["matches"] == []
