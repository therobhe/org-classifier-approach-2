"""
Test script to demonstrate the org-classifier pipeline.
Run with: python test_pipeline.py
"""
import asyncio
import pandas as pd
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from org_classifier.pipeline import ClassificationPipeline


async def test_basic_classification():
    """Test basic name-based classification."""
    print("=" * 60)
    print("Test 1: Basic Name Extraction")
    print("=" * 60)
    
    test_names = [
        "Siemens AG",
        "Deutsche Bank AG",
        "Boruscher Sportverein Dortmund e.V.",
        "REWE Markt GmbH",
        "SAP SE",
        "Unknown Company Name",
    ]
    
    async with ClassificationPipeline(offline=True) as pipeline:
        for name in test_names:
            result = await pipeline.classify_organisation(name)
            print(f"\n{name}")
            print(f"  Legal Form: {result.legal_form}")
            print(f"  Confidence: {result.confidence}")
            print(f"  Source: {result.source}")


async def test_csv_processing():
    """Test CSV processing with sample file."""
    print("\n" + "=" * 60)
    print("Test 2: CSV Processing (Offline Mode)")
    print("=" * 60)
    
    input_path = Path("sample_input.csv")
    output_path = Path("sample_output.csv")
    
    if not input_path.exists():
        print(f"\n⚠ Sample input file not found: {input_path}")
        print("Creating sample CSV...")
        
        sample_data = {
            "organisation_name": [
                "Siemens AG",
                "Deutsche Bank AG",
                "FC Bayern München e.V.",
                "REWE Markt GmbH",
                "Stiftung Warentest",
                "SAP SE",
            ]
        }
        df = pd.DataFrame(sample_data)
        df.to_csv(input_path, index=False)
        print(f"✓ Created {input_path}")
    
    async with ClassificationPipeline(offline=True, max_concurrent_requests=5) as pipeline:
        await pipeline.process_csv(
            input_path=input_path,
            output_path=output_path
        )
    
    print(f"\n✓ Output written to: {output_path}")
    
    # Display results
    df = pd.read_csv(output_path)
    print("\nResults:")
    print(df.to_string(index=False))


async def test_heuristics():
    """Test heuristic classification."""
    print("\n" + "=" * 60)
    print("Test 3: Heuristic Fallback")
    print("=" * 60)
    
    test_names = [
        "Münchener Verein für Geschichte",
        "Deutsche Stiftung Denkmalschutz",
        "Berliner Genossenschaft",
        "Random Company Without Legal Form",
    ]
    
    async with ClassificationPipeline(offline=True) as pipeline:
        for name in test_names:
            result = await pipeline.classify_organisation(name)
            print(f"\n{name}")
            print(f"  Legal Form: {result.legal_form}")
            print(f"  Confidence: {result.confidence}")
            print(f"  Source: {result.source}")


async def main():
    """Run all tests."""
    await test_basic_classification()
    await test_csv_processing()
    await test_heuristics()
    
    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
