from __future__ import annotations

from scripts.check_nodegroup_parity import diff_values, normalize_value, semantic_sha256


def test_normalize_value_stabilizes_float_edge_cases():
    assert normalize_value([-0.0, float("inf"), float("-inf"), float("nan")]) == [
        0.0,
        "Infinity",
        "-Infinity",
        "NaN",
    ]


def test_semantic_sha256_ignores_mapping_insertion_order():
    assert semantic_sha256({"b": 2, "a": [1.0]}) == semantic_sha256(
        {"a": [1.0], "b": 2}
    )


def test_diff_values_reports_nested_semantic_delta():
    differences = diff_values(
        {"nodes": [{"operation": "ADD", "inputs": [1.0, 2.0]}]},
        {"nodes": [{"operation": "MULTIPLY", "inputs": [1.0]}]},
    )

    assert "$.nodes[0].inputs: length 2 != 1" in differences
    assert "$.nodes[0].operation: 'ADD' != 'MULTIPLY'" in differences
