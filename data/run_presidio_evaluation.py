#!/usr/bin/env python3
"""
Presidio HIPAA De-identification Evaluation Script
Evaluates Presidio on synthetic PHI dataset with trace logging
Matches GPT-4o trace structure for comparison
"""

import json
import csv
import os
import re
from datetime import datetime
from typing import List, Dict, Any, Tuple
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


class MedicalRecordNumberRecognizer(PatternRecognizer):
    """Custom recognizer for Medical Record Numbers (MRN)"""
    def __init__(self):
        patterns = [
            Pattern("MRN_PATTERN_1", r"\b\d{6,10}\b", 0.5),  # 6-10 digit numbers
            Pattern("MRN_PATTERN_2", r"\bMRN[:\s]+\d{6,10}\b", 0.9),  # MRN: 123456
            Pattern("MRN_PATTERN_3", r"\b[A-Z]{2,3}-\d{5,8}\b", 0.7),  # ABC-12345
        ]
        super().__init__(
            supported_entity="MEDICAL_RECORD_NUMBER",
            patterns=patterns,
            name="MedicalRecordNumberRecognizer"
        )


class UniqueIdentifierRecognizer(PatternRecognizer):
    """Custom recognizer for unique identifiers (ID numbers)"""
    def __init__(self):
        patterns = [
            Pattern("ID_PATTERN_1", r"\bID[:\s]+\d{6,12}\b", 0.9),  # ID: 123456789
            Pattern("ID_PATTERN_2", r"\(ID:\s*\d{6,12}\)", 0.95),  # (ID: 123456789)
        ]
        super().__init__(
            supported_entity="UNIQUE_IDENTIFIER",
            patterns=patterns,
            name="UniqueIdentifierRecognizer"
        )


def initialize_presidio_analyzer() -> AnalyzerEngine:
    """Initialize Presidio with custom medical recognizers"""
    analyzer = AnalyzerEngine()

    # Add custom medical recognizers
    analyzer.registry.add_recognizer(MedicalRecordNumberRecognizer())
    analyzer.registry.add_recognizer(UniqueIdentifierRecognizer())

    return analyzer


