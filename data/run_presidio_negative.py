#!/usr/bin/env python3
"""
Presidio Negative Query Evaluation - False Positive Rate Assessment
Evaluates Presidio on queries with NO PHI to measure over-redaction
"""

import json
import csv
import os
import re
from datetime import datetime
from typing import List, Dict, Any
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


class MedicalRecordNumberRecognizer(PatternRecognizer):
    """Custom recognizer for Medical Record Numbers (MRN)"""
    def __init__(self):
        patterns = [
            Pattern("MRN_PATTERN_1", r"\b\d{6,10}\b", 0.5),
            Pattern("MRN_PATTERN_2", r"\bMRN[:\s]+\d{6,10}\b", 0.9),
            Pattern("MRN_PATTERN_3", r"\b[A-Z]{2,3}-\d{5,8}\b", 0.7),
        ]
        super().__init__(
            supported_entity="MEDICAL_RECORD_NUMBER",
            patterns=patterns,
            name="MedicalRecordNumberRecognizer"
        )


class UniqueIdentifierRecognizer(PatternRecognizer):
    """Custom recognizer for unique identifiers"""
    def __init__(self):
        patterns = [
            Pattern("ID_PATTERN_1", r"\bID[:\s]+\d{6,12}\b", 0.9),
            Pattern("ID_PATTERN_2", r"\(ID:\s*\d{6,12}\)", 0.95),
        ]
        super().__init__(
            supported_entity="UNIQUE_IDENTIFIER",
            patterns=patterns,
            name="UniqueIdentifierRecognizer"
        )


def initialize_presidio_analyzer() -> AnalyzerEngine:
    """Initialize Presidio with custom medical recognizers"""
    analyzer = AnalyzerEngine()
    analyzer.registry.add_recognizer(MedicalRecordNumberRecognizer())
    analyzer.registry.add_recognizer(UniqueIdentifierRecognizer())
    return analyzer


def map_presidio_entity_to_hipaa(entity_type: str) -> str:
    """Map Presidio entity types to HIPAA PHI categories"""
    mapping = {
        "PERSON": "NAME",
        "LOCATION": "GEOGRAPHIC_LOCATION",
        "GPE": "GEOGRAPHIC_LOCATION",
        "DATE_TIME": "DATE",
        "EMAIL_ADDRESS": "EMAIL_ADDRESS",
        "PHONE_NUMBER": "PHONE_NUMBER",
        "US_SSN": "SOCIAL_SECURITY_NUMBER",
        "MEDICAL_LICENSE": "CERTIFICATE_LICENSE_NUMBER",
        "URL": "WEB_URL",
        "IP_ADDRESS": "IP_ADDRESS",
        "MEDICAL_RECORD_NUMBER": "MEDICAL_RECORD_NUMBER",
        "UNIQUE_IDENTIFIER": "UNIQUE_IDENTIFIER",
        "CREDIT_CARD": "ACCOUNT_NUMBER",
        "US_BANK_NUMBER": "ACCOUNT_NUMBER",
        "CRYPTO": "UNIQUE_IDENTIFIER",
    }
    return mapping.get(entity_type, entity_type)


def detect_false_redactions(
    original_query: str,
    anonymized_query: str
) -> Dict[str, Any]:
    """
    Detect false positive redactions in queries that should have NO PHI
    """
    # Count redaction markers
    redaction_pattern = r'\[REDACTED_([A-Z_]+)\]'
    matches = re.findall(redaction_pattern, anonymized_query)

    false_redactions_by_type = {}
    for redaction_type in matches:
        false_redactions_by_type[redaction_type] = false_redactions_by_type.get(redaction_type, 0) + 1

    has_false_redactions = len(matches) > 0

    return {
        "has_false_redactions": has_false_redactions,
        "redaction_count": len(matches),
        "false_redactions_by_type": false_redactions_by_type,
        "status": "OVER_REDACTED" if has_false_redactions else "CLEAN"
    }


