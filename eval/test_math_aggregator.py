"""Unit tests for Math Aggregator, Temporal Delta Solver, and Attribute Extractor."""

from __future__ import annotations

import pytest
from search.phases.math_aggregator import extract_and_aggregate_quantities, parse_numeric_val, format_numeric_val
from search.phases.temporal_delta_solver import calculate_temporal_delta
from search.phases.attribute_extractor import extract_entity_attribute


class TestMathAggregator:
    def test_parse_numeric_val(self):
        assert parse_numeric_val("500,000") == 500000.0
        assert parse_numeric_val("300", "k") == 300000.0
        assert parse_numeric_val("1.5", "million") == 1500000.0

    def test_format_numeric_val(self):
        assert format_numeric_val(800000.0) == "800,000"
        assert format_numeric_val(14.5) == "14.50"

    def test_extract_and_aggregate_quantities(self):
        query = "How many documents am I planning to handle in total when combining my Elasticsearch and Solr projects?"
        candidates = [
            ("mem_1", "I have 500,000 documents in Elasticsearch."),
            ("mem_2", "I have 300,000 documents in Solr."),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result == "800,000"


class TestTemporalDeltaSolver:
    def test_calculate_temporal_delta(self):
        query = "How many days passed between when I started working on context window management and when I began developing vector search?"
        candidates = [
            ("mem_1", "Started working on context window management on 2024-03-01."),
            ("mem_2", "Began developing vector search on 2024-03-15."),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "14 days"


class TestAttributeExtractor:
    def test_extract_entity_attribute(self):
        query = "What version of the vector database am I evaluating for indexing over 1 million documents?"
        candidates = [
            ("mem_1", "Evaluating Qdrant version 1.7.0 for indexing 1M documents."),
        ]
        result = extract_entity_attribute(query, candidates)
        assert result == "1.7.0"
