import asyncio
import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.org_classifier.pipeline import ClassificationPipeline
from src.org_classifier.config import settings

INPUT = Path('test_input.csv')
OUTPUT = Path('test_output.csv')

with INPUT.open('w', encoding='utf-8') as f:
    f.write('organisation_name\n')
    f.write('Siemens AG\n')
    f.write('Robert Bosch GmbH\n')
    f.write('Khoroosh\n')
    f.write('Shotokan Dojo Sonkei\n')

async def run(offline: bool):
    async with ClassificationPipeline(offline=offline) as p:
        await p.process_csv(INPUT, OUTPUT, org_column='organisation_name')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Small pipeline smoke test (offline or Gemini online).')
    parser.add_argument('--online', action='store_true', help='Run with Gemini web classification enabled')
    args = parser.parse_args()

    offline = not args.online
    if not offline and not settings.gemini_api_key:
        print('GEMINI_API_KEY is not set. Export it first or run without --online.')
        sys.exit(2)

    asyncio.run(run(offline=offline))
    print('done')