def process_negative_queries(csv_file: str, output_dir: str) -> Dict[str, Any]:
    """
    Process negative queries (NO PHI) to measure false positive rate

    Args:
        csv_file: Path to negative_queries.csv
        output_dir: Directory to save trace files

    Returns:
        Aggregate statistics
    """
    # Initialize Presidio
    print(f"Initializing Presidio analyzer...")
    analyzer = initialize_presidio_analyzer()
    anonymizer = AnonymizerEngine()

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Read negative queries
    print(f"Loading negative queries from {csv_file}...")
    queries = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        queries = list(reader)

    print(f"Processing {len(queries)} negative queries (NO PHI expected)...")

    # Process each query
    queries_correctly_unchanged = 0
    queries_with_false_positives = 0
    total_false_redactions = 0
    false_redactions_by_type = {}

    for i, query_row in enumerate(queries):
        query_id = query_row['query_id']
        query_text = query_row['query_text']

        # Analyze for PII/PHI
        analyzer_results = analyzer.analyze(
            text=query_text,
            language="en",
            entities=None,
            score_threshold=0.35
        )

        # Convert results to detailed entity list
        detected_entities = []
        for result in analyzer_results:
            detected_entities.append({
                "entity_type": result.entity_type,
                "hipaa_category": map_presidio_entity_to_hipaa(result.entity_type),
                "start": result.start,
                "end": result.end,
                "score": result.score,
                "text": query_text[result.start:result.end],
                "recognizer": result.recognition_metadata.get("recognizer_name", "Unknown")
            })

        # Create operator config for anonymization
        operators = {}
        for result in analyzer_results:
            hipaa_type = map_presidio_entity_to_hipaa(result.entity_type)
            operators[result.entity_type] = OperatorConfig(
                "replace",
                {"new_value": f"[REDACTED_{hipaa_type}]"}
            )

        # Anonymize
        anonymized_result = anonymizer.anonymize(
            text=query_text,
            analyzer_results=analyzer_results,
            operators=operators
        )

        anonymized_text = anonymized_result.text

        # Detect false redactions
        false_redaction_check = detect_false_redactions(query_text, anonymized_text)

        # Aggregate false redactions by type
        for redaction_type, count in false_redaction_check['false_redactions_by_type'].items():
            false_redactions_by_type[redaction_type] = false_redactions_by_type.get(redaction_type, 0) + count

        # Create trace file
        trace = {
            "query_id": query_id,
            "timestamp": datetime.now().isoformat(),
            "model": "Presidio-2.2.360",
            "query_type": "NEGATIVE (No PHI)",
            "analyzer_config": {
                "language": "en",
                "score_threshold": 0.35,
                "custom_recognizers": ["MedicalRecordNumberRecognizer", "UniqueIdentifierRecognizer"]
            },
            "original_query": query_text,
            "presidio_output": anonymized_text,
            "presidio_entities_detected": detected_entities,
            "false_redaction_check": false_redaction_check
        }

        # Save trace file
        trace_file = os.path.join(output_dir, f"query_{query_id}_trace.json")
        with open(trace_file, 'w', encoding='utf-8') as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)

        # Update statistics
        total_false_redactions += false_redaction_check['redaction_count']
        if false_redaction_check['has_false_redactions']:
            queries_with_false_positives += 1
        else:
            queries_correctly_unchanged += 1

        # Progress
        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(queries)} queries...")

    # Calculate aggregate metrics
    false_positive_rate = queries_with_false_positives / len(queries) if queries else 0
    specificity = queries_correctly_unchanged / len(queries) if queries else 1.0

    aggregate = {
        "evaluation_timestamp": datetime.now().isoformat(),
        "model": "Presidio-2.2.360",
        "query_type": "negative",
        "total_queries_processed": len(queries),
        "queries_correctly_unchanged": queries_correctly_unchanged,
        "queries_with_false_positives": queries_with_false_positives,
        "total_false_redactions": total_false_redactions,
        "false_redactions_by_type": false_redactions_by_type,
        "false_positive_rate": false_positive_rate,
        "specificity": specificity
    }

    # Save aggregate summary
    summary_file = os.path.join(output_dir, "aggregate_summary.json")
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(aggregate, f, indent=2)

    print(f"\n✓ Completed {len(queries)} negative queries")
    print(f"  Correctly unchanged: {queries_correctly_unchanged}")
    print(f"  False positives: {queries_with_false_positives}")
    print(f"  Total false redactions: {total_false_redactions}")
    print(f"  False positive rate: {false_positive_rate:.2%}")
    print(f"  Specificity: {specificity:.2%}")

    return aggregate


def main():
    """Main execution"""
    base_dir = "/Users/jacweath/Desktop/safesearch_/data"

    print("=" * 80)
    print("PRESIDIO NEGATIVE QUERY EVALUATION (False Positive Assessment)")
    print("=" * 80)

    negative_csv = os.path.join(base_dir, "negative_queries.csv")
    negative_output = os.path.join(base_dir, "Presidio_negative")

    results = process_negative_queries(
        csv_file=negative_csv,
        output_dir=negative_output
    )

    print("\n" + "=" * 80)
    print("EVALUATION COMPLETE")
    print("=" * 80)
    print(f"\nResults saved to: {negative_output}")
    print(f"  - {results['total_queries_processed']} trace files")
    print(f"  - aggregate_summary.json")
    print(f"\nFalse Positive Rate: {results['false_positive_rate']:.2%}")
    print(f"Specificity: {results['specificity']:.2%}")
    print(f"\nComparison to GPT-4o:")
    print(f"  - GPT-4o false positive rate: 96.80%")
    print(f"  - Presidio false positive rate: {results['false_positive_rate']:.2%}")
    print(f"  - Difference: {96.80 - (results['false_positive_rate'] * 100):.2f}%")


if __name__ == "__main__":
    main()