def map_presidio_entity_to_hipaa(entity_type: str) -> str:
    """Map Presidio entity types to HIPAA PHI categories"""
    mapping = {
        "PERSON": "NAME",
        "LOCATION": "GEOGRAPHIC_LOCATION",
        "GPE": "GEOGRAPHIC_LOCATION",  # Geopolitical entity
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


def anonymize_with_presidio(
    text: str,
    analyzer: AnalyzerEngine,
    anonymizer: AnonymizerEngine
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Analyze and anonymize text using Presidio

    Returns:
        Tuple of (anonymized_text, detected_entities)
    """
    # Analyze for PII/PHI
    analyzer_results = analyzer.analyze(
        text=text,
        language="en",
        entities=None,  # Detect all entity types
        score_threshold=0.35  # Lower threshold to catch more potential PHI
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
            "text": text[result.start:result.end],
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
        text=text,
        analyzer_results=analyzer_results,
        operators=operators
    )

    return anonymized_result.text, detected_entities


def parse_ground_truth_phi(phi_entities_json: str) -> List[Dict[str, Any]]:
    """Parse ground truth PHI entities from CSV JSON string"""
    try:
        entities = json.loads(phi_entities_json)
        return entities
    except:
        return []


def format_ground_truth_for_trace(entities: List[Dict[str, Any]]) -> str:
    """Format ground truth PHI entities as pipe-separated string"""
    if not entities:
        return ""

    parts = []
    for entity in entities:
        parts.append(f"{entity['type']}: {entity['value']}")

    return " | ".join(parts)


def detect_phi_leaks(
    original_query: str,
    anonymized_query: str,
    ground_truth_entities: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Detect if any ground truth PHI leaked into anonymized output
    """
    leaked_phi = []

    for entity in ground_truth_entities:
        phi_value = entity['value']

        # Check if PHI value appears in anonymized output
        if phi_value.lower() in anonymized_query.lower():
            leaked_phi.append({
                "type": entity['type'],
                "value": phi_value,
                "start": entity.get('start', -1),
                "end": entity.get('end', -1)
            })

    return {
        "leaked_phi_count": len(leaked_phi),
        "leaked_phi_details": leaked_phi,
        "status": "LEAK_DETECTED" if leaked_phi else "NO_LEAK"
    }


def calculate_metrics(
    ground_truth_entities: List[Dict[str, Any]],
    detected_entities: List[Dict[str, Any]],
    leak_detection: Dict[str, Any]
) -> Dict[str, Any]:
    """Calculate recall, precision, F1 score"""
    total_phi = len(ground_truth_entities)
    leaked = leak_detection['leaked_phi_count']
    successfully_redacted = total_phi - leaked

    # Recall: successfully redacted / total PHI
    recall = successfully_redacted / total_phi if total_phi > 0 else 1.0

    # For precision, count how many detected entities matched ground truth
    # This is an approximation - matching by text value
    gt_values = {e['value'].lower() for e in ground_truth_entities}
    detected_values = {e['text'].lower() for e in detected_entities}

    true_positives = len(gt_values & detected_values)
    total_detected = len(detected_entities)

    precision = true_positives / total_detected if total_detected > 0 else 1.0

    # F1 score
    if recall + precision > 0:
        f1_score = 2 * (precision * recall) / (precision + recall)
    else:
        f1_score = 0.0

    return {
        "total_phi_entities": total_phi,
        "successfully_redacted": successfully_redacted,
        "leaked_failures": leaked,
        "total_detected": total_detected,
        "recall": f"{recall:.4f}",
        "precision": f"{precision:.4f}",
        "f1_score": f"{f1_score:.4f}"
    }


def create_trace_file(
    query_id: str,
    original_query: str,
    ground_truth_entities: List[Dict[str, Any]],
    anonymized_query: str,
    detected_entities: List[Dict[str, Any]],
    output_dir: str
) -> Dict[str, Any]:
    """Create trace file matching GPT-4o format with Presidio enhancements"""

    # Detect leaks
    leak_detection = detect_phi_leaks(original_query, anonymized_query, ground_truth_entities)

    # Calculate metrics
    metrics = calculate_metrics(ground_truth_entities, detected_entities, leak_detection)

    # Create trace structure
    trace = {
        "query_id": query_id,
        "timestamp": datetime.now().isoformat(),
        "model": "Presidio-2.2.360",
        "analyzer_config": {
            "language": "en",
            "score_threshold": 0.35,
            "custom_recognizers": ["MedicalRecordNumberRecognizer", "UniqueIdentifierRecognizer"]
        },
        "original_query": original_query,
        "ground_truth_phi": format_ground_truth_for_trace(ground_truth_entities),
        "ground_truth_phi_count": len(ground_truth_entities),
        "presidio_output": anonymized_query,
        "presidio_entities_detected": detected_entities,
        "leak_detection": leak_detection,
        "metrics": metrics
    }

    # Save trace file
    trace_file = os.path.join(output_dir, f"query_{query_id}_trace.json")
    with open(trace_file, 'w', encoding='utf-8') as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)

    return trace


def process_queries(
    csv_file: str,
    output_dir: str,
    query_type: str = "positive"
) -> Dict[str, Any]:
    """
    Process all queries from CSV and generate trace files

    Args:
        csv_file: Path to CSV file with queries
        output_dir: Directory to save trace files
        query_type: "positive" or "negative"

    Returns:
        Aggregate statistics
    """
    # Initialize Presidio
    print(f"Initializing Presidio analyzer...")
    analyzer = initialize_presidio_analyzer()
    anonymizer = AnonymizerEngine()

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Read queries
    print(f"Loading queries from {csv_file}...")
    queries = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        queries = list(reader)

    print(f"Processing {len(queries)} {query_type} queries...")

    # Process each query
    total_phi = 0
    total_redacted = 0
    total_leaked = 0
    queries_with_leaks = 0

    for i, query_row in enumerate(queries):
        query_id = query_row['query_id']
        query_text = query_row['query_text']
        phi_entities_json = query_row.get('phi_entities', '[]')

        # Parse ground truth
        ground_truth_entities = parse_ground_truth_phi(phi_entities_json)

        # Anonymize with Presidio
        anonymized_text, detected_entities = anonymize_with_presidio(
            query_text, analyzer, anonymizer
        )

        # Create trace file
        trace = create_trace_file(
            query_id,
            query_text,
            ground_truth_entities,
            anonymized_text,
            detected_entities,
            output_dir
        )

        # Aggregate statistics
        total_phi += trace['metrics']['total_phi_entities']
        total_redacted += trace['metrics']['successfully_redacted']
        total_leaked += trace['metrics']['leaked_failures']
        if trace['leak_detection']['leaked_phi_count'] > 0:
            queries_with_leaks += 1

        # Progress
        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(queries)} queries...")

    # Calculate aggregate metrics
    recall = total_redacted / total_phi if total_phi > 0 else 1.0

    aggregate = {
        "evaluation_timestamp": datetime.now().isoformat(),
        "model": "Presidio-2.2.360",
        "query_type": query_type,
        "total_queries_processed": len(queries),
        "total_phi_entities": total_phi,
        "successfully_redacted": total_redacted,
        "leaked_phi": total_leaked,
        "queries_with_leaks": queries_with_leaks,
        "queries_perfect_redaction": len(queries) - queries_with_leaks,
        "recall": recall,
        "perfect_rate": (len(queries) - queries_with_leaks) / len(queries) if queries else 0
    }

    # Save aggregate summary
    summary_file = os.path.join(output_dir, "aggregate_summary.json")
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(aggregate, f, indent=2)

    print(f"\n✓ Completed {len(queries)} queries")
    print(f"  Total PHI: {total_phi}")
    print(f"  Successfully redacted: {total_redacted}")
    print(f"  Leaked: {total_leaked}")
    print(f"  Recall: {recall:.2%}")
    print(f"  Perfect queries: {len(queries) - queries_with_leaks}/{len(queries)}")

    return aggregate


def main():
    """Main execution"""
    base_dir = "/Users/jacweath/Desktop/safesearch_/data"

    print("=" * 80)
    print("PRESIDIO HIPAA DE-IDENTIFICATION EVALUATION")
    print("=" * 80)

    # Process positive queries (PHI-containing)
    print("\n[1/1] POSITIVE QUERIES (PHI-containing)")
    print("-" * 80)

    positive_csv = os.path.join(base_dir, "positive_queries.csv")
    positive_output = os.path.join(base_dir, "Presidio_positive")

    positive_results = process_queries(
        csv_file=positive_csv,
        output_dir=positive_output,
        query_type="positive"
    )

    print("\n" + "=" * 80)
    print("EVALUATION COMPLETE")
    print("=" * 80)
    print(f"\nResults saved to: {positive_output}")
    print(f"  - {positive_results['total_queries_processed']} trace files")
    print(f"  - aggregate_summary.json")
    print(f"\nRecall: {positive_results['recall']:.2%}")
    print(f"Perfect redaction rate: {positive_results['perfect_rate']:.2%}")


if __name__ == "__main__":
    main()
